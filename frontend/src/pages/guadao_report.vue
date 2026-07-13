<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import FolioDateTimeRange from "../components/FolioDateTimeRange.vue";

type Summary = {
  count: number;
  steamGross: number;
  steamNet: number;
  cash: number;
  totalDiscountRatio: number | null;
};

type ItemSummary = Summary & { marketHashName: string };

type DetailRow = {
  completedAtLocal: string;
  marketHashName: string;
  steamGross: number;
  steamNet: number;
  cash: number;
  totalDiscountRatio: number | null;
  assetId: string;
  listingId: string;
};

type GuadaoReport = {
  startLocal: string;
  endLocal: string;
  summary: Summary;
  items: ItemSummary[];
  details: DetailRow[];
  detailsIncluded: boolean;
  steamSoldReconciliation: {
    closed: Summary;
    unclosed: Summary;
    ignored: Summary;
  };
  steamSoldMissingSoldAt: { summary: Summary };
  closedFromSellOutsideRange: Summary;
  historicalUnclosedBeforeRange: Summary;
};

type ReconciliationRow = {
  key: string;
  label: string;
  tone: "success" | "warning" | "neutral";
  summary: Summary;
  historical: boolean;
};

function startOfMonth(): Date {
  const now = new Date();
  return new Date(now.getFullYear(), now.getMonth(), 1, 0, 0, 0, 0);
}

const dateRange = ref<Date[]>([startOfMonth(), new Date()]);
const itemName = ref("");
const includeDetails = ref(false);
const loading = ref(false);
const error = ref("");
const report = ref<GuadaoReport | null>(null);
const showTimeExplanation = ref(false);
const itemSortDescending = ref(true);
const detailPage = ref(1);
const detailPageSize = 50;

const emptySummary = (): Summary => ({
  count: 0,
  steamGross: 0,
  steamNet: 0,
  cash: 0,
  totalDiscountRatio: null,
});

const reconciliationRows = computed<ReconciliationRow[]>(() => {
  if (!report.value) return [];
  return [
    { key: "closed", label: "本期已闭环", tone: "success", summary: report.value.steamSoldReconciliation.closed, historical: false },
    { key: "unclosed", label: "本期未闭环", tone: "warning", summary: report.value.steamSoldReconciliation.unclosed, historical: false },
    { key: "history-closed", label: "本期历史补仓", tone: "neutral", summary: report.value.closedFromSellOutsideRange, historical: true },
    { key: "history-unclosed", label: "本期历史未闭环", tone: "neutral", summary: report.value.historicalUnclosedBeforeRange, historical: true },
  ];
});

const currentWalletSummary = computed<Summary>(() => {
  if (!report.value) return emptySummary();
  const closed = report.value.steamSoldReconciliation.closed;
  const unclosed = report.value.steamSoldReconciliation.unclosed;
  const steamNet = closed.steamNet + unclosed.steamNet;
  const cash = closed.cash + unclosed.cash;
  return {
    count: closed.count + unclosed.count,
    steamGross: closed.steamGross + unclosed.steamGross,
    steamNet,
    cash,
    totalDiscountRatio: steamNet > 0 ? cash / steamNet : null,
  };
});

const sortedItems = computed(() => {
  const items = [...(report.value?.items ?? [])];
  return items.sort((a, b) => itemSortDescending.value ? b.cash - a.cash : a.cash - b.cash);
});

const missingOfficialTime = computed(() => report.value?.steamSoldMissingSoldAt.summary ?? emptySummary());
const detailPageCount = computed(() => Math.max(1, Math.ceil((report.value?.details.length ?? 0) / detailPageSize)));
const visibleDetails = computed(() => {
  const start = (detailPage.value - 1) * detailPageSize;
  return (report.value?.details ?? []).slice(start, start + detailPageSize);
});
const apiState = computed(() => {
  if (loading.value && !report.value) return { label: "正在连接报表 API", tone: "pending" };
  if (error.value) return { label: "报表 API 异常", tone: "danger" };
  if (report.value) return { label: "报表 API 已连接", tone: "success" };
  return { label: "等待查询", tone: "pending" };
});

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

function toLocalInput(value: Date): string {
  return `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(value.getDate())}T${pad(value.getHours())}:${pad(value.getMinutes())}`;
}

function formatMoney(value: number): string {
  return `CNY ${Number(value || 0).toFixed(2)}`;
}

function formatPct(value: number | null): string {
  return value == null ? "—" : `${(value * 100).toFixed(2)}%`;
}

function formatRangeLabel(): string {
  if (!report.value) return "尚未查询";
  return `${report.value.startLocal.replace("T", " ").slice(0, 16)} 至 ${report.value.endLocal.replace("T", " ").slice(0, 16)}（北京时间）`;
}

async function queryReport(): Promise<void> {
  if (dateRange.value.length !== 2 || !dateRange.value[0] || !dateRange.value[1]) {
    error.value = "请选择完整的开始和结束时间";
    return;
  }
  loading.value = true;
  error.value = "";
  try {
    const response = await fetch("/api/guadao-report/query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        dateFrom: toLocalInput(dateRange.value[0]),
        dateTo: toLocalInput(dateRange.value[1]),
        marketHashName: itemName.value.trim() || null,
        includeDetails: includeDetails.value,
      }),
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    report.value = payload.report as GuadaoReport;
    detailPage.value = 1;
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "挂刀报表查询失败";
  } finally {
    loading.value = false;
  }
}

onMounted(queryReport);
</script>

<template>
  <main class="page guadao-report-page">
    <header class="page-header report-heading">
      <div>
        <p class="eyebrow">Guadao Report</p>
        <h1>挂刀余额折扣报表</h1>
        <p>Steam 卖出与 C5 补仓对账</p>
      </div>
      <div class="api-state" :class="apiState.tone">
        <span></span>{{ apiState.label }}
      </div>
    </header>

    <section class="panel report-query-panel">
      <div class="report-query-grid">
        <label class="query-field date-field">
          <span>时间范围</span>
          <FolioDateTimeRange v-model="dateRange" />
        </label>
        <label class="query-field">
          <span>饰品</span>
          <input v-model="itemName" type="text" placeholder="精确 market hash name（可选）" @keyup.enter="queryReport" />
        </label>
        <label class="detail-toggle">
          <input v-model="includeDetails" type="checkbox" />
          <span>每笔明细</span>
        </label>
        <button class="primary-button query-button" type="button" :disabled="loading" @click="queryReport">
          {{ loading ? "查询中…" : "查询报表" }}
        </button>
      </div>
      <p v-if="error" class="query-error">{{ error }}</p>
      <p v-else class="query-caption">{{ formatRangeLabel() }}</p>
    </section>

    <template v-if="report">
      <section class="report-section">
        <div class="section-heading">
          <div>
            <p class="eyebrow">Current Wallet Income</p>
            <h2>本期钱包入账</h2>
          </div>
          <span>仅本期已闭环 + 本期未闭环</span>
        </div>
        <div class="report-metrics">
          <article><span>卖出笔数</span><strong>{{ currentWalletSummary.count }}</strong></article>
          <article><span>Steam 面值</span><strong>{{ formatMoney(currentWalletSummary.steamGross) }}</strong></article>
          <article><span>Steam 到手</span><strong>{{ formatMoney(currentWalletSummary.steamNet) }}</strong></article>
          <article><span>C5 金额</span><strong>{{ formatMoney(currentWalletSummary.cash) }}</strong></article>
          <article><span>总折比</span><strong>{{ formatPct(currentWalletSummary.totalDiscountRatio) }}</strong></article>
        </div>
        <p class="section-note">只有这组数据对应所选时间段的 Steam 钱包卖出入账；历史补仓与历史未闭环不参与本期加总。</p>
      </section>

      <section class="panel reconciliation-panel">
        <div class="section-heading compact">
          <div><p class="eyebrow">Reconciliation</p><h2>卖出时间对账</h2></div>
        </div>
        <div class="table-wrap">
          <table class="data-table report-table">
            <thead><tr><th>状态</th><th>笔数</th><th>Steam 面值</th><th>Steam 到手</th><th>C5 金额</th><th>总折比</th></tr></thead>
            <tbody>
              <tr v-for="row in reconciliationRows" :key="row.key" :class="{ 'historical-row': row.historical }">
                <td><span class="status-pill" :class="row.tone">{{ row.label }}</span></td>
                <td>{{ row.summary.count }}</td>
                <td>{{ formatMoney(row.summary.steamGross) }}</td>
                <td>{{ formatMoney(row.summary.steamNet) }}</td>
                <td>{{ row.summary.cash > 0 ? formatMoney(row.summary.cash) : "—" }}</td>
                <td>{{ formatPct(row.summary.totalDiscountRatio) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="panel-footnote">历史相关行仅用于解释本期开始前的卖出尾巴，不计入本期钱包入账。</p>
      </section>

      <section v-if="missingOfficialTime.count" class="time-warning">
        <div><strong>程序时间归期 {{ missingOfficialTime.count }} 笔</strong><span>Steam 到手 {{ formatMoney(missingOfficialTime.steamNet) }}</span><span>已计入主表</span></div>
        <button type="button" @click="showTimeExplanation = !showTimeExplanation">{{ showTimeExplanation ? "收起说明" : "查看说明" }}</button>
        <p v-if="showTimeExplanation">这些流水缺少 Steam 官方成交时间，已按程序确认卖出时间计入本期已闭环或本期未闭环。该分组只提示时间来源，不能重复相加。</p>
      </section>

      <section class="panel item-summary-panel">
        <div class="section-heading compact">
          <div><p class="eyebrow">Item Summary</p><h2>按饰品汇总</h2><span>按 C5 补仓完成时间</span></div>
          <button class="secondary-button" type="button" @click="itemSortDescending = !itemSortDescending">
            按 C5 现金{{ itemSortDescending ? "降序" : "升序" }}
          </button>
        </div>
        <div v-if="sortedItems.length" class="table-wrap">
          <table class="data-table report-table item-table">
            <thead><tr><th>饰品</th><th>笔数</th><th>Steam 到手</th><th>C5 现金</th><th>总折比</th></tr></thead>
            <tbody><tr v-for="row in sortedItems" :key="row.marketHashName"><td>{{ row.marketHashName }}</td><td>{{ row.count }}</td><td>{{ formatMoney(row.steamNet) }}</td><td>{{ formatMoney(row.cash) }}</td><td>{{ formatPct(row.totalDiscountRatio) }}</td></tr></tbody>
          </table>
        </div>
        <div v-else class="empty-report">所选时间范围没有已完成 C5 补仓的挂刀闭环。</div>
      </section>

      <section v-if="includeDetails" class="panel detail-panel">
        <div class="section-heading compact"><div><p class="eyebrow">Details</p><h2>每笔闭环明细</h2></div><span>{{ report.details.length }} 笔</span></div>
        <div v-if="report.details.length" class="table-wrap">
          <table class="data-table report-table detail-table">
            <thead><tr><th>完成时间</th><th>饰品</th><th>Steam 面值</th><th>Steam 到手</th><th>C5 现金</th><th>总折比</th><th>Asset</th><th>Listing</th></tr></thead>
            <tbody><tr v-for="row in visibleDetails" :key="`${row.listingId}-${row.assetId}`"><td>{{ row.completedAtLocal }}</td><td>{{ row.marketHashName }}</td><td>{{ formatMoney(row.steamGross) }}</td><td>{{ formatMoney(row.steamNet) }}</td><td>{{ formatMoney(row.cash) }}</td><td>{{ formatPct(row.totalDiscountRatio) }}</td><td>{{ row.assetId || "—" }}</td><td>{{ row.listingId || "—" }}</td></tr></tbody>
          </table>
        </div>
        <div v-else class="empty-report">当前筛选没有可显示的逐笔明细。</div>
        <div v-if="report.details.length > detailPageSize" class="detail-pagination">
          <span>第 {{ detailPage }} / {{ detailPageCount }} 页 · 每页 {{ detailPageSize }} 条</span>
          <div>
            <button class="secondary-button" type="button" :disabled="detailPage <= 1" @click="detailPage -= 1">上一页</button>
            <button class="secondary-button" type="button" :disabled="detailPage >= detailPageCount" @click="detailPage += 1">下一页</button>
          </div>
        </div>
      </section>
    </template>

    <section v-else-if="loading" class="panel report-loading"><span></span><p>正在生成挂刀对账报表…</p></section>
  </main>
</template>

<style scoped>
.guadao-report-page { max-width: 1360px; }
.report-heading p:not(.eyebrow) { margin: 7px 0 0; color: var(--folio-muted); font-size: 13px; }
.api-state { display: inline-flex; align-items: center; gap: 8px; border: 1px solid var(--folio-line); border-radius: 999px; padding: 7px 11px; color: var(--folio-muted); background: #fff; font-size: 11px; font-weight: 700; }
.api-state span { width: 7px; height: 7px; border-radius: 50%; background: currentColor; box-shadow: 0 0 0 4px rgba(35,106,76,.08); }
.api-state.success { color: var(--folio-green); }
.api-state.danger { color: var(--folio-red); }
.api-state.pending { color: var(--folio-amber); }
.report-query-panel { padding: 16px; }
.report-query-grid { display: grid; grid-template-columns: minmax(440px, 1.5fr) minmax(260px, 1fr) auto 140px; gap: 14px; align-items: end; }
.query-field { min-width: 0; display: grid; gap: 7px; color: var(--folio-muted); font-size: 11px; font-weight: 700; }
.query-field > input { min-height: 44px; border: 1px solid #dfe4df; border-radius: 11px; padding: 9px 12px; color: var(--folio-ink); background: #fff; outline: none; }
.query-field > input:focus { border-color: var(--folio-green); box-shadow: 0 0 0 3px rgba(35,106,76,.1); }
.detail-toggle { min-height: 44px; display: inline-flex; align-items: center; gap: 9px; padding: 0 5px; color: #4f5a53; font-size: 12px; font-weight: 700; white-space: nowrap; }
.detail-toggle input { width: 17px; height: 17px; accent-color: var(--folio-green); }
.query-button { min-height: 44px; }
.query-caption, .query-error { margin: 12px 0 0; border-top: 1px solid var(--folio-line); padding-top: 10px; color: var(--folio-muted); font-size: 11px; }
.query-error { color: var(--folio-red); }
.report-section { display: grid; gap: 10px; }
.section-heading { display: flex; justify-content: space-between; align-items: end; gap: 18px; }
.section-heading h2 { margin: 0; color: var(--folio-ink); font-size: 19px; letter-spacing: -.025em; }
.section-heading > span, .section-heading div > span { color: var(--folio-muted); font-size: 11px; }
.section-heading.compact { align-items: center; margin-bottom: 13px; }
.section-heading.compact .eyebrow { margin-bottom: 3px; }
.report-metrics { display: grid; grid-template-columns: .85fr repeat(4, 1fr); gap: 10px; }
.report-metrics article { min-height: 94px; display: grid; align-content: space-between; border: 1px solid var(--folio-line); border-radius: 15px; padding: 15px 16px; background: #fff; box-shadow: var(--folio-shadow); }
.report-metrics span { color: var(--folio-muted); font-size: 11px; font-weight: 650; }
.report-metrics strong { color: var(--folio-green); font-size: 22px; letter-spacing: -.035em; }
.section-note { margin: 0; color: var(--folio-muted); font-size: 11px; }
.report-table { min-width: 880px; }
.report-table th { color: #59645d; background: var(--folio-surface-soft); font-size: 11px; }
.report-table td { color: var(--folio-ink); font-size: 12px; font-variant-numeric: tabular-nums; }
.report-table tr.historical-row:first-of-type td { border-top: 2px solid #d9dfda; }
.historical-row td { background: #fbfcfb; }
.status-pill { display: inline-flex; align-items: center; border-radius: 999px; padding: 5px 9px; font-size: 11px; font-weight: 750; }
.status-pill.success { color: var(--folio-green); background: var(--folio-green-soft); }
.status-pill.warning { color: var(--folio-amber); background: var(--folio-amber-soft); }
.status-pill.neutral { color: #657069; background: #eef1ee; }
.panel-footnote { margin: 11px 0 0; color: var(--folio-muted); font-size: 11px; }
.time-warning { display: grid; grid-template-columns: 1fr auto; gap: 8px 16px; align-items: center; border: 1px solid #ead59e; border-radius: 13px; padding: 11px 14px; color: #715214; background: var(--folio-amber-soft); }
.time-warning div { display: flex; gap: 14px; align-items: center; font-size: 12px; }
.time-warning button { border: 0; color: #715214; background: transparent; font-size: 11px; font-weight: 750; cursor: pointer; }
.time-warning p { grid-column: 1 / -1; margin: 0; border-top: 1px solid #ead9ac; padding-top: 8px; font-size: 11px; line-height: 1.65; }
.item-table td:first-child { font-weight: 650; }
.detail-table { min-width: 1260px; }
.detail-pagination { display: flex; justify-content: space-between; align-items: center; gap: 14px; border-top: 1px solid var(--folio-line); padding-top: 13px; color: var(--folio-muted); font-size: 11px; }
.detail-pagination div { display: flex; gap: 8px; }
.detail-pagination .secondary-button { min-height: 34px; padding: 5px 11px; }
.empty-report { border: 1px dashed #d8ded9; border-radius: 12px; padding: 28px; color: var(--folio-muted); text-align: center; background: var(--folio-surface-soft); font-size: 12px; }
.report-loading { min-height: 170px; display: grid; place-items: center; align-content: center; gap: 12px; color: var(--folio-muted); }
.report-loading span { width: 26px; height: 26px; border: 3px solid #dbe7df; border-top-color: var(--folio-green); border-radius: 50%; animation: report-spin .8s linear infinite; }
@keyframes report-spin { to { transform: rotate(360deg); } }
</style>
