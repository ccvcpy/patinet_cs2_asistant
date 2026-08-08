<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import FolioIcon, { type FolioIconName } from "../components/FolioIcon.vue";
import {
  AUDIT_STAGES,
  AUDIT_TABLES,
  GUADAO_AUDIT_STORAGE_KEY,
  auditCellValue,
  auditRowKey,
  buildAuditExportUrl,
  buildAuditRowsUrl,
  buildAuditRunRequest,
  defaultAuditForm,
  evidenceTone,
  extractEvidenceGaps,
  finiteNumber,
  firstValue,
  formatAuditCell,
  formatDateTime,
  formatMoney,
  formatMoneyFen,
  isActiveAuditStatus,
  normalizeAuditPreset,
  normalizeAuditRows,
  normalizeAuditRun,
  stageState,
  summaryNumber,
  validateAuditForm,
  verdictCopy,
  type AuditAccountOption,
  type AuditDataset,
  type AuditEvidenceGap,
  type AuditFormState,
  type AuditRow,
  type AuditRowsPage,
  type AuditRun,
  type AuditTableColumn,
  type AuditTableDefinition,
} from "./guadao_audit_shared";

type ActionName = "" | "start" | "cancel" | "retry";

type TableState = AuditRowsPage & {
  loading: boolean;
  error: string;
};

type EvidenceSelection = {
  table: AuditTableDefinition;
  row: AuditRow | null;
  gaps: AuditEvidenceGap[];
  title: string;
};

const PAGE_SIZE = 25;
const POLL_INTERVAL_MS = 2_000;

const initialForm = defaultAuditForm();
const form = reactive<AuditFormState>({ ...initialForm, accountIds: [] });
const presetForm = ref<AuditFormState>({ ...initialForm, accountIds: [] });
const accounts = ref<AuditAccountOption[]>([]);
const presetLoading = ref(true);
const presetWarning = ref("");

const run = ref<AuditRun | null>(null);
const requestId = ref("");
const requestError = ref("");
const actionNotice = ref("");
const actionLoading = ref<ActionName>("");
const polling = ref(false);
const evidenceSelection = ref<EvidenceSelection | null>(null);

const tableStates = reactive<Record<AuditDataset, TableState>>({
  steamSales: emptyTableState("steamSales"),
  rebuyChains: emptyTableState("rebuyChains"),
  itemConservation: emptyTableState("itemConservation"),
  wallet: emptyTableState("wallet"),
});

let pollTimer: number | undefined;
let rowsLoadedForRequest = "";

function emptyTableState(dataset: AuditDataset): TableState {
  return { dataset, rows: [], page: 1, pageSize: PAGE_SIZE, total: 0, hasMore: false, loading: false, error: "" };
}

function tableState(dataset: AuditDataset): TableState {
  return tableStates[dataset];
}

function resetTables(): void {
  for (const table of AUDIT_TABLES) {
    Object.assign(tableStates[table.dataset], emptyTableState(table.dataset));
  }
  rowsLoadedForRequest = "";
}

function payloadRecord(payload: unknown): Record<string, unknown> {
  return payload && typeof payload === "object" && !Array.isArray(payload)
    ? payload as Record<string, unknown>
    : {};
}

function payloadError(payload: unknown, fallback: string): string {
  const record = payloadRecord(payload);
  return String(record.error || record.detail || record.message || fallback);
}

async function requestJson(path: string, init?: RequestInit): Promise<{ response: Response; payload: unknown }> {
  const response = await fetch(path, {
    cache: "no-store",
    ...init,
    headers: init?.body
      ? { "Content-Type": "application/json", ...(init.headers || {}) }
      : { ...(init?.headers || {}) },
  });
  const text = await response.text();
  let payload: unknown = {};
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = { error: text.slice(0, 500) };
    }
  }
  const record = payloadRecord(payload);
  if (!response.ok || record.ok === false) {
    throw new Error(payloadError(payload, `请求失败（HTTP ${response.status}）`));
  }
  return { response, payload };
}

function applyPresetValues(): void {
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

async function loadPresets(): Promise<void> {
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
  } catch (reason) {
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
  } finally {
    presetLoading.value = false;
  }
}

function saveRequestId(value: string): void {
  requestId.value = value;
  try {
    window.localStorage.setItem(GUADAO_AUDIT_STORAGE_KEY, value);
  } catch {
    actionNotice.value = "任务已创建，但浏览器无法保存最近 requestId；本页仍可继续轮询。";
  }
}

function restoreStoredRequestId(): string {
  try {
    return window.localStorage.getItem(GUADAO_AUDIT_STORAGE_KEY)?.trim() || "";
  } catch {
    return "";
  }
}

function stopPolling(): void {
  if (pollTimer !== undefined) window.clearTimeout(pollTimer);
  pollTimer = undefined;
  polling.value = false;
}

function schedulePoll(delay = POLL_INTERVAL_MS): void {
  stopPolling();
  polling.value = true;
  pollTimer = window.setTimeout(() => void refreshRun(true), delay);
}

async function restoreLastRun(): Promise<void> {
  const stored = restoreStoredRequestId();
  if (!stored) return;
  requestId.value = stored;
  await refreshRun(false);
}

async function refreshRun(silent: boolean): Promise<void> {
  const targetId = requestId.value.trim();
  if (!targetId) return;
  if (!silent) requestError.value = "";
  try {
    const { payload } = await requestJson(`/api/guadao-audit/runs/${encodeURIComponent(targetId)}`);
    if (requestId.value !== targetId) return;
    run.value = normalizeAuditRun(payload, targetId);
    requestError.value = "";
    if (isActiveAuditStatus(run.value.status)) {
      schedulePoll();
      return;
    }
    stopPolling();
    if (rowsLoadedForRequest !== targetId) await loadAllTables();
  } catch (reason) {
    requestError.value = reason instanceof Error ? reason.message : String(reason);
    if (run.value && isActiveAuditStatus(run.value.status)) schedulePoll(5_000);
  }
}

async function startAudit(): Promise<void> {
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
    if (!accepted.requestId) throw new Error("后端已接受请求，但未返回 requestId");
    saveRequestId(accepted.requestId);
    run.value = {
      ...accepted,
      status: "queued",
      verdict: null,
      stage: null,
      progress: { done: 0, total: accepted.progress.total, percent: 0, message: "已进入只读对账队列" },
    };
    actionNotice.value = "任务已入队。HTTP 202 仅表示排队成功，页面会继续轮询真实终态。";
    schedulePoll(1_200);
  } catch (reason) {
    requestError.value = reason instanceof Error ? reason.message : String(reason);
  } finally {
    actionLoading.value = "";
  }
}

async function cancelAudit(): Promise<void> {
  if (!requestId.value || !run.value || !isActiveAuditStatus(run.value.status)) return;
  actionLoading.value = "cancel";
  requestError.value = "";
  try {
    await requestJson(`/api/guadao-audit/runs/${encodeURIComponent(requestId.value)}/cancel`, { method: "POST" });
    actionNotice.value = "已提交取消请求；等待后端确认最终状态。";
    schedulePoll(500);
  } catch (reason) {
    requestError.value = reason instanceof Error ? reason.message : String(reason);
  } finally {
    actionLoading.value = "";
  }
}

async function retryAudit(): Promise<void> {
  if (!requestId.value || !run.value || isActiveAuditStatus(run.value.status)) return;
  const previousId = requestId.value;
  actionLoading.value = "retry";
  requestError.value = "";
  actionNotice.value = "";
  try {
    const { response, payload } = await requestJson(`/api/guadao-audit/runs/${encodeURIComponent(previousId)}/retry`, { method: "POST" });
    if (![200, 202].includes(response.status)) throw new Error(`重试任务返回了意外状态 ${response.status}`);
    const accepted = normalizeAuditRun(payload);
    if (!accepted.requestId) throw new Error("重试已接受，但未返回新的 requestId");
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
    if (isActiveAuditStatus(run.value.status)) schedulePoll(1_200);
    else await loadAllTables();
  } catch (reason) {
    requestError.value = reason instanceof Error ? reason.message : String(reason);
  } finally {
    actionLoading.value = "";
  }
}

async function loadTable(dataset: AuditDataset, page = 1): Promise<void> {
  const targetId = requestId.value;
  if (!targetId) return;
  const state = tableStates[dataset];
  state.loading = true;
  state.error = "";
  try {
    const { payload } = await requestJson(buildAuditRowsUrl(targetId, dataset, page, state.pageSize));
    if (requestId.value !== targetId) return;
    Object.assign(state, normalizeAuditRows(payload, dataset, page, state.pageSize), { loading: false, error: "" });
  } catch (reason) {
    state.error = reason instanceof Error ? reason.message : String(reason);
    state.loading = false;
  }
}

async function loadAllTables(): Promise<void> {
  const targetId = requestId.value;
  if (!targetId) return;
  await Promise.all(AUDIT_TABLES.map((table) => loadTable(table.dataset, 1)));
  if (requestId.value === targetId) rowsLoadedForRequest = targetId;
}

function previousPage(dataset: AuditDataset): void {
  const state = tableStates[dataset];
  if (state.page <= 1 || state.loading) return;
  void loadTable(dataset, state.page - 1);
}

function nextPage(dataset: AuditDataset): void {
  const state = tableStates[dataset];
  if (!state.hasMore || state.loading) return;
  void loadTable(dataset, state.page + 1);
}

function openRowEvidence(table: AuditTableDefinition, row: AuditRow): void {
  evidenceSelection.value = {
    table,
    row,
    gaps: extractEvidenceGaps(row),
    title: `${table.title} · 证据详情`,
  };
}

function openRunEvidence(): void {
  if (!run.value) return;
  const loadedRowGaps = AUDIT_TABLES.flatMap((table) => (
    tableStates[table.dataset].rows.flatMap((row) => extractEvidenceGaps(row))
  ));
  evidenceSelection.value = {
    table: AUDIT_TABLES[0],
    row: null,
    gaps: [...run.value.evidenceGaps, ...loadedRowGaps],
    title: "任务级证据缺口",
  };
}

function closeEvidence(): void {
  evidenceSelection.value = null;
}

function handleEscape(event: KeyboardEvent): void {
  if (event.key === "Escape") closeEvidence();
}

function rowGapCount(row: AuditRow): number {
  return extractEvidenceGaps(row).length;
}

function cellClass(row: AuditRow, column: AuditTableColumn): Array<string | Record<string, boolean>> {
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

function evidenceClass(row: AuditRow, column: AuditTableColumn): string {
  return `evidence-${evidenceTone(auditCellValue(row, column))}`;
}

function rowIdentifier(row: AuditRow, index: number): string {
  return String(firstValue(row, ["requestId", "listingId", "purchaseId", "sourceSellOperationId", "marketHashName", "accountId"]) || `第 ${index + 1} 行`);
}

function formatCount(value: number | null): string {
  return value === null ? "—" : new Intl.NumberFormat("zh-CN").format(Math.trunc(value));
}

function kpiTone(equal: boolean | null, dangerWhenPositive = false): string {
  if (equal === null) return "neutral";
  if (dangerWhenPositive) return equal ? "danger" : "success";
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
  if (!run.value) return "尚未开始";
  return {
    queued: "排队中",
    running: "核验中",
    completed: "已完成",
    failed: "任务失败",
    cancelled: "已取消",
  }[run.value.status];
});

const statusTone = computed(() => {
  if (!run.value) return "neutral";
  if (run.value.status === "completed") return run.value.verdict === "passed" ? "success" : run.value.verdict === "failed" ? "danger" : "warning";
  if (run.value.status === "failed") return "danger";
  if (run.value.status === "cancelled") return "neutral";
  return "running";
});

const kpis = computed<Array<{ label: string; value: string; note: string; icon: FolioIconName; tone: string }>>(() => {
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
  if (wallet === null || realValue === null || wallet === 0) return "—";
  return new Intl.NumberFormat("zh-CN", { style: "percent", minimumFractionDigits: 4, maximumFractionDigits: 6 }).format(realValue / wallet);
});

const accountCaption = computed(() => {
  if (accounts.value.length) return `${accounts.value.length} 个预设 Steam 账号`;
  return "全部已配置 Steam 账号（预期 5 个）";
});

const loadedGapCount = computed(() => AUDIT_TABLES.reduce((total, table) => (
  total + tableStates[table.dataset].rows.reduce((rowTotal, row) => rowTotal + rowGapCount(row), 0)
), run.value?.evidenceGaps.length || 0));

const drawerFields = computed(() => {
  const selection = evidenceSelection.value;
  if (!selection?.row) return [];
  return selection.table.columns.map((column) => ({
    label: column.label,
    value: formatAuditCell(selection.row as AuditRow, column),
  }));
});

function coverageText(source: "steam" | "c5" | "wallet"): string {
  const coverage = run.value?.coverage || {};
  const aliases = source === "steam"
    ? ["steamHistory", "steam", "steamCoverage", "steamComplete"]
    : source === "wallet"
      ? ["steamBalance", "wallet", "walletCoverage", "walletComplete"]
      : ["c5", "c5Coverage", "c5Complete"];
  const raw = firstValue(coverage, aliases);
  if (typeof raw === "boolean") return raw ? "覆盖完整" : "覆盖不完整";
  if (raw && typeof raw === "object" && !Array.isArray(raw)) {
    const record = raw as AuditRow;
    const complete = firstValue(record, ["coverageComplete", "complete", "rangeCoverageComplete"]);
    if (complete === true) return "覆盖完整";
    if (complete === false) return "覆盖不完整";
    return String(firstValue(record, ["label", "status", "message"]) || "待核验");
  }
  return raw === undefined || raw === null || raw === "" ? "待核验" : String(raw);
}

function coverageTone(source: "steam" | "c5" | "wallet"): string {
  const text = coverageText(source);
  if (text === "覆盖完整") return "success";
  if (text === "覆盖不完整") return "warning";
  return "neutral";
}

function exportUrl(format: "json" | "csv" | "markdown"): string {
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
</script>

<template>
  <main class="page guadao-audit-page" aria-label="挂刀执行-测试工具">
    <section class="audit-hero" aria-labelledby="audit-page-title">
      <div class="hero-copy">
        <p class="audit-overline">Guadao Reconciliation</p>
        <h1 id="audit-page-title">挂刀执行器严格对账</h1>
        <p>以 Steam 官方市场历史和 C5 官方订单为证据，复核卖出、补仓去向、数量守恒、钱包余额与综合折扣。</p>
        <div class="hero-badges" aria-label="安全边界">
          <span><FolioIcon name="shield" :size="14" />只读外部系统</span>
          <span><FolioIcon name="lock" :size="14" />不写策略与交易状态</span>
          <span><FolioIcon name="document" :size="14" />缺证据不判通过</span>
        </div>
      </div>
      <div class="hero-run-state" :class="`tone-${statusTone}`">
        <span>{{ statusLabel }}</span>
        <strong>{{ run ? `${progressPercent}%` : "—" }}</strong>
        <small v-if="requestId" class="mono">{{ requestId }}</small>
        <small v-else>创建任务后自动保存 requestId</small>
      </div>
    </section>

    <section class="panel audit-setup-panel" aria-labelledby="audit-setup-title">
      <div class="panel-heading">
        <div>
          <p class="section-overline">Read-only baseline</p>
          <h2 id="audit-setup-title">基准与时间窗口</h2>
          <span>编辑仅影响本次审计请求，不会写入策略配置、库存或执行流水。</span>
        </div>
        <button class="secondary-button" type="button" :disabled="active || presetLoading" @click="applyPresetValues">
          <FolioIcon name="refresh" :size="15" />恢复内置基准
        </button>
      </div>

      <div class="audit-form-grid">
        <label>
          <span>开始时间（北京时间）</span>
          <input v-model="form.dateFrom" type="datetime-local" :disabled="active" />
          <small>内置起点：2026-07-19 15:20</small>
        </label>
        <label>
          <span>结束时间（北京时间）</span>
          <input v-model="form.dateTo" type="datetime-local" :disabled="active" />
          <small>结束时间必须晚于开始时间</small>
        </label>
        <label>
          <span>期初 Steam 账面余额</span>
          <div class="money-input"><b>¥</b><input v-model="form.openingWallet" type="number" min="0" step="0.01" :disabled="active" /></div>
          <small>内置值：¥2502.92</small>
        </label>
        <label>
          <span>期初余额真实价值</span>
          <div class="money-input"><b>¥</b><input v-model="form.openingRealValue" type="number" min="0" step="0.001" :disabled="active" /></div>
          <small>内置值：¥1755.474 · 当前折扣 {{ openingDiscount }}</small>
        </label>
      </div>

      <div class="account-scope">
        <div>
          <span>账号范围</span>
          <strong>{{ accountCaption }}</strong>
        </div>
        <div class="account-chips">
          <span v-for="account in accounts" :key="account.id" :title="account.steamId || account.id">
            <FolioIcon name="account" :size="13" />{{ account.label }}
          </span>
          <span v-if="!accounts.length"><FolioIcon name="account" :size="13" />all</span>
        </div>
      </div>

      <p v-if="presetWarning" class="audit-message warning-message">
        <FolioIcon name="warning" :size="16" />{{ presetWarning }}
      </p>
      <p v-if="requestError" class="audit-message error-message" role="alert">
        <FolioIcon name="error" :size="16" />{{ requestError }}
      </p>
      <p v-else-if="run?.error" class="audit-message error-message" role="alert">
        <FolioIcon name="error" :size="16" />
        {{ run.error.source }} / {{ run.error.code }}：{{ run.error.message }}
        <span>{{ run.error.retryable ? "可重试" : "不可自动重试" }}</span>
      </p>
      <p v-else-if="actionNotice" class="audit-message success-message" aria-live="polite">
        <FolioIcon name="info" :size="16" />{{ actionNotice }}
      </p>

      <div class="audit-actions">
        <button class="primary-button" type="button" :disabled="active || actionLoading !== '' || presetLoading" @click="startAudit">
          <FolioIcon name="play" :size="16" />{{ actionLoading === "start" ? "正在提交" : "开始严格对账" }}
        </button>
        <button class="secondary-button cancel-button" type="button" :disabled="!active || actionLoading !== ''" @click="cancelAudit">
          <FolioIcon name="pause" :size="16" />{{ actionLoading === "cancel" ? "正在提交" : "取消任务" }}
        </button>
        <button class="secondary-button" type="button" :disabled="!terminal || actionLoading !== ''" @click="retryAudit">
          <FolioIcon name="refresh" :size="16" />{{ actionLoading === "retry" ? "正在创建" : "重试任务" }}
        </button>
        <span class="read-only-note"><FolioIcon name="shield" :size="15" />所有动作仅管理审计任务</span>
      </div>
    </section>

    <section class="panel progress-panel" aria-labelledby="audit-progress-title">
      <div class="panel-heading progress-heading">
        <div>
          <p class="section-overline">Evidence pipeline</p>
          <h2 id="audit-progress-title">五阶段证据进度</h2>
        </div>
        <div class="progress-summary">
          <span>{{ statusLabel }}</span>
          <strong>{{ progressPercent }}%</strong>
        </div>
      </div>
      <div class="progress-bar" aria-hidden="true"><span :style="{ width: `${progressPercent}%` }" /></div>
      <ol class="stage-grid">
        <li v-for="stage in AUDIT_STAGES" :key="stage.key" :class="`stage-${stageState(stage.key, run)}`">
          <span class="stage-marker">
            <FolioIcon v-if="stageState(stage.key, run) === 'completed'" name="success" :size="16" />
            <FolioIcon v-else-if="stageState(stage.key, run) === 'failed'" name="error" :size="16" />
            <FolioIcon v-else-if="stageState(stage.key, run) === 'cancelled'" name="pause" :size="15" />
            <b v-else>{{ stage.index + 1 }}</b>
          </span>
          <strong>{{ stage.label }}</strong>
          <small>{{ stage.hint }}</small>
        </li>
      </ol>
      <p class="progress-message">
        <FolioIcon :name="polling ? 'refresh' : 'clock'" :class="{ spinning: polling }" :size="15" />
        {{ run?.progress.message || (run ? "等待后端更新阶段进度" : "开始任务后显示真实采集进度") }}
        <span v-if="run?.progress.total">{{ run.progress.done }}/{{ run.progress.total }}</span>
      </p>
    </section>

    <section class="audit-kpi-grid" aria-label="核心对账指标">
      <article v-for="kpi in kpis" :key="kpi.label" class="audit-kpi" :class="`kpi-${kpi.tone}`">
        <span class="kpi-icon"><FolioIcon :name="kpi.icon" :size="19" /></span>
        <div><span>{{ kpi.label }}</span><strong>{{ kpi.value }}</strong><small>{{ kpi.note }}</small></div>
      </article>
    </section>

    <section class="verdict-panel" :class="`verdict-${run?.verdict || 'waiting'}`" aria-live="polite">
      <div class="verdict-icon">
        <FolioIcon :name="run?.verdict === 'passed' ? 'success' : run?.verdict === 'failed' ? 'error' : run?.verdict === 'inconclusive' ? 'warning' : 'circle-dashed'" :size="25" />
      </div>
      <div class="verdict-copy">
        <span>{{ verdict.label }}</span>
        <h2>{{ verdict.title }}</h2>
        <p>{{ verdict.description }}</p>
        <small v-if="run?.updatedAt">最后更新：{{ formatDateTime(run.updatedAt) }}</small>
      </div>
      <div class="coverage-list">
        <span :class="`coverage-${coverageTone('steam')}`">Steam：{{ coverageText("steam") }}</span>
        <span :class="`coverage-${coverageTone('c5')}`">C5：{{ coverageText("c5") }}</span>
        <span :class="`coverage-${coverageTone('wallet')}`">钱包：{{ coverageText("wallet") }}</span>
      </div>
      <button v-if="run && loadedGapCount" class="evidence-summary-button" type="button" @click="openRunEvidence">
        <FolioIcon name="warning" :size="15" />{{ loadedGapCount }} 个已加载证据缺口
      </button>
    </section>

    <section v-for="table in AUDIT_TABLES" :key="table.dataset" class="panel audit-table-panel" :aria-labelledby="`${table.dataset}-title`">
      <div class="table-panel-heading">
        <div>
          <p class="section-overline">{{ table.dataset }}</p>
          <h2 :id="`${table.dataset}-title`">{{ table.title }}</h2>
          <span>{{ table.description }}</span>
        </div>
        <span class="row-count">{{ tableState(table.dataset).total }} 条</span>
      </div>

      <p v-if="tableState(table.dataset).error" class="table-error">
        <FolioIcon name="error" :size="15" />{{ tableState(table.dataset).error }}
      </p>

      <div class="audit-table-wrap" :class="{ 'is-loading': tableState(table.dataset).loading }">
        <table class="audit-data-table">
          <thead>
            <tr>
              <th v-for="column in table.columns" :key="column.key">{{ column.label }}</th>
              <th>证据详情</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, index) in tableState(table.dataset).rows" :key="auditRowKey(row, table.dataset, index)">
              <td v-for="column in table.columns" :key="column.key" :class="cellClass(row, column)">
                <span v-if="column.format === 'evidence'" class="evidence-pill" :class="evidenceClass(row, column)">
                  {{ formatAuditCell(row, column) }}
                </span>
                <span v-else :title="formatAuditCell(row, column)">{{ formatAuditCell(row, column) }}</span>
              </td>
              <td>
                <button class="evidence-button" type="button" @click="openRowEvidence(table, row)">
                  <FolioIcon :name="rowGapCount(row) ? 'warning' : 'document'" :size="14" />
                  查看证据<span v-if="rowGapCount(row)">{{ rowGapCount(row) }}</span>
                </button>
              </td>
            </tr>
            <tr v-if="!tableState(table.dataset).rows.length && !tableState(table.dataset).loading">
              <td :colspan="table.columns.length + 1" class="empty-table-cell">
                <FolioIcon name="document" :size="21" />{{ table.emptyText }}
              </td>
            </tr>
            <tr v-if="tableState(table.dataset).loading">
              <td :colspan="table.columns.length + 1" class="empty-table-cell">
                <FolioIcon class="spinning" name="refresh" :size="19" />正在读取 {{ table.title }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <footer class="table-pagination">
        <span>第 {{ tableState(table.dataset).page }} 页 · 每页 {{ tableState(table.dataset).pageSize }} 条</span>
        <div>
          <button type="button" :disabled="tableState(table.dataset).page <= 1 || tableState(table.dataset).loading" @click="previousPage(table.dataset)">
            <FolioIcon name="chevron-left" :size="14" />上一页
          </button>
          <button type="button" :disabled="!tableState(table.dataset).hasMore || tableState(table.dataset).loading" @click="nextPage(table.dataset)">
            下一页<FolioIcon name="chevron-right" :size="14" />
          </button>
        </div>
      </footer>
    </section>

    <section class="panel export-panel" aria-labelledby="audit-export-title">
      <div>
        <p class="section-overline">Immutable export</p>
        <h2 id="audit-export-title">按 requestId 导出完整证据</h2>
        <span>导出由服务端生成全量数据，不受当前表格分页影响；不提供 Excel。</span>
      </div>
      <div class="export-actions">
        <a :class="{ disabled: !requestId || active }" :aria-disabled="!requestId || active" :href="requestId && !active ? exportUrl('json') : undefined">
          <FolioIcon name="download" :size="15" />JSON
        </a>
        <a :class="{ disabled: !requestId || active }" :aria-disabled="!requestId || active" :href="requestId && !active ? exportUrl('csv') : undefined">
          <FolioIcon name="download" :size="15" />CSV
        </a>
        <a :class="{ disabled: !requestId || active }" :aria-disabled="!requestId || active" :href="requestId && !active ? exportUrl('markdown') : undefined">
          <FolioIcon name="download" :size="15" />Markdown
        </a>
      </div>
    </section>

    <div v-if="evidenceSelection" class="evidence-backdrop" @click.self="closeEvidence">
      <aside class="evidence-drawer" role="dialog" aria-modal="true" aria-labelledby="evidence-drawer-title">
        <header>
          <div>
            <p class="section-overline">Evidence gaps</p>
            <h2 id="evidence-drawer-title">{{ evidenceSelection.title }}</h2>
          </div>
          <button type="button" aria-label="关闭证据抽屉" @click="closeEvidence"><FolioIcon name="x" :size="18" /></button>
        </header>

        <div v-if="evidenceSelection.row" class="evidence-row-id">
          <span>记录标识</span><strong class="mono">{{ rowIdentifier(evidenceSelection.row, 0) }}</strong>
        </div>

        <section v-if="drawerFields.length" class="evidence-field-grid">
          <div v-for="field in drawerFields" :key="field.label"><span>{{ field.label }}</span><strong>{{ field.value }}</strong></div>
        </section>

        <section class="gap-list">
          <h3>证据缺口</h3>
          <article v-for="(gap, index) in evidenceSelection.gaps" :key="`${gap.source}-${gap.code}-${index}`">
            <div><span>{{ gap.source }}</span><b>{{ gap.code }}</b></div>
            <p>{{ gap.message }}</p>
            <dl>
              <div><dt>状态</dt><dd>{{ gap.state }}</dd></div>
              <div><dt>区间覆盖</dt><dd>{{ gap.coverageComplete === true ? "完整" : gap.coverageComplete === false ? "不完整" : "未知" }}</dd></div>
              <div><dt>观测时间</dt><dd>{{ gap.observedAt ? formatDateTime(gap.observedAt) : "—" }}</dd></div>
            </dl>
            <p v-if="gap.references.length" class="gap-references mono">{{ gap.references.join(" · ") }}</p>
          </article>
          <div v-if="!evidenceSelection.gaps.length" class="no-gap-state">
            <FolioIcon name="success" :size="20" />该记录没有声明证据缺口；这里只展示服务端返回的安全证据摘要。
          </div>
        </section>
      </aside>
    </div>
  </main>
</template>

<style scoped>
.guadao-audit-page {
  width: min(1480px, calc(100vw - 48px));
  gap: 16px;
  padding-top: 18px;
  color: var(--folio-ink);
  font-family: var(--ops-font, "Segoe UI Variable Text", "Microsoft YaHei UI", "PingFang SC", system-ui, sans-serif);
}

.audit-hero {
  min-height: 190px;
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 32px;
  padding: 28px 30px;
  border: 1px solid rgba(255, 255, 255, .1);
  border-radius: 20px;
  color: #fff;
  background:
    radial-gradient(circle at 86% 6%, rgba(167, 211, 184, .2), transparent 31%),
    radial-gradient(circle at 8% 100%, rgba(139, 200, 165, .13), transparent 34%),
    #173f31;
  box-shadow: 0 24px 64px rgba(20, 59, 46, .16);
}

.hero-copy { max-width: 850px; }
.audit-overline, .section-overline { margin: 0; color: var(--folio-green); font-size: 10px; font-weight: 800; letter-spacing: .13em; text-transform: uppercase; }
.audit-hero .audit-overline { color: #a7d3b8; }
.audit-hero h1 { margin: 7px 0 9px; color: #fff; font-size: clamp(30px, 3.4vw, 43px); line-height: 1.08; letter-spacing: -.05em; }
.audit-hero p:not(.audit-overline) { max-width: 760px; margin: 0; color: rgba(255, 255, 255, .66); font-size: 13px; line-height: 1.75; }
.hero-badges { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 19px; }
.hero-badges span { min-height: 29px; display: inline-flex; align-items: center; gap: 6px; padding: 5px 9px; border: 1px solid rgba(255, 255, 255, .12); border-radius: 8px; color: rgba(255, 255, 255, .78); background: rgba(255, 255, 255, .055); font-size: 10px; font-weight: 700; }

.hero-run-state { min-width: 190px; display: grid; gap: 4px; align-self: stretch; align-content: center; padding: 18px; border: 1px solid rgba(255, 255, 255, .13); border-radius: 15px; background: rgba(255, 255, 255, .075); }
.hero-run-state span { color: rgba(255, 255, 255, .67); font-size: 11px; font-weight: 700; }
.hero-run-state strong { font-size: 33px; letter-spacing: -.045em; }
.hero-run-state small { max-width: 240px; overflow: hidden; color: rgba(255, 255, 255, .55); font-size: 9px; text-overflow: ellipsis; white-space: nowrap; }
.hero-run-state.tone-success strong { color: #bfe5c9; }
.hero-run-state.tone-warning strong { color: #f0d59b; }
.hero-run-state.tone-danger strong { color: #f0b6af; }

.audit-setup-panel, .progress-panel, .audit-table-panel, .export-panel { border-radius: 17px; box-shadow: 0 6px 26px rgba(34, 49, 41, .035); }
.panel-heading, .table-panel-heading, .export-panel { display: flex; align-items: center; justify-content: space-between; gap: 20px; }
.panel-heading h2, .table-panel-heading h2, .export-panel h2 { margin: 4px 0 3px; font-size: 18px; letter-spacing: -.025em; }
.panel-heading > div > span, .table-panel-heading > div > span, .export-panel > div > span { color: var(--folio-muted); font-size: 11px; line-height: 1.6; }
.panel-heading .secondary-button, .audit-actions button { display: inline-flex; align-items: center; justify-content: center; gap: 7px; }

.audit-form-grid { display: grid; grid-template-columns: repeat(4, minmax(170px, 1fr)); gap: 12px; margin-top: 18px; }
.audit-form-grid label { min-width: 0; display: grid; gap: 6px; }
.audit-form-grid label > span, .account-scope > div:first-child > span { color: #5f6a63; font-size: 10px; font-weight: 700; }
.audit-form-grid input { width: 100%; min-height: 42px; border: 1px solid #dfe4df; border-radius: 11px; padding: 9px 11px; color: var(--folio-ink); background: #fff; }
.audit-form-grid input:focus { border-color: var(--folio-green); box-shadow: 0 0 0 3px rgba(35, 106, 76, .1); }
.audit-form-grid input:disabled { color: #68726c; background: #f1f3f0; }
.audit-form-grid small { color: #89918c; font-size: 9px; }
.money-input { position: relative; }
.money-input b { position: absolute; z-index: 1; top: 50%; left: 12px; color: var(--folio-green); font-size: 12px; transform: translateY(-50%); }
.money-input input { padding-left: 28px; }

.account-scope { display: flex; align-items: center; justify-content: space-between; gap: 18px; margin-top: 16px; padding: 11px 12px; border: 1px solid var(--folio-line); border-radius: 12px; background: var(--folio-surface-soft); }
.account-scope > div:first-child { display: grid; gap: 2px; }
.account-scope strong { font-size: 12px; }
.account-chips { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; }
.account-chips span { display: inline-flex; align-items: center; gap: 5px; padding: 5px 8px; border: 1px solid #dbe5dd; border-radius: 8px; color: var(--folio-green-dark); background: #fff; font-size: 9px; font-weight: 700; }

.audit-message { min-height: 40px; display: flex; align-items: center; gap: 8px; margin: 12px 0 0; padding: 9px 11px; border: 1px solid; border-radius: 10px; font-size: 11px; line-height: 1.5; }
.success-message { border-color: #cfe2d5; color: var(--folio-green); background: var(--folio-green-soft); }
.warning-message { border-color: #e8d8b6; color: var(--folio-amber); background: var(--folio-amber-soft); }
.error-message { border-color: #ebceca; color: var(--folio-red); background: var(--folio-red-soft); }
.audit-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 14px; }
.audit-actions button { min-height: 40px; }
.cancel-button:not(:disabled) { border-color: #ead2cf; color: var(--folio-red); background: var(--folio-red-soft); }
.read-only-note { display: inline-flex; align-items: center; gap: 6px; margin-left: auto; color: var(--folio-muted); font-size: 10px; }

.progress-heading { align-items: flex-end; }
.progress-summary { display: flex; align-items: baseline; gap: 8px; color: var(--folio-muted); }
.progress-summary span { font-size: 10px; font-weight: 700; }
.progress-summary strong { color: var(--folio-green); font-size: 23px; letter-spacing: -.035em; }
.progress-bar { height: 5px; overflow: hidden; margin: 14px 0 17px; border-radius: 999px; background: #e9ede9; }
.progress-bar span { height: 100%; display: block; border-radius: inherit; background: linear-gradient(90deg, var(--folio-green), #5a9c75); transition: width .22s ease; }
.stage-grid { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px; margin: 0; padding: 0; list-style: none; }
.stage-grid li { min-width: 0; min-height: 100px; display: grid; align-content: start; gap: 5px; padding: 11px; border: 1px solid var(--folio-line); border-radius: 12px; background: #fafbfa; }
.stage-marker { width: 27px; height: 27px; display: grid; place-items: center; border: 1px solid #dbe1dc; border-radius: 8px; color: #7f8982; background: #fff; }
.stage-marker b { font-size: 10px; }
.stage-grid li > strong { font-size: 11px; }
.stage-grid li > small { color: var(--folio-muted); font-size: 9px; line-height: 1.45; }
.stage-grid .stage-current { border-color: #b9d9c3; background: #f1f8f3; box-shadow: inset 0 0 0 1px rgba(35, 106, 76, .05); }
.stage-grid .stage-current .stage-marker { border-color: var(--folio-green); color: #fff; background: var(--folio-green); }
.stage-grid .stage-completed .stage-marker { border-color: #c6dfce; color: var(--folio-green); background: var(--folio-green-soft); }
.stage-grid .stage-failed .stage-marker { border-color: #ebc9c5; color: var(--folio-red); background: var(--folio-red-soft); }
.stage-grid .stage-cancelled .stage-marker { color: #6f7872; background: #ecefec; }
.progress-message { display: flex; align-items: center; gap: 7px; margin: 13px 1px 0; color: var(--folio-muted); font-size: 10px; }
.progress-message > span { margin-left: auto; font-variant-numeric: tabular-nums; }

.audit-kpi-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.audit-kpi { min-height: 116px; display: flex; align-items: flex-start; gap: 12px; padding: 16px; border: 1px solid var(--folio-line); border-radius: 15px; background: linear-gradient(145deg, #fff, #f8faf8); box-shadow: 0 6px 24px rgba(34, 49, 41, .035); }
.kpi-icon { width: 38px; height: 38px; display: grid; flex: 0 0 auto; place-items: center; border-radius: 11px; color: var(--folio-green); background: var(--folio-green-soft); }
.audit-kpi > div { min-width: 0; display: grid; gap: 2px; }
.audit-kpi > div > span { color: var(--folio-muted); font-size: 10px; font-weight: 700; }
.audit-kpi strong { overflow: hidden; color: var(--folio-ink); font-size: 21px; letter-spacing: -.035em; text-overflow: ellipsis; white-space: nowrap; }
.audit-kpi small { color: #89928c; font-size: 9px; }
.audit-kpi.kpi-success { border-color: #cee2d5; }
.audit-kpi.kpi-danger { border-color: #ebceca; background: linear-gradient(145deg, #fff, #fdf6f5); }
.audit-kpi.kpi-danger .kpi-icon { color: var(--folio-red); background: var(--folio-red-soft); }

.verdict-panel { display: grid; grid-template-columns: auto minmax(0, 1fr) auto auto; align-items: center; gap: 16px; padding: 18px 20px; border: 1px solid var(--folio-line); border-radius: 16px; background: #fff; box-shadow: 0 6px 26px rgba(34, 49, 41, .035); }
.verdict-icon { width: 48px; height: 48px; display: grid; place-items: center; border-radius: 14px; color: #728078; background: #eef1ee; }
.verdict-copy > span { color: var(--folio-muted); font-size: 9px; font-weight: 800; letter-spacing: .12em; }
.verdict-copy h2 { margin: 2px 0; font-size: 17px; }
.verdict-copy p { margin: 0; color: var(--folio-muted); font-size: 10px; line-height: 1.55; }
.verdict-copy small { display: block; margin-top: 4px; color: #929a95; font-size: 9px; }
.verdict-passed { border-color: #c8dfcf; background: #f5faf6; }
.verdict-passed .verdict-icon { color: var(--folio-green); background: var(--folio-green-soft); }
.verdict-failed { border-color: #e8c9c5; background: #fff8f7; }
.verdict-failed .verdict-icon { color: var(--folio-red); background: var(--folio-red-soft); }
.verdict-inconclusive { border-color: #e6d5b2; background: #fdf9ef; }
.verdict-inconclusive .verdict-icon { color: var(--folio-amber); background: var(--folio-amber-soft); }
.coverage-list { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; }
.coverage-list span { padding: 5px 8px; border-radius: 8px; font-size: 9px; font-weight: 700; }
.coverage-success { color: var(--folio-green); background: var(--folio-green-soft); }
.coverage-warning { color: var(--folio-amber); background: var(--folio-amber-soft); }
.coverage-neutral { color: #6f7872; background: #edf0ed; }
.evidence-summary-button { min-height: 34px; display: inline-flex; align-items: center; gap: 6px; border: 1px solid #e5d1aa; border-radius: 9px; padding: 6px 9px; color: var(--folio-amber); background: var(--folio-amber-soft); font-size: 9px; font-weight: 700; }

.audit-table-panel { padding: 0; overflow: hidden; }
.table-panel-heading { padding: 16px 18px 13px; }
.row-count { flex: 0 0 auto; padding: 5px 8px; border-radius: 8px; color: var(--folio-green); background: var(--folio-green-soft); font-size: 10px; font-weight: 800; }
.table-error { display: flex; align-items: center; gap: 6px; margin: 0 18px 11px; padding: 8px 9px; border: 1px solid #ebceca; border-radius: 8px; color: var(--folio-red); background: var(--folio-red-soft); font-size: 10px; }
.audit-table-wrap { min-width: 0; overflow: auto; border-top: 1px solid var(--folio-line); border-bottom: 1px solid var(--folio-line); }
.audit-table-wrap.is-loading { opacity: .78; }
.audit-data-table { width: 100%; min-width: 1180px; border-collapse: collapse; font-size: 10px; }
.audit-data-table th, .audit-data-table td { max-width: 220px; padding: 10px 11px; border-bottom: 1px solid #edf0ed; text-align: left; vertical-align: middle; }
.audit-data-table th { position: sticky; top: 0; z-index: 1; color: #59645d; background: #f5f8f5; font-size: 9px; font-weight: 800; white-space: nowrap; }
.audit-data-table td > span:not(.evidence-pill) { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.audit-data-table tbody tr:hover { background: #f8fbf9; }
.cell-strong { color: var(--folio-green-deep); font-weight: 700; }
.cell-moneyFen, .cell-money, .cell-integer, .cell-percent { font-variant-numeric: tabular-nums; white-space: nowrap; }
.cell-difference { color: var(--folio-red); font-weight: 800; background: rgba(249, 236, 234, .45); }
.evidence-pill { display: inline-flex; padding: 4px 7px; border-radius: 999px; font-size: 8px; font-weight: 800; white-space: nowrap; }
.evidence-success { color: var(--folio-green); background: var(--folio-green-soft); }
.evidence-danger { color: var(--folio-red); background: var(--folio-red-soft); }
.evidence-warning { color: var(--folio-amber); background: var(--folio-amber-soft); }
.evidence-neutral { color: #6f7872; background: #edf0ed; }
.evidence-button { min-height: 30px; display: inline-flex; align-items: center; gap: 5px; border: 1px solid #dfe5df; border-radius: 8px; padding: 5px 7px; color: var(--folio-green-dark); background: #fff; font-size: 9px; font-weight: 700; white-space: nowrap; }
.evidence-button:hover { border-color: #bed4c5; background: var(--folio-green-soft); }
.evidence-button span { min-width: 16px; height: 16px; display: inline-grid; place-items: center; border-radius: 999px; color: var(--folio-amber); background: var(--folio-amber-soft); font-size: 8px; }
.empty-table-cell { height: 96px; color: var(--folio-muted); text-align: center !important; }
.empty-table-cell svg { margin-right: 7px; vertical-align: middle; }
.table-pagination { min-height: 48px; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 8px 13px; }
.table-pagination > span { color: var(--folio-muted); font-size: 9px; }
.table-pagination > div { display: flex; gap: 6px; }
.table-pagination button { min-height: 31px; display: inline-flex; align-items: center; gap: 4px; border: 1px solid #dfe5df; border-radius: 8px; padding: 5px 8px; color: #405048; background: #f7f9f7; font-size: 9px; font-weight: 700; }
.table-pagination button:hover:not(:disabled) { color: var(--folio-green-dark); border-color: #c9d8cd; background: var(--folio-green-soft); }

.export-panel { padding: 17px 18px; }
.export-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 7px; }
.export-actions a { min-height: 36px; display: inline-flex; align-items: center; gap: 6px; border: 1px solid #dfe5df; border-radius: 9px; padding: 7px 10px; color: var(--folio-green-dark); background: #f2f5f2; font-size: 10px; font-weight: 700; text-decoration: none; }
.export-actions a:hover:not(.disabled) { border-color: #bdd3c4; background: var(--folio-green-soft); transform: translateY(-1px); }
.export-actions a.disabled { pointer-events: none; opacity: .45; }

.evidence-backdrop { position: fixed; z-index: 100; inset: 0; display: flex; justify-content: flex-end; background: rgba(17, 31, 24, .24); backdrop-filter: blur(2px); }
.evidence-drawer { width: min(560px, 94vw); height: 100%; overflow-y: auto; padding: 20px; color: var(--folio-ink); background: var(--folio-bg); box-shadow: -18px 0 54px rgba(34, 49, 41, .12); animation: drawer-enter .18s ease both; }
@keyframes drawer-enter { from { opacity: 0; transform: translateX(14px); } }
.evidence-drawer > header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; padding-bottom: 15px; border-bottom: 1px solid var(--folio-line); }
.evidence-drawer h2 { margin: 4px 0 0; font-size: 20px; letter-spacing: -.03em; }
.evidence-drawer > header button { width: 36px; height: 36px; display: grid; place-items: center; border: 1px solid var(--folio-line); border-radius: 10px; color: #5f6a63; background: #fff; }
.evidence-row-id { display: grid; gap: 3px; margin-top: 14px; padding: 11px; border: 1px solid #cee2d5; border-radius: 11px; background: var(--folio-green-soft); }
.evidence-row-id span { color: var(--folio-muted); font-size: 9px; }
.evidence-row-id strong { overflow-wrap: anywhere; font-size: 11px; }
.evidence-field-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin-top: 12px; }
.evidence-field-grid > div { min-width: 0; display: grid; gap: 3px; padding: 9px; border: 1px solid var(--folio-line); border-radius: 10px; background: #fff; }
.evidence-field-grid span { color: var(--folio-muted); font-size: 8px; }
.evidence-field-grid strong { overflow-wrap: anywhere; font-size: 10px; }
.gap-list { margin-top: 18px; }
.gap-list h3 { margin: 0 0 9px; font-size: 14px; }
.gap-list article { margin-bottom: 9px; padding: 12px; border: 1px solid #e7d4af; border-radius: 12px; background: #fffaf0; }
.gap-list article > div:first-child { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.gap-list article > div:first-child span { color: var(--folio-amber); font-size: 9px; font-weight: 800; text-transform: uppercase; }
.gap-list article > div:first-child b { color: #795b23; font-size: 9px; }
.gap-list article > p { margin: 7px 0; font-size: 11px; line-height: 1.6; }
.gap-list dl { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; margin: 0; }
.gap-list dl > div { display: grid; gap: 2px; }
.gap-list dt { color: var(--folio-muted); font-size: 8px; }
.gap-list dd { margin: 0; font-size: 9px; font-weight: 700; }
.gap-references { color: #6c5a36; overflow-wrap: anywhere; }
.no-gap-state { min-height: 110px; display: grid; place-items: center; align-content: center; gap: 7px; padding: 18px; border: 1px dashed #cddbd1; border-radius: 12px; color: var(--folio-green); background: #f6faf7; font-size: 10px; text-align: center; }

.mono { font-family: "Cascadia Mono", "SFMono-Regular", Consolas, monospace; }
.spinning { animation: spin .9s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

@media (max-width: 1100px) {
  .audit-form-grid, .audit-kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .stage-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .verdict-panel { grid-template-columns: auto minmax(0, 1fr); }
  .coverage-list, .evidence-summary-button { justify-self: start; grid-column: 2; }
}

@media (max-width: 760px) {
  .guadao-audit-page { width: min(100vw - 20px, 1480px); padding-top: 10px; }
  .audit-hero { min-height: 0; align-items: stretch; flex-direction: column; padding: 22px 18px; border-radius: 17px; }
  .hero-run-state { min-width: 0; }
  .panel-heading, .table-panel-heading, .export-panel, .account-scope { align-items: stretch; flex-direction: column; }
  .audit-form-grid, .audit-kpi-grid, .stage-grid { grid-template-columns: 1fr; }
  .account-chips { justify-content: flex-start; }
  .read-only-note { width: 100%; margin-left: 0; }
  .audit-actions button { flex: 1 1 140px; }
  .verdict-panel { grid-template-columns: 1fr; }
  .coverage-list, .evidence-summary-button { grid-column: auto; justify-self: stretch; justify-content: flex-start; }
  .table-pagination { align-items: stretch; flex-direction: column; }
  .table-pagination > div, .table-pagination button { flex: 1; }
  .export-actions { justify-content: flex-start; }
  .evidence-field-grid, .gap-list dl { grid-template-columns: 1fr; }
}
</style>
