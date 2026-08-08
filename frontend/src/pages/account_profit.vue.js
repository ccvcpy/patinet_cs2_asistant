import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import FolioIcon from "../components/FolioIcon.vue";
import { AUDIT_STAGES, AUDIT_TABLES, GUADAO_AUDIT_STORAGE_KEY, auditCellValue, auditRowKey, buildAuditExportUrl, buildAuditRowsUrl, buildAuditRunRequest, defaultAuditForm, evidenceTone, extractEvidenceGaps, finiteNumber, firstValue, formatAuditCell, formatDateTime, formatMoney, formatMoneyFen, isActiveAuditStatus, normalizeAuditPreset, normalizeAuditRows, normalizeAuditRun, stageState, summaryNumber, validateAuditForm, verdictCopy, } from "./guadao_audit_shared";
const PAGE_SIZE = 25;
const POLL_INTERVAL_MS = 2000;
const initialForm = defaultAuditForm();
const form = reactive({ ...initialForm, accountIds: [] });
const presetForm = ref({ ...initialForm, accountIds: [] });
const accounts = ref([]);
const presetLoading = ref(true);
const presetWarning = ref("");
const run = ref(null);
const requestId = ref("");
const requestError = ref("");
const actionNotice = ref("");
const actionLoading = ref("");
const polling = ref(false);
const evidenceSelection = ref(null);
const tableStates = reactive({
    steamSales: emptyTableState("steamSales"),
    rebuyChains: emptyTableState("rebuyChains"),
    itemConservation: emptyTableState("itemConservation"),
    wallet: emptyTableState("wallet"),
});
let pollTimer;
let rowsLoadedForRequest = "";
function emptyTableState(dataset) {
    return { dataset, rows: [], page: 1, pageSize: PAGE_SIZE, total: 0, hasMore: false, loading: false, error: "" };
}
function tableState(dataset) {
    return tableStates[dataset];
}
function resetTables() {
    for (const table of AUDIT_TABLES) {
        Object.assign(tableStates[table.dataset], emptyTableState(table.dataset));
    }
    rowsLoadedForRequest = "";
}
function payloadRecord(payload) {
    return payload && typeof payload === "object" && !Array.isArray(payload)
        ? payload
        : {};
}
function payloadError(payload, fallback) {
    const record = payloadRecord(payload);
    return String(record.error || record.detail || record.message || fallback);
}
async function requestJson(path, init) {
    const response = await fetch(path, {
        cache: "no-store",
        ...init,
        headers: init?.body
            ? { "Content-Type": "application/json", ...(init.headers || {}) }
            : { ...(init?.headers || {}) },
    });
    const text = await response.text();
    let payload = {};
    if (text) {
        try {
            payload = JSON.parse(text);
        }
        catch {
            payload = { error: text.slice(0, 500) };
        }
    }
    const record = payloadRecord(payload);
    if (!response.ok || record.ok === false) {
        throw new Error(payloadError(payload, `请求失败（HTTP ${response.status}）`));
    }
    return { response, payload };
}
function applyPresetValues() {
    Object.assign(form, {
        dateFrom: presetForm.value.dateFrom,
        dateTo: presetForm.value.dateTo,
        openingWallet: presetForm.value.openingWallet,
        openingRealValue: presetForm.value.openingRealValue,
        accountIds: [...presetForm.value.accountIds],
    });
    requestError.value = "";
    actionNotice.value = "已恢复只读测试基准；尚未向后端提交任务。";
}
async function loadPresets() {
    presetLoading.value = true;
    presetWarning.value = "";
    const fallback = normalizeAuditPreset({});
    try {
        const { payload } = await requestJson("/api/guadao-audit/presets");
        const preset = normalizeAuditPreset(payload);
        accounts.value = preset.accounts;
        const accountIds = preset.accounts.map((account) => account.id);
        presetForm.value = {
            dateFrom: preset.dateFrom,
            dateTo: preset.dateTo,
            openingWallet: preset.openingWallet,
            openingRealValue: preset.openingRealValue,
            accountIds,
        };
        Object.assign(form, presetForm.value, { accountIds: [...accountIds] });
        if (preset.accounts.length !== 5) {
            presetWarning.value = `预设接口当前返回 ${preset.accounts.length} 个账号；任务仍会按后端 strict_official 口径核验。`;
        }
    }
    catch (reason) {
        accounts.value = [];
        presetForm.value = {
            dateFrom: fallback.dateFrom,
            dateTo: fallback.dateTo,
            openingWallet: fallback.openingWallet,
            openingRealValue: fallback.openingRealValue,
            accountIds: [],
        };
        Object.assign(form, presetForm.value, { accountIds: [] });
        presetWarning.value = `预设接口暂不可读，已使用内置基准；账号范围提交为 all。${reason instanceof Error ? ` ${reason.message}` : ""}`;
    }
    finally {
        presetLoading.value = false;
    }
}
function saveRequestId(value) {
    requestId.value = value;
    try {
        window.localStorage.setItem(GUADAO_AUDIT_STORAGE_KEY, value);
    }
    catch {
        actionNotice.value = "任务已创建，但浏览器无法保存最近 requestId；本页仍可继续轮询。";
    }
}
function restoreStoredRequestId() {
    try {
        return window.localStorage.getItem(GUADAO_AUDIT_STORAGE_KEY)?.trim() || "";
    }
    catch {
        return "";
    }
}
function stopPolling() {
    if (pollTimer !== undefined)
        window.clearTimeout(pollTimer);
    pollTimer = undefined;
    polling.value = false;
}
function schedulePoll(delay = POLL_INTERVAL_MS) {
    stopPolling();
    polling.value = true;
    pollTimer = window.setTimeout(() => void refreshRun(true), delay);
}
async function restoreLastRun() {
    const stored = restoreStoredRequestId();
    if (!stored)
        return;
    requestId.value = stored;
    await refreshRun(false);
}
async function refreshRun(silent) {
    const targetId = requestId.value.trim();
    if (!targetId)
        return;
    if (!silent)
        requestError.value = "";
    try {
        const { payload } = await requestJson(`/api/guadao-audit/runs/${encodeURIComponent(targetId)}`);
        if (requestId.value !== targetId)
            return;
        run.value = normalizeAuditRun(payload, targetId);
        requestError.value = "";
        if (isActiveAuditStatus(run.value.status)) {
            schedulePoll();
            return;
        }
        stopPolling();
        if (rowsLoadedForRequest !== targetId)
            await loadAllTables();
    }
    catch (reason) {
        requestError.value = reason instanceof Error ? reason.message : String(reason);
        if (run.value && isActiveAuditStatus(run.value.status))
            schedulePoll(5000);
    }
}
async function startAudit() {
    const errors = validateAuditForm(form);
    if (errors.length) {
        requestError.value = errors.join("；");
        return;
    }
    stopPolling();
    actionLoading.value = "start";
    requestError.value = "";
    actionNotice.value = "";
    resetTables();
    try {
        const { response, payload } = await requestJson("/api/guadao-audit/runs", {
            method: "POST",
            body: JSON.stringify(buildAuditRunRequest(form)),
        });
        if (response.status !== 202) {
            throw new Error(`创建任务必须返回 HTTP 202，当前为 ${response.status}`);
        }
        const accepted = normalizeAuditRun(payload);
        if (!accepted.requestId)
            throw new Error("后端已接受请求，但未返回 requestId");
        saveRequestId(accepted.requestId);
        run.value = {
            ...accepted,
            status: "queued",
            verdict: null,
            stage: null,
            progress: { done: 0, total: accepted.progress.total, percent: 0, message: "已进入只读对账队列" },
        };
        actionNotice.value = "任务已入队。HTTP 202 仅表示排队成功，页面会继续轮询真实终态。";
        schedulePoll(1200);
    }
    catch (reason) {
        requestError.value = reason instanceof Error ? reason.message : String(reason);
    }
    finally {
        actionLoading.value = "";
    }
}
async function cancelAudit() {
    if (!requestId.value || !run.value || !isActiveAuditStatus(run.value.status))
        return;
    actionLoading.value = "cancel";
    requestError.value = "";
    try {
        await requestJson(`/api/guadao-audit/runs/${encodeURIComponent(requestId.value)}/cancel`, { method: "POST" });
        actionNotice.value = "已提交取消请求；等待后端确认最终状态。";
        schedulePoll(500);
    }
    catch (reason) {
        requestError.value = reason instanceof Error ? reason.message : String(reason);
    }
    finally {
        actionLoading.value = "";
    }
}
async function retryAudit() {
    if (!requestId.value || !run.value || isActiveAuditStatus(run.value.status))
        return;
    const previousId = requestId.value;
    actionLoading.value = "retry";
    requestError.value = "";
    actionNotice.value = "";
    try {
        const { response, payload } = await requestJson(`/api/guadao-audit/runs/${encodeURIComponent(previousId)}/retry`, { method: "POST" });
        if (![200, 202].includes(response.status))
            throw new Error(`重试任务返回了意外状态 ${response.status}`);
        const accepted = normalizeAuditRun(payload);
        if (!accepted.requestId)
            throw new Error("重试已接受，但未返回新的 requestId");
        resetTables();
        saveRequestId(accepted.requestId);
        run.value = response.status === 202
            ? {
                ...accepted,
                status: "queued",
                verdict: null,
                stage: null,
                progress: { done: 0, total: accepted.progress.total, percent: 0, message: "重试任务已进入只读队列" },
            }
            : accepted;
        actionNotice.value = response.status === 202
            ? `已从 ${previousId} 创建新的只读审计尝试；HTTP 202 仅表示排队，旧结果保持不变。`
            : `已从 ${previousId} 创建新的只读审计尝试；旧结果保持不变。`;
        if (isActiveAuditStatus(run.value.status))
            schedulePoll(1200);
        else
            await loadAllTables();
    }
    catch (reason) {
        requestError.value = reason instanceof Error ? reason.message : String(reason);
    }
    finally {
        actionLoading.value = "";
    }
}
async function loadTable(dataset, page = 1) {
    const targetId = requestId.value;
    if (!targetId)
        return;
    const state = tableStates[dataset];
    state.loading = true;
    state.error = "";
    try {
        const { payload } = await requestJson(buildAuditRowsUrl(targetId, dataset, page, state.pageSize));
        if (requestId.value !== targetId)
            return;
        Object.assign(state, normalizeAuditRows(payload, dataset, page, state.pageSize), { loading: false, error: "" });
    }
    catch (reason) {
        state.error = reason instanceof Error ? reason.message : String(reason);
        state.loading = false;
    }
}
async function loadAllTables() {
    const targetId = requestId.value;
    if (!targetId)
        return;
    await Promise.all(AUDIT_TABLES.map((table) => loadTable(table.dataset, 1)));
    if (requestId.value === targetId)
        rowsLoadedForRequest = targetId;
}
function previousPage(dataset) {
    const state = tableStates[dataset];
    if (state.page <= 1 || state.loading)
        return;
    void loadTable(dataset, state.page - 1);
}
function nextPage(dataset) {
    const state = tableStates[dataset];
    if (!state.hasMore || state.loading)
        return;
    void loadTable(dataset, state.page + 1);
}
function openRowEvidence(table, row) {
    evidenceSelection.value = {
        table,
        row,
        gaps: extractEvidenceGaps(row),
        title: `${table.title} · 证据详情`,
    };
}
function openRunEvidence() {
    if (!run.value)
        return;
    const loadedRowGaps = AUDIT_TABLES.flatMap((table) => (tableStates[table.dataset].rows.flatMap((row) => extractEvidenceGaps(row))));
    evidenceSelection.value = {
        table: AUDIT_TABLES[0],
        row: null,
        gaps: [...run.value.evidenceGaps, ...loadedRowGaps],
        title: "任务级证据缺口",
    };
}
function closeEvidence() {
    evidenceSelection.value = null;
}
function handleEscape(event) {
    if (event.key === "Escape")
        closeEvidence();
}
function rowGapCount(row) {
    return extractEvidenceGaps(row).length;
}
function cellClass(row, column) {
    const value = auditCellValue(row, column);
    const parsed = finiteNumber(value);
    return [
        `cell-${column.format}`,
        {
            "cell-strong": column.tone === "strong",
            "cell-difference": column.tone === "difference" && parsed !== null && Math.abs(parsed) > 0.000001,
        },
    ];
}
function evidenceClass(row, column) {
    return `evidence-${evidenceTone(auditCellValue(row, column))}`;
}
function rowIdentifier(row, index) {
    return String(firstValue(row, ["requestId", "listingId", "purchaseId", "sourceSellOperationId", "marketHashName", "accountId"]) || `第 ${index + 1} 行`);
}
function formatCount(value) {
    return value === null ? "—" : new Intl.NumberFormat("zh-CN").format(Math.trunc(value));
}
function kpiTone(equal, dangerWhenPositive = false) {
    if (equal === null)
        return "neutral";
    if (dangerWhenPositive)
        return equal ? "danger" : "success";
    return equal ? "success" : "danger";
}
const active = computed(() => Boolean(run.value && isActiveAuditStatus(run.value.status)));
const terminal = computed(() => Boolean(run.value && !isActiveAuditStatus(run.value.status)));
const verdict = computed(() => {
    if (run.value?.status === "failed" && !run.value.verdict) {
        return {
            label: "RUN FAILED",
            title: "只读审计任务执行失败",
            description: run.value.error?.message || "后端未能完成证据采集；该状态不是对账失败结论。",
        };
    }
    if (run.value?.status === "cancelled") {
        return {
            label: "CANCELLED",
            title: "只读审计任务已取消",
            description: "取消只停止本次证据采集，不会改变旧结果、策略配置或交易状态。",
        };
    }
    return verdictCopy(run.value?.verdict || null);
});
const progressPercent = computed(() => Math.round(run.value?.progress.percent || 0));
const statusLabel = computed(() => {
    if (!run.value)
        return "尚未开始";
    return {
        queued: "排队中",
        running: "核验中",
        completed: "已完成",
        failed: "任务失败",
        cancelled: "已取消",
    }[run.value.status];
});
const statusTone = computed(() => {
    if (!run.value)
        return "neutral";
    if (run.value.status === "completed")
        return run.value.verdict === "passed" ? "success" : run.value.verdict === "failed" ? "danger" : "warning";
    if (run.value.status === "failed")
        return "danger";
    if (run.value.status === "cancelled")
        return "neutral";
    return "running";
});
const kpis = computed(() => {
    const summary = run.value?.summary || {};
    const programSales = summaryNumber(summary, ["programSteamSalesCount", "programSalesCount", "localSoldCount"]);
    const officialSales = summaryNumber(summary, ["officialSteamSalesCount", "steamOfficialSalesCount", "steamSalesCount"]);
    const tracked = summaryNumber(summary, ["trackedRebuyCount", "rebuyDispositionCount", "accountedSellCount"]);
    const mismatchItems = summaryNumber(summary, ["itemMismatchCount", "quantityMismatchItemCount", "conservationMismatchCount"]);
    const walletDifferenceFen = summaryNumber(summary, ["walletDifferenceFen", "endingWalletDifferenceFen", "balanceDifferenceFen"]);
    const salesBoolean = firstValue(summary, ["programSalesEqualOfficial"]);
    const destinationBoolean = firstValue(summary, ["allSalesHaveDestination"]);
    const conservationBoolean = firstValue(summary, ["allItemsConserved"]);
    const walletBoolean = firstValue(summary, ["walletReconciled"]);
    const salesEqual = programSales !== null && officialSales !== null
        ? programSales === officialSales
        : typeof salesBoolean === "boolean" ? salesBoolean : null;
    const trackedEqual = tracked !== null && officialSales !== null
        ? tracked === officialSales
        : typeof destinationBoolean === "boolean" ? destinationBoolean : null;
    const mismatchPositive = mismatchItems !== null
        ? mismatchItems > 0
        : typeof conservationBoolean === "boolean" ? !conservationBoolean : null;
    const walletRow = tableStates.wallet.rows[0] || {};
    const walletDifference = summaryNumber(walletRow, ["balanceDifference"]);
    const walletDifferent = walletDifferenceFen !== null
        ? Math.abs(walletDifferenceFen) > 0
        : walletDifference !== null ? Math.abs(walletDifference) > 0 : typeof walletBoolean === "boolean" ? !walletBoolean : null;
    const salesValue = programSales !== null || officialSales !== null
        ? `${formatCount(programSales)} / ${formatCount(officialSales)}`
        : salesEqual === null ? "—" : salesEqual ? "一致" : "存在差异";
    const destinationValue = tracked !== null || officialSales !== null
        ? `${formatCount(tracked)} / ${formatCount(officialSales)}`
        : trackedEqual === null ? "—" : trackedEqual ? "全部有去向" : "存在断链";
    const conservationValue = mismatchItems !== null
        ? `${formatCount(mismatchItems)} 个`
        : mismatchPositive === null ? "—" : mismatchPositive ? "存在数量差" : "全部守恒";
    const walletValue = walletDifferenceFen !== null
        ? formatMoneyFen(walletDifferenceFen)
        : walletDifference !== null ? formatMoney(walletDifference) : walletDifferent === null ? "—" : walletDifferent ? "存在差额" : "已对平";
    return [
        {
            label: "Steam 卖出匹配",
            value: salesValue,
            note: "程序记录 / 官方记录",
            icon: "link",
            tone: kpiTone(salesEqual),
        },
        {
            label: "卖出去向覆盖",
            value: destinationValue,
            note: "已追踪去向 / 官方卖出",
            icon: "shield",
            tone: kpiTone(trackedEqual),
        },
        {
            label: "数量差物品",
            value: conservationValue,
            note: "必须逐项为 0",
            icon: "case",
            tone: kpiTone(mismatchPositive, true),
        },
        {
            label: "Steam 钱包差额",
            value: walletValue,
            note: "推算期末 - 官方实际",
            icon: "wallet",
            tone: kpiTone(walletDifferent, true),
        },
    ];
});
const openingDiscount = computed(() => {
    const wallet = finiteNumber(form.openingWallet);
    const realValue = finiteNumber(form.openingRealValue);
    if (wallet === null || realValue === null || wallet === 0)
        return "—";
    return new Intl.NumberFormat("zh-CN", { style: "percent", minimumFractionDigits: 4, maximumFractionDigits: 6 }).format(realValue / wallet);
});
const accountCaption = computed(() => {
    if (accounts.value.length)
        return `${accounts.value.length} 个预设 Steam 账号`;
    return "全部已配置 Steam 账号（预期 5 个）";
});
const loadedGapCount = computed(() => AUDIT_TABLES.reduce((total, table) => (total + tableStates[table.dataset].rows.reduce((rowTotal, row) => rowTotal + rowGapCount(row), 0)), run.value?.evidenceGaps.length || 0));
const drawerFields = computed(() => {
    const selection = evidenceSelection.value;
    if (!selection?.row)
        return [];
    return selection.table.columns.map((column) => ({
        label: column.label,
        value: formatAuditCell(selection.row, column),
    }));
});
function coverageText(source) {
    const coverage = run.value?.coverage || {};
    const aliases = source === "steam"
        ? ["steamHistory", "steam", "steamCoverage", "steamComplete"]
        : source === "wallet"
            ? ["steamBalance", "wallet", "walletCoverage", "walletComplete"]
            : ["c5", "c5Coverage", "c5Complete"];
    const raw = firstValue(coverage, aliases);
    if (typeof raw === "boolean")
        return raw ? "覆盖完整" : "覆盖不完整";
    if (raw && typeof raw === "object" && !Array.isArray(raw)) {
        const record = raw;
        const complete = firstValue(record, ["coverageComplete", "complete", "rangeCoverageComplete"]);
        if (complete === true)
            return "覆盖完整";
        if (complete === false)
            return "覆盖不完整";
        return String(firstValue(record, ["label", "status", "message"]) || "待核验");
    }
    return raw === undefined || raw === null || raw === "" ? "待核验" : String(raw);
}
function coverageTone(source) {
    const text = coverageText(source);
    if (text === "覆盖完整")
        return "success";
    if (text === "覆盖不完整")
        return "warning";
    return "neutral";
}
function exportUrl(format) {
    return requestId.value ? buildAuditExportUrl(requestId.value, format) : "#";
}
onMounted(async () => {
    window.addEventListener("keydown", handleEscape);
    await loadPresets();
    await restoreLastRun();
});
onBeforeUnmount(() => {
    stopPolling();
    window.removeEventListener("keydown", handleEscape);
});
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
/** @type {__VLS_StyleScopedClasses['audit-hero']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-overline']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-hero']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-hero']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-overline']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-badges']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-run-state']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-run-state']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-run-state']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-run-state']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-run-state']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-run-state']} */ ;
/** @type {__VLS_StyleScopedClasses['export-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['panel-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['table-panel-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['export-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['panel-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['table-panel-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['export-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['panel-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-form-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-form-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-form-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-form-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-form-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-form-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['money-input']} */ ;
/** @type {__VLS_StyleScopedClasses['money-input']} */ ;
/** @type {__VLS_StyleScopedClasses['account-scope']} */ ;
/** @type {__VLS_StyleScopedClasses['account-scope']} */ ;
/** @type {__VLS_StyleScopedClasses['account-scope']} */ ;
/** @type {__VLS_StyleScopedClasses['account-chips']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['progress-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['progress-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['progress-bar']} */ ;
/** @type {__VLS_StyleScopedClasses['stage-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['stage-marker']} */ ;
/** @type {__VLS_StyleScopedClasses['stage-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['stage-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['stage-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['stage-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['stage-current']} */ ;
/** @type {__VLS_StyleScopedClasses['stage-marker']} */ ;
/** @type {__VLS_StyleScopedClasses['stage-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['stage-marker']} */ ;
/** @type {__VLS_StyleScopedClasses['stage-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['stage-marker']} */ ;
/** @type {__VLS_StyleScopedClasses['stage-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['stage-marker']} */ ;
/** @type {__VLS_StyleScopedClasses['progress-message']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-kpi']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-kpi']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-kpi']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-kpi']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-kpi']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-kpi']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-kpi']} */ ;
/** @type {__VLS_StyleScopedClasses['kpi-danger']} */ ;
/** @type {__VLS_StyleScopedClasses['kpi-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['verdict-copy']} */ ;
/** @type {__VLS_StyleScopedClasses['verdict-copy']} */ ;
/** @type {__VLS_StyleScopedClasses['verdict-copy']} */ ;
/** @type {__VLS_StyleScopedClasses['verdict-passed']} */ ;
/** @type {__VLS_StyleScopedClasses['verdict-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['verdict-failed']} */ ;
/** @type {__VLS_StyleScopedClasses['verdict-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['verdict-inconclusive']} */ ;
/** @type {__VLS_StyleScopedClasses['verdict-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['coverage-list']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-table-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['table-panel-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-table-wrap']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-data-table']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-data-table']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-data-table']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-data-table']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-data-table']} */ ;
/** @type {__VLS_StyleScopedClasses['evidence-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['evidence-button']} */ ;
/** @type {__VLS_StyleScopedClasses['evidence-button']} */ ;
/** @type {__VLS_StyleScopedClasses['empty-table-cell']} */ ;
/** @type {__VLS_StyleScopedClasses['table-pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['table-pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['table-pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['table-pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['export-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['export-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['export-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['export-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['disabled']} */ ;
/** @type {__VLS_StyleScopedClasses['evidence-drawer']} */ ;
/** @type {__VLS_StyleScopedClasses['evidence-drawer']} */ ;
/** @type {__VLS_StyleScopedClasses['evidence-drawer']} */ ;
/** @type {__VLS_StyleScopedClasses['evidence-row-id']} */ ;
/** @type {__VLS_StyleScopedClasses['evidence-row-id']} */ ;
/** @type {__VLS_StyleScopedClasses['evidence-field-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['evidence-field-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['evidence-field-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['gap-list']} */ ;
/** @type {__VLS_StyleScopedClasses['gap-list']} */ ;
/** @type {__VLS_StyleScopedClasses['gap-list']} */ ;
/** @type {__VLS_StyleScopedClasses['gap-list']} */ ;
/** @type {__VLS_StyleScopedClasses['gap-list']} */ ;
/** @type {__VLS_StyleScopedClasses['gap-list']} */ ;
/** @type {__VLS_StyleScopedClasses['gap-list']} */ ;
/** @type {__VLS_StyleScopedClasses['gap-list']} */ ;
/** @type {__VLS_StyleScopedClasses['gap-list']} */ ;
/** @type {__VLS_StyleScopedClasses['gap-list']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-form-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-kpi-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['stage-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['verdict-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['coverage-list']} */ ;
/** @type {__VLS_StyleScopedClasses['evidence-summary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['guadao-audit-page']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-hero']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-run-state']} */ ;
/** @type {__VLS_StyleScopedClasses['panel-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['table-panel-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['export-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['account-scope']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-form-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-kpi-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['stage-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['account-chips']} */ ;
/** @type {__VLS_StyleScopedClasses['read-only-note']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['verdict-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['coverage-list']} */ ;
/** @type {__VLS_StyleScopedClasses['evidence-summary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['table-pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['table-pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['table-pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['export-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['evidence-field-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['gap-list']} */ ;
// CSS variable injection 
// CSS variable injection end 
__VLS_asFunctionalElement(__VLS_intrinsicElements.main, __VLS_intrinsicElements.main)({
    ...{ class: "page guadao-audit-page" },
    'aria-label': "挂刀执行-测试工具",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "audit-hero" },
    'aria-labelledby': "audit-page-title",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "hero-copy" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
    ...{ class: "audit-overline" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h1, __VLS_intrinsicElements.h1)({
    id: "audit-page-title",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "hero-badges" },
    'aria-label': "安全边界",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_0 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "shield",
    size: (14),
}));
const __VLS_1 = __VLS_0({
    name: "shield",
    size: (14),
}, ...__VLS_functionalComponentArgsRest(__VLS_0));
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_3 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "lock",
    size: (14),
}));
const __VLS_4 = __VLS_3({
    name: "lock",
    size: (14),
}, ...__VLS_functionalComponentArgsRest(__VLS_3));
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_6 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "document",
    size: (14),
}));
const __VLS_7 = __VLS_6({
    name: "document",
    size: (14),
}, ...__VLS_functionalComponentArgsRest(__VLS_6));
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "hero-run-state" },
    ...{ class: (`tone-${__VLS_ctx.statusTone}`) },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
(__VLS_ctx.statusLabel);
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.run ? `${__VLS_ctx.progressPercent}%` : "—");
if (__VLS_ctx.requestId) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({
        ...{ class: "mono" },
    });
    (__VLS_ctx.requestId);
}
else {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "panel audit-setup-panel" },
    'aria-labelledby': "audit-setup-title",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "panel-heading" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
    ...{ class: "section-overline" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({
    id: "audit-setup-title",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.applyPresetValues) },
    ...{ class: "secondary-button" },
    type: "button",
    disabled: (__VLS_ctx.active || __VLS_ctx.presetLoading),
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_9 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "refresh",
    size: (15),
}));
const __VLS_10 = __VLS_9({
    name: "refresh",
    size: (15),
}, ...__VLS_functionalComponentArgsRest(__VLS_9));
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "audit-form-grid" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
    type: "datetime-local",
    disabled: (__VLS_ctx.active),
});
(__VLS_ctx.form.dateFrom);
__VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
    type: "datetime-local",
    disabled: (__VLS_ctx.active),
});
(__VLS_ctx.form.dateTo);
__VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "money-input" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
    type: "number",
    min: "0",
    step: "0.01",
    disabled: (__VLS_ctx.active),
});
(__VLS_ctx.form.openingWallet);
__VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "money-input" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
    type: "number",
    min: "0",
    step: "0.001",
    disabled: (__VLS_ctx.active),
});
(__VLS_ctx.form.openingRealValue);
__VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
(__VLS_ctx.openingDiscount);
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "account-scope" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.accountCaption);
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "account-chips" },
});
for (const [account] of __VLS_getVForSourceType((__VLS_ctx.accounts))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        key: (account.id),
        title: (account.steamId || account.id),
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_12 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: "account",
        size: (13),
    }));
    const __VLS_13 = __VLS_12({
        name: "account",
        size: (13),
    }, ...__VLS_functionalComponentArgsRest(__VLS_12));
    (account.label);
}
if (!__VLS_ctx.accounts.length) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_15 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: "account",
        size: (13),
    }));
    const __VLS_16 = __VLS_15({
        name: "account",
        size: (13),
    }, ...__VLS_functionalComponentArgsRest(__VLS_15));
}
if (__VLS_ctx.presetWarning) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "audit-message warning-message" },
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_18 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: "warning",
        size: (16),
    }));
    const __VLS_19 = __VLS_18({
        name: "warning",
        size: (16),
    }, ...__VLS_functionalComponentArgsRest(__VLS_18));
    (__VLS_ctx.presetWarning);
}
if (__VLS_ctx.requestError) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "audit-message error-message" },
        role: "alert",
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_21 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: "error",
        size: (16),
    }));
    const __VLS_22 = __VLS_21({
        name: "error",
        size: (16),
    }, ...__VLS_functionalComponentArgsRest(__VLS_21));
    (__VLS_ctx.requestError);
}
else if (__VLS_ctx.run?.error) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "audit-message error-message" },
        role: "alert",
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_24 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: "error",
        size: (16),
    }));
    const __VLS_25 = __VLS_24({
        name: "error",
        size: (16),
    }, ...__VLS_functionalComponentArgsRest(__VLS_24));
    (__VLS_ctx.run.error.source);
    (__VLS_ctx.run.error.code);
    (__VLS_ctx.run.error.message);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.run.error.retryable ? "可重试" : "不可自动重试");
}
else if (__VLS_ctx.actionNotice) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "audit-message success-message" },
        'aria-live': "polite",
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_27 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: "info",
        size: (16),
    }));
    const __VLS_28 = __VLS_27({
        name: "info",
        size: (16),
    }, ...__VLS_functionalComponentArgsRest(__VLS_27));
    (__VLS_ctx.actionNotice);
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "audit-actions" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.startAudit) },
    ...{ class: "primary-button" },
    type: "button",
    disabled: (__VLS_ctx.active || __VLS_ctx.actionLoading !== '' || __VLS_ctx.presetLoading),
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_30 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "play",
    size: (16),
}));
const __VLS_31 = __VLS_30({
    name: "play",
    size: (16),
}, ...__VLS_functionalComponentArgsRest(__VLS_30));
(__VLS_ctx.actionLoading === "start" ? "正在提交" : "开始严格对账");
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.cancelAudit) },
    ...{ class: "secondary-button cancel-button" },
    type: "button",
    disabled: (!__VLS_ctx.active || __VLS_ctx.actionLoading !== ''),
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_33 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "pause",
    size: (16),
}));
const __VLS_34 = __VLS_33({
    name: "pause",
    size: (16),
}, ...__VLS_functionalComponentArgsRest(__VLS_33));
(__VLS_ctx.actionLoading === "cancel" ? "正在提交" : "取消任务");
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.retryAudit) },
    ...{ class: "secondary-button" },
    type: "button",
    disabled: (!__VLS_ctx.terminal || __VLS_ctx.actionLoading !== ''),
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_36 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "refresh",
    size: (16),
}));
const __VLS_37 = __VLS_36({
    name: "refresh",
    size: (16),
}, ...__VLS_functionalComponentArgsRest(__VLS_36));
(__VLS_ctx.actionLoading === "retry" ? "正在创建" : "重试任务");
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "read-only-note" },
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_39 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "shield",
    size: (15),
}));
const __VLS_40 = __VLS_39({
    name: "shield",
    size: (15),
}, ...__VLS_functionalComponentArgsRest(__VLS_39));
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "panel progress-panel" },
    'aria-labelledby': "audit-progress-title",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "panel-heading progress-heading" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
    ...{ class: "section-overline" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({
    id: "audit-progress-title",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "progress-summary" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
(__VLS_ctx.statusLabel);
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.progressPercent);
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "progress-bar" },
    'aria-hidden': "true",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span)({
    ...{ style: ({ width: `${__VLS_ctx.progressPercent}%` }) },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.ol, __VLS_intrinsicElements.ol)({
    ...{ class: "stage-grid" },
});
for (const [stage] of __VLS_getVForSourceType((__VLS_ctx.AUDIT_STAGES))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.li, __VLS_intrinsicElements.li)({
        key: (stage.key),
        ...{ class: (`stage-${__VLS_ctx.stageState(stage.key, __VLS_ctx.run)}`) },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "stage-marker" },
    });
    if (__VLS_ctx.stageState(stage.key, __VLS_ctx.run) === 'completed') {
        /** @type {[typeof FolioIcon, ]} */ ;
        // @ts-ignore
        const __VLS_42 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
            name: "success",
            size: (16),
        }));
        const __VLS_43 = __VLS_42({
            name: "success",
            size: (16),
        }, ...__VLS_functionalComponentArgsRest(__VLS_42));
    }
    else if (__VLS_ctx.stageState(stage.key, __VLS_ctx.run) === 'failed') {
        /** @type {[typeof FolioIcon, ]} */ ;
        // @ts-ignore
        const __VLS_45 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
            name: "error",
            size: (16),
        }));
        const __VLS_46 = __VLS_45({
            name: "error",
            size: (16),
        }, ...__VLS_functionalComponentArgsRest(__VLS_45));
    }
    else if (__VLS_ctx.stageState(stage.key, __VLS_ctx.run) === 'cancelled') {
        /** @type {[typeof FolioIcon, ]} */ ;
        // @ts-ignore
        const __VLS_48 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
            name: "pause",
            size: (15),
        }));
        const __VLS_49 = __VLS_48({
            name: "pause",
            size: (15),
        }, ...__VLS_functionalComponentArgsRest(__VLS_48));
    }
    else {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
        (stage.index + 1);
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (stage.label);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (stage.hint);
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
    ...{ class: "progress-message" },
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_51 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: (__VLS_ctx.polling ? 'refresh' : 'clock'),
    ...{ class: ({ spinning: __VLS_ctx.polling }) },
    size: (15),
}));
const __VLS_52 = __VLS_51({
    name: (__VLS_ctx.polling ? 'refresh' : 'clock'),
    ...{ class: ({ spinning: __VLS_ctx.polling }) },
    size: (15),
}, ...__VLS_functionalComponentArgsRest(__VLS_51));
(__VLS_ctx.run?.progress.message || (__VLS_ctx.run ? "等待后端更新阶段进度" : "开始任务后显示真实采集进度"));
if (__VLS_ctx.run?.progress.total) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.run.progress.done);
    (__VLS_ctx.run.progress.total);
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "audit-kpi-grid" },
    'aria-label': "核心对账指标",
});
for (const [kpi] of __VLS_getVForSourceType((__VLS_ctx.kpis))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
        key: (kpi.label),
        ...{ class: "audit-kpi" },
        ...{ class: (`kpi-${kpi.tone}`) },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "kpi-icon" },
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_54 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: (kpi.icon),
        size: (19),
    }));
    const __VLS_55 = __VLS_54({
        name: (kpi.icon),
        size: (19),
    }, ...__VLS_functionalComponentArgsRest(__VLS_54));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (kpi.label);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (kpi.value);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (kpi.note);
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "verdict-panel" },
    ...{ class: (`verdict-${__VLS_ctx.run?.verdict || 'waiting'}`) },
    'aria-live': "polite",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "verdict-icon" },
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_57 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: (__VLS_ctx.run?.verdict === 'passed' ? 'success' : __VLS_ctx.run?.verdict === 'failed' ? 'error' : __VLS_ctx.run?.verdict === 'inconclusive' ? 'warning' : 'circle-dashed'),
    size: (25),
}));
const __VLS_58 = __VLS_57({
    name: (__VLS_ctx.run?.verdict === 'passed' ? 'success' : __VLS_ctx.run?.verdict === 'failed' ? 'error' : __VLS_ctx.run?.verdict === 'inconclusive' ? 'warning' : 'circle-dashed'),
    size: (25),
}, ...__VLS_functionalComponentArgsRest(__VLS_57));
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "verdict-copy" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
(__VLS_ctx.verdict.label);
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
(__VLS_ctx.verdict.title);
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
(__VLS_ctx.verdict.description);
if (__VLS_ctx.run?.updatedAt) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (__VLS_ctx.formatDateTime(__VLS_ctx.run.updatedAt));
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "coverage-list" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: (`coverage-${__VLS_ctx.coverageTone('steam')}`) },
});
(__VLS_ctx.coverageText("steam"));
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: (`coverage-${__VLS_ctx.coverageTone('c5')}`) },
});
(__VLS_ctx.coverageText("c5"));
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: (`coverage-${__VLS_ctx.coverageTone('wallet')}`) },
});
(__VLS_ctx.coverageText("wallet"));
if (__VLS_ctx.run && __VLS_ctx.loadedGapCount) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.openRunEvidence) },
        ...{ class: "evidence-summary-button" },
        type: "button",
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_60 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: "warning",
        size: (15),
    }));
    const __VLS_61 = __VLS_60({
        name: "warning",
        size: (15),
    }, ...__VLS_functionalComponentArgsRest(__VLS_60));
    (__VLS_ctx.loadedGapCount);
}
for (const [table] of __VLS_getVForSourceType((__VLS_ctx.AUDIT_TABLES))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        key: (table.dataset),
        ...{ class: "panel audit-table-panel" },
        'aria-labelledby': (`${table.dataset}-title`),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "table-panel-heading" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "section-overline" },
    });
    (table.dataset);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({
        id: (`${table.dataset}-title`),
    });
    (table.title);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (table.description);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "row-count" },
    });
    (__VLS_ctx.tableState(table.dataset).total);
    if (__VLS_ctx.tableState(table.dataset).error) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
            ...{ class: "table-error" },
        });
        /** @type {[typeof FolioIcon, ]} */ ;
        // @ts-ignore
        const __VLS_63 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
            name: "error",
            size: (15),
        }));
        const __VLS_64 = __VLS_63({
            name: "error",
            size: (15),
        }, ...__VLS_functionalComponentArgsRest(__VLS_63));
        (__VLS_ctx.tableState(table.dataset).error);
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "audit-table-wrap" },
        ...{ class: ({ 'is-loading': __VLS_ctx.tableState(table.dataset).loading }) },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.table, __VLS_intrinsicElements.table)({
        ...{ class: "audit-data-table" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.thead, __VLS_intrinsicElements.thead)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.tr, __VLS_intrinsicElements.tr)({});
    for (const [column] of __VLS_getVForSourceType((table.columns))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.th, __VLS_intrinsicElements.th)({
            key: (column.key),
        });
        (column.label);
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.th, __VLS_intrinsicElements.th)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.tbody, __VLS_intrinsicElements.tbody)({});
    for (const [row, index] of __VLS_getVForSourceType((__VLS_ctx.tableState(table.dataset).rows))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.tr, __VLS_intrinsicElements.tr)({
            key: (__VLS_ctx.auditRowKey(row, table.dataset, index)),
        });
        for (const [column] of __VLS_getVForSourceType((table.columns))) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({
                key: (column.key),
                ...{ class: (__VLS_ctx.cellClass(row, column)) },
            });
            if (column.format === 'evidence') {
                __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
                    ...{ class: "evidence-pill" },
                    ...{ class: (__VLS_ctx.evidenceClass(row, column)) },
                });
                (__VLS_ctx.formatAuditCell(row, column));
            }
            else {
                __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
                    title: (__VLS_ctx.formatAuditCell(row, column)),
                });
                (__VLS_ctx.formatAuditCell(row, column));
            }
        }
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
            ...{ onClick: (...[$event]) => {
                    __VLS_ctx.openRowEvidence(table, row);
                } },
            ...{ class: "evidence-button" },
            type: "button",
        });
        /** @type {[typeof FolioIcon, ]} */ ;
        // @ts-ignore
        const __VLS_66 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
            name: (__VLS_ctx.rowGapCount(row) ? 'warning' : 'document'),
            size: (14),
        }));
        const __VLS_67 = __VLS_66({
            name: (__VLS_ctx.rowGapCount(row) ? 'warning' : 'document'),
            size: (14),
        }, ...__VLS_functionalComponentArgsRest(__VLS_66));
        if (__VLS_ctx.rowGapCount(row)) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
            (__VLS_ctx.rowGapCount(row));
        }
    }
    if (!__VLS_ctx.tableState(table.dataset).rows.length && !__VLS_ctx.tableState(table.dataset).loading) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.tr, __VLS_intrinsicElements.tr)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({
            colspan: (table.columns.length + 1),
            ...{ class: "empty-table-cell" },
        });
        /** @type {[typeof FolioIcon, ]} */ ;
        // @ts-ignore
        const __VLS_69 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
            name: "document",
            size: (21),
        }));
        const __VLS_70 = __VLS_69({
            name: "document",
            size: (21),
        }, ...__VLS_functionalComponentArgsRest(__VLS_69));
        (table.emptyText);
    }
    if (__VLS_ctx.tableState(table.dataset).loading) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.tr, __VLS_intrinsicElements.tr)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({
            colspan: (table.columns.length + 1),
            ...{ class: "empty-table-cell" },
        });
        /** @type {[typeof FolioIcon, ]} */ ;
        // @ts-ignore
        const __VLS_72 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
            ...{ class: "spinning" },
            name: "refresh",
            size: (19),
        }));
        const __VLS_73 = __VLS_72({
            ...{ class: "spinning" },
            name: "refresh",
            size: (19),
        }, ...__VLS_functionalComponentArgsRest(__VLS_72));
        (table.title);
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.footer, __VLS_intrinsicElements.footer)({
        ...{ class: "table-pagination" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.tableState(table.dataset).page);
    (__VLS_ctx.tableState(table.dataset).pageSize);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                __VLS_ctx.previousPage(table.dataset);
            } },
        type: "button",
        disabled: (__VLS_ctx.tableState(table.dataset).page <= 1 || __VLS_ctx.tableState(table.dataset).loading),
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_75 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: "chevron-left",
        size: (14),
    }));
    const __VLS_76 = __VLS_75({
        name: "chevron-left",
        size: (14),
    }, ...__VLS_functionalComponentArgsRest(__VLS_75));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                __VLS_ctx.nextPage(table.dataset);
            } },
        type: "button",
        disabled: (!__VLS_ctx.tableState(table.dataset).hasMore || __VLS_ctx.tableState(table.dataset).loading),
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_78 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: "chevron-right",
        size: (14),
    }));
    const __VLS_79 = __VLS_78({
        name: "chevron-right",
        size: (14),
    }, ...__VLS_functionalComponentArgsRest(__VLS_78));
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "panel export-panel" },
    'aria-labelledby': "audit-export-title",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
    ...{ class: "section-overline" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({
    id: "audit-export-title",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "export-actions" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.a, __VLS_intrinsicElements.a)({
    ...{ class: ({ disabled: !__VLS_ctx.requestId || __VLS_ctx.active }) },
    'aria-disabled': (!__VLS_ctx.requestId || __VLS_ctx.active),
    href: (__VLS_ctx.requestId && !__VLS_ctx.active ? __VLS_ctx.exportUrl('json') : undefined),
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_81 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "download",
    size: (15),
}));
const __VLS_82 = __VLS_81({
    name: "download",
    size: (15),
}, ...__VLS_functionalComponentArgsRest(__VLS_81));
__VLS_asFunctionalElement(__VLS_intrinsicElements.a, __VLS_intrinsicElements.a)({
    ...{ class: ({ disabled: !__VLS_ctx.requestId || __VLS_ctx.active }) },
    'aria-disabled': (!__VLS_ctx.requestId || __VLS_ctx.active),
    href: (__VLS_ctx.requestId && !__VLS_ctx.active ? __VLS_ctx.exportUrl('csv') : undefined),
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_84 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "download",
    size: (15),
}));
const __VLS_85 = __VLS_84({
    name: "download",
    size: (15),
}, ...__VLS_functionalComponentArgsRest(__VLS_84));
__VLS_asFunctionalElement(__VLS_intrinsicElements.a, __VLS_intrinsicElements.a)({
    ...{ class: ({ disabled: !__VLS_ctx.requestId || __VLS_ctx.active }) },
    'aria-disabled': (!__VLS_ctx.requestId || __VLS_ctx.active),
    href: (__VLS_ctx.requestId && !__VLS_ctx.active ? __VLS_ctx.exportUrl('markdown') : undefined),
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_87 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "download",
    size: (15),
}));
const __VLS_88 = __VLS_87({
    name: "download",
    size: (15),
}, ...__VLS_functionalComponentArgsRest(__VLS_87));
if (__VLS_ctx.evidenceSelection) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ onClick: (__VLS_ctx.closeEvidence) },
        ...{ class: "evidence-backdrop" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.aside, __VLS_intrinsicElements.aside)({
        ...{ class: "evidence-drawer" },
        role: "dialog",
        'aria-modal': "true",
        'aria-labelledby': "evidence-drawer-title",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "section-overline" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({
        id: "evidence-drawer-title",
    });
    (__VLS_ctx.evidenceSelection.title);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.closeEvidence) },
        type: "button",
        'aria-label': "关闭证据抽屉",
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_90 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: "x",
        size: (18),
    }));
    const __VLS_91 = __VLS_90({
        name: "x",
        size: (18),
    }, ...__VLS_functionalComponentArgsRest(__VLS_90));
    if (__VLS_ctx.evidenceSelection.row) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "evidence-row-id" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({
            ...{ class: "mono" },
        });
        (__VLS_ctx.rowIdentifier(__VLS_ctx.evidenceSelection.row, 0));
    }
    if (__VLS_ctx.drawerFields.length) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
            ...{ class: "evidence-field-grid" },
        });
        for (const [field] of __VLS_getVForSourceType((__VLS_ctx.drawerFields))) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
                key: (field.label),
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
            (field.label);
            __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
            (field.value);
        }
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "gap-list" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h3, __VLS_intrinsicElements.h3)({});
    for (const [gap, index] of __VLS_getVForSourceType((__VLS_ctx.evidenceSelection.gaps))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
            key: (`${gap.source}-${gap.code}-${index}`),
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        (gap.source);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
        (gap.code);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
        (gap.message);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (gap.state);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (gap.coverageComplete === true ? "完整" : gap.coverageComplete === false ? "不完整" : "未知");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (gap.observedAt ? __VLS_ctx.formatDateTime(gap.observedAt) : "—");
        if (gap.references.length) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
                ...{ class: "gap-references mono" },
            });
            (gap.references.join(" · "));
        }
    }
    if (!__VLS_ctx.evidenceSelection.gaps.length) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "no-gap-state" },
        });
        /** @type {[typeof FolioIcon, ]} */ ;
        // @ts-ignore
        const __VLS_93 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
            name: "success",
            size: (20),
        }));
        const __VLS_94 = __VLS_93({
            name: "success",
            size: (20),
        }, ...__VLS_functionalComponentArgsRest(__VLS_93));
    }
}
/** @type {__VLS_StyleScopedClasses['page']} */ ;
/** @type {__VLS_StyleScopedClasses['guadao-audit-page']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-hero']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-copy']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-overline']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-badges']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-run-state']} */ ;
/** @type {__VLS_StyleScopedClasses['mono']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-setup-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['panel-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['section-overline']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-form-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['money-input']} */ ;
/** @type {__VLS_StyleScopedClasses['money-input']} */ ;
/** @type {__VLS_StyleScopedClasses['account-scope']} */ ;
/** @type {__VLS_StyleScopedClasses['account-chips']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-message']} */ ;
/** @type {__VLS_StyleScopedClasses['warning-message']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-message']} */ ;
/** @type {__VLS_StyleScopedClasses['error-message']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-message']} */ ;
/** @type {__VLS_StyleScopedClasses['error-message']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-message']} */ ;
/** @type {__VLS_StyleScopedClasses['success-message']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['primary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['cancel-button']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['read-only-note']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['progress-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['panel-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['progress-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['section-overline']} */ ;
/** @type {__VLS_StyleScopedClasses['progress-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['progress-bar']} */ ;
/** @type {__VLS_StyleScopedClasses['stage-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['stage-marker']} */ ;
/** @type {__VLS_StyleScopedClasses['progress-message']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-kpi-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-kpi']} */ ;
/** @type {__VLS_StyleScopedClasses['kpi-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['verdict-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['verdict-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['verdict-copy']} */ ;
/** @type {__VLS_StyleScopedClasses['coverage-list']} */ ;
/** @type {__VLS_StyleScopedClasses['evidence-summary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-table-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['table-panel-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['section-overline']} */ ;
/** @type {__VLS_StyleScopedClasses['row-count']} */ ;
/** @type {__VLS_StyleScopedClasses['table-error']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-table-wrap']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-data-table']} */ ;
/** @type {__VLS_StyleScopedClasses['evidence-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['evidence-button']} */ ;
/** @type {__VLS_StyleScopedClasses['empty-table-cell']} */ ;
/** @type {__VLS_StyleScopedClasses['empty-table-cell']} */ ;
/** @type {__VLS_StyleScopedClasses['spinning']} */ ;
/** @type {__VLS_StyleScopedClasses['table-pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['export-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['section-overline']} */ ;
/** @type {__VLS_StyleScopedClasses['export-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['evidence-backdrop']} */ ;
/** @type {__VLS_StyleScopedClasses['evidence-drawer']} */ ;
/** @type {__VLS_StyleScopedClasses['section-overline']} */ ;
/** @type {__VLS_StyleScopedClasses['evidence-row-id']} */ ;
/** @type {__VLS_StyleScopedClasses['mono']} */ ;
/** @type {__VLS_StyleScopedClasses['evidence-field-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['gap-list']} */ ;
/** @type {__VLS_StyleScopedClasses['gap-references']} */ ;
/** @type {__VLS_StyleScopedClasses['mono']} */ ;
/** @type {__VLS_StyleScopedClasses['no-gap-state']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            FolioIcon: FolioIcon,
            AUDIT_STAGES: AUDIT_STAGES,
            AUDIT_TABLES: AUDIT_TABLES,
            auditRowKey: auditRowKey,
            formatAuditCell: formatAuditCell,
            formatDateTime: formatDateTime,
            stageState: stageState,
            form: form,
            accounts: accounts,
            presetLoading: presetLoading,
            presetWarning: presetWarning,
            run: run,
            requestId: requestId,
            requestError: requestError,
            actionNotice: actionNotice,
            actionLoading: actionLoading,
            polling: polling,
            evidenceSelection: evidenceSelection,
            tableState: tableState,
            applyPresetValues: applyPresetValues,
            startAudit: startAudit,
            cancelAudit: cancelAudit,
            retryAudit: retryAudit,
            previousPage: previousPage,
            nextPage: nextPage,
            openRowEvidence: openRowEvidence,
            openRunEvidence: openRunEvidence,
            closeEvidence: closeEvidence,
            rowGapCount: rowGapCount,
            cellClass: cellClass,
            evidenceClass: evidenceClass,
            rowIdentifier: rowIdentifier,
            active: active,
            terminal: terminal,
            verdict: verdict,
            progressPercent: progressPercent,
            statusLabel: statusLabel,
            statusTone: statusTone,
            kpis: kpis,
            openingDiscount: openingDiscount,
            accountCaption: accountCaption,
            loadedGapCount: loadedGapCount,
            drawerFields: drawerFields,
            coverageText: coverageText,
            coverageTone: coverageTone,
            exportUrl: exportUrl,
        };
    },
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
});
; /* PartiallyEnd: #4569/main.vue */
