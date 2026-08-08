<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import FolioIcon from "../components/FolioIcon.vue";
import CaseMonitorButton from "../components/case-monitor/CaseMonitorButton.vue";
import CaseMonitorCategoryTabs from "../components/case-monitor/CaseMonitorCategoryTabs.vue";
import CaseMonitorDetailDrawer from "../components/case-monitor/CaseMonitorDetailDrawer.vue";
import CaseMonitorFeedback from "../components/case-monitor/CaseMonitorFeedback.vue";
import CaseMonitorIntervalPicker from "../components/case-monitor/CaseMonitorIntervalPicker.vue";
import CaseMonitorPagination from "../components/case-monitor/CaseMonitorPagination.vue";
import CaseMonitorRecommendationRow from "../components/case-monitor/CaseMonitorRecommendationRow.vue";
import CaseMonitorSearch from "../components/case-monitor/CaseMonitorSearch.vue";
import CaseMonitorSegmented from "../components/case-monitor/CaseMonitorSegmented.vue";
import CaseMonitorStatusChip from "../components/case-monitor/CaseMonitorStatusChip.vue";
import CaseMonitorToggle from "../components/case-monitor/CaseMonitorToggle.vue";
import {
  formatClock,
  formatDuration,
  recommendedRatio,
} from "../components/case-monitor/format";
import type {
  CaseCategoryOption,
  CaseMonitorJob,
  CaseMonitorStatus,
  CaseRatioItem,
  CaseRatioReport,
  SelectOption,
} from "../components/case-monitor/types";
import "../components/case-monitor/case-monitor.css";

type ReportWindow = "24" | "168" | "720" | "custom";
type Notice = {
  tone: "success" | "error";
  message: string;
  showExports?: boolean;
};

const typeOrder = ["weapon_case", "capsule", "souvenir_package"] as const;
const typeLabels: Record<string, string> = {
  weapon_case: "武器箱",
  capsule: "胶囊",
  souvenir_package: "纪念包",
};
const reportWindows: SelectOption<ReportWindow>[] = [
  { value: "24", label: "24小时" },
  { value: "168", label: "7天" },
  { value: "720", label: "30天" },
  { value: "custom", label: "自定义" },
];

const report = ref<CaseRatioReport | null>(null);
const runtime = ref<CaseMonitorStatus | null>(null);
const loading = ref(true);
const reportError = ref("");
const statusError = ref("");
const action = ref<"" | "collect" | "report" | "start" | "pause">("");
const intervalMinutes = ref(5);
const reportWindow = ref<ReportWindow>("24");
const refreshLiquidity = ref(true);
const customStart = ref("");
const customEnd = ref("");
const keyword = ref("");
const typeFilter = ref("all");
const currentPage = ref(1);
const pageSize = ref(10);
const selectedName = ref("");
const drawerItem = ref<CaseRatioItem | null>(null);
const notice = ref<Notice | null>(null);
const initializedStatus = ref(false);
const lastSeenCompletedJob = ref("");
let pollTimer: number | undefined;

async function readJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    cache: "no-store",
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
  });
  const payload = await response.json().catch(() => ({})) as Record<string, unknown>;
  if (!response.ok) {
    throw new Error(String(payload.error || `HTTP ${response.status}`));
  }
  return payload as T;
}

async function loadReport(): Promise<void> {
  reportError.value = "";
  try {
    const payload = await readJson<{ report: CaseRatioReport }>("/api/case-monitor/report/latest");
    report.value = payload.report;
    return;
  } catch (apiError) {
    try {
      const response = await fetch("/guadao_case_ratio_report.json", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      report.value = await response.json() as CaseRatioReport;
    } catch {
      report.value = null;
      reportError.value = apiError instanceof Error ? apiError.message : String(apiError);
    }
  }
}

function maybeHandleCompletedJob(job: CaseMonitorJob | null): void {
  if (!job || job.status !== "completed") return;
  if (!initializedStatus.value) {
    lastSeenCompletedJob.value = job.jobId;
    return;
  }
  if (lastSeenCompletedJob.value === job.jobId) return;
  lastSeenCompletedJob.value = job.jobId;
  if (job.jobType === "report") {
    void loadReport();
    notice.value = {
      tone: "success",
      message: "全量箱子报告已生成，网页数据已刷新。",
      showExports: true,
    };
  } else {
    const okCount = Number(job.result?.okCount || 0);
    const missingCount =
      Number(job.result?.missingC5Count || 0) +
      Number(job.result?.missingSteamCount || 0);
    notice.value = {
      tone: "success",
      message: `采集完成：成功 ${okCount}，缺价 ${missingCount}`,
    };
  }
}

async function loadStatus(): Promise<void> {
  try {
    const payload = await readJson<CaseMonitorStatus>("/api/case-monitor/status");
    runtime.value = payload;
    statusError.value = "";
    if (!payload.runtime.enabled || !initializedStatus.value) {
      intervalMinutes.value = payload.runtime.intervalMinutes || intervalMinutes.value;
    }
    maybeHandleCompletedJob(payload.latestJob);
    initializedStatus.value = true;
  } catch (error) {
    runtime.value = null;
    statusError.value = error instanceof Error ? error.message : String(error);
    initializedStatus.value = true;
  }
}

async function postAction<T>(
  name: typeof action.value,
  path: string,
  body: Record<string, unknown> = {},
): Promise<T> {
  action.value = name;
  try {
    const payload = await readJson<T>(path, {
      method: "POST",
      body: JSON.stringify(body),
    });
    await loadStatus();
    return payload;
  } catch (error) {
    notice.value = {
      tone: "error",
      message: error instanceof Error ? error.message : String(error),
    };
    throw error;
  } finally {
    action.value = "";
  }
}

async function collectOnce(): Promise<void> {
  try {
    await postAction("collect", "/api/case-monitor/collect");
    notice.value = { tone: "success", message: "采集任务已进入后台队列。" };
  } catch {
    // The shared action handler already exposes the error.
  }
}

async function generateReport(): Promise<void> {
  const body: Record<string, unknown> = {
    refreshLiquidity: refreshLiquidity.value,
  };
  if (reportWindow.value === "custom") {
    if (!customStart.value || !customEnd.value) {
      notice.value = { tone: "error", message: "自定义报告窗口需要同时选择开始和结束时间。" };
      return;
    }
    body.dateFrom = new Date(customStart.value).toISOString();
    body.dateTo = new Date(customEnd.value).toISOString();
  } else {
    body.hours = Number(reportWindow.value);
  }
  try {
    await postAction("report", "/api/case-monitor/report", body);
    notice.value = { tone: "success", message: "全量箱子报告正在后台生成。" };
  } catch {
    // The shared action handler already exposes the error.
  }
}

async function toggleMonitor(): Promise<void> {
  const enabled = runtime.value?.runtime.enabled ?? false;
  try {
    if (enabled) {
      await postAction("pause", "/api/case-monitor/pause");
      notice.value = { tone: "success", message: "监控已暂停，当前任务会安全完成。" };
    } else {
      await postAction("start", "/api/case-monitor/start", {
        intervalMinutes: intervalMinutes.value,
      });
      notice.value = {
        tone: "success",
        message: `监控已启动，每 ${intervalMinutes.value} 分钟采集一次。`,
      };
    }
  } catch {
    // The shared action handler already exposes the error.
  }
}

const runtimeStatus = computed(() => {
  if (statusError.value) return "offline";
  const current = runtime.value?.currentJob;
  if (current?.status === "running" || current?.status === "queued") {
    return current.jobType === "report" ? "reporting" : "collecting";
  }
  return runtime.value?.runtime.enabled ? "running" : "paused";
});

const runtimeLabel = computed(() => {
  const job = runtime.value?.currentJob;
  if (job?.status === "running" || job?.status === "queued") {
    const total = Number(job.progressTotal || 0);
    const progress = total > 0 ? ` ${job.progressCurrent}/${total}` : "";
    return job.jobType === "report" ? `正在生成报告${progress}` : `正在采集${progress}`;
  }
  if (statusError.value) return "后端离线";
  return runtime.value?.runtime.enabled ? "监控运行中" : "监控已暂停";
});

const busy = computed(() => Boolean(runtime.value?.runtime.busy || action.value));

const categoryOptions = computed<CaseCategoryOption[]>(() => {
  const counts = report.value?.crateTypeCounts || {};
  const options = typeOrder.map((key) => ({
    key,
    label: typeLabels[key],
    count: Number(counts[key] || 0),
  }));
  const known = options.reduce((sum, option) => sum + option.count, 0);
  const all = Number(report.value?.itemCount || 0);
  return [
    { key: "all", label: "全部", count: all },
    ...options,
    { key: "other", label: "其他箱类", count: Math.max(0, all - known) },
  ];
});

const filteredItems = computed(() => {
  const source = report.value?.items || [];
  const needle = keyword.value.trim().toLocaleLowerCase("zh-CN");
  return source.filter((item) => {
    const categoryMatched =
      typeFilter.value === "all" ||
      (typeFilter.value === "other"
        ? !typeOrder.includes(item.crateType as typeof typeOrder[number])
        : item.crateType === typeFilter.value);
    const text = `${item.marketHashName} ${item.name || ""}`.toLocaleLowerCase("zh-CN");
    return categoryMatched && (!needle || text.includes(needle));
  });
});

const rankedItems = computed(() =>
  [...filteredItems.value].sort((left, right) => {
    const leftRatio = recommendedRatio(left);
    const rightRatio = recommendedRatio(right);
    const leftSane = leftRatio >= 0.3 && leftRatio <= 0.95 ? 1 : 0;
    const rightSane = rightRatio >= 0.3 && rightRatio <= 0.95 ? 1 : 0;
    return (
      rightSane - leftSane ||
      Number(right.recommendationScore || 0) - Number(left.recommendationScore || 0) ||
      Number(right.steamVolume24h || 0) - Number(left.steamVolume24h || 0) ||
      left.marketHashName.localeCompare(right.marketHashName)
    );
  }),
);

const totalPages = computed(() => Math.max(1, Math.ceil(rankedItems.value.length / pageSize.value)));
const pagedItems = computed(() => {
  const start = (currentPage.value - 1) * pageSize.value;
  return rankedItems.value.slice(start, start + pageSize.value);
});

const validSnapshots = computed(() => Number(report.value?.statusCounts?.ok || 0));
const averageCoverage = computed(() => {
  const items = report.value?.items || [];
  if (!items.length) return 0;
  return items.reduce((sum, item) => sum + Number(item.coveragePct || 0), 0) / items.length;
});
const lastCollection = computed(() => runtime.value?.runtime.lastCollectionResult || {});
const missingCount = computed(() =>
  Number(lastCollection.value.missingC5Count || 0) +
  Number(lastCollection.value.missingSteamCount || 0),
);

function openDetail(item: CaseRatioItem): void {
  selectedName.value = item.marketHashName;
  drawerItem.value = item;
}

function chooseCategory(value: string): void {
  typeFilter.value = value;
  currentPage.value = 1;
  selectedName.value = "";
}

function exportUrl(format: "json" | "summary_csv" | "buckets_csv" | "markdown"): string {
  const reportId = runtime.value?.latestReport.reportId;
  const query = new URLSearchParams({ format });
  if (reportId) query.set("reportId", reportId);
  return `/api/case-monitor/report/export?${query.toString()}`;
}

watch(keyword, () => {
  currentPage.value = 1;
  selectedName.value = "";
});
watch(pageSize, () => {
  currentPage.value = 1;
});
watch(rankedItems, () => {
  currentPage.value = Math.min(currentPage.value, totalPages.value);
  if (!selectedName.value && rankedItems.value.length) {
    selectedName.value = rankedItems.value[0].marketHashName;
  }
}, { immediate: true });

onMounted(async () => {
  await Promise.all([loadStatus(), loadReport()]);
  loading.value = false;
  pollTimer = window.setInterval(() => void loadStatus(), 2500);
});

onBeforeUnmount(() => {
  if (pollTimer !== undefined) window.clearInterval(pollTimer);
});
</script>

<template>
  <main class="cm-surface case-monitor-page">
    <header class="case-monitor-header">
      <div class="case-monitor-title">
        <h1>箱子挂刀比监控</h1>
        <CaseMonitorStatusChip :status="runtimeStatus" :label="runtimeLabel" />
      </div>
      <p class="case-monitor-safety">只采集与报告，不会修改策略配置或触发真实交易</p>
      <div class="case-monitor-actions">
        <CaseMonitorButton
          tone="primary"
          icon="refresh"
          :loading="action === 'collect'"
          :disabled="busy"
          @click="collectOnce"
        >
          采集一次
        </CaseMonitorButton>
        <CaseMonitorButton
          tone="primary"
          icon="download"
          :loading="action === 'report'"
          :disabled="busy"
          @click="generateReport"
        >
          生成报告
        </CaseMonitorButton>
        <CaseMonitorButton
          :tone="runtime?.runtime.enabled ? 'quiet' : 'success'"
          :icon="runtime?.runtime.enabled ? 'pause' : 'play'"
          :loading="action === 'pause' || action === 'start'"
          :disabled="Boolean(runtime?.runtime.busy)"
          @click="toggleMonitor"
        >
          {{ runtime?.runtime.enabled ? "暂停监控" : "开始监控" }}
        </CaseMonitorButton>
      </div>
    </header>

    <section class="case-monitor-controls" aria-label="监控与报告控制">
      <div class="case-control-group">
        <span class="case-control-label">采集间隔</span>
        <CaseMonitorIntervalPicker
          v-model="intervalMinutes"
          :disabled="Boolean(runtime?.runtime.enabled || busy)"
        />
      </div>
      <span class="case-control-divider" />
      <div class="case-control-group">
        <span class="case-control-label">报告窗口</span>
        <CaseMonitorSegmented v-model="reportWindow" :options="reportWindows" :disabled="busy" />
        <FolioIcon name="calendar" :size="14" class="case-custom-calendar" />
        <div v-if="reportWindow === 'custom'" class="case-custom-range">
          <input v-model="customStart" type="datetime-local" aria-label="报告开始时间" />
          <span>至</span>
          <input v-model="customEnd" type="datetime-local" aria-label="报告结束时间" />
        </div>
      </div>
      <span class="case-control-divider" />
      <div class="case-control-group">
        <span class="case-control-label">刷新 Steam 成交量</span>
        <CaseMonitorToggle v-model="refreshLiquidity" :disabled="busy" />
      </div>
      <span class="case-control-divider" />
      <div class="case-cycle-summary">
        本轮 {{ lastCollection.targetCount || 0 }} 类 ·
        成功 {{ lastCollection.okCount || 0 }} ·
        缺 C5 {{ lastCollection.missingC5Count || 0 }} ·
        缺 Steam {{ lastCollection.missingSteamCount || 0 }}
      </div>
      <span class="case-control-divider" />
      <div class="case-time-stat"><span>上次采集</span><strong>{{ formatClock(runtime?.runtime.lastCollectionAt) }}</strong></div>
      <span class="case-control-divider" />
      <div class="case-time-stat"><span>下次采集</span><strong>{{ formatClock(runtime?.runtime.nextRunAt) }}</strong></div>
      <span class="case-control-divider" />
      <div class="case-time-stat"><span>上次报告</span><strong>{{ formatClock(runtime?.runtime.lastReportAt || report?.generatedAt) }}</strong></div>
    </section>

    <section class="case-monitor-metrics" aria-label="监控概览">
      <article class="case-metric">
        <span class="case-metric__icon case-metric__icon--green"><FolioIcon name="shield" :size="31" /></span>
        <div><span>有效快照</span><strong>{{ validSnapshots.toLocaleString("zh-CN") }}</strong></div>
      </article>
      <article class="case-metric">
        <span class="case-metric__icon case-metric__icon--blue"><FolioIcon name="case" :size="31" /></span>
        <div><span>监控品类</span><strong class="is-blue">{{ report?.itemCount || 0 }}</strong></div>
      </article>
      <article class="case-metric">
        <span class="case-metric__icon case-metric__icon--amber"><FolioIcon name="report" :size="31" /></span>
        <div><span>覆盖率</span><strong class="is-amber">{{ averageCoverage.toFixed(2) }}%</strong></div>
      </article>
      <article class="case-metric">
        <span class="case-metric__icon case-metric__icon--green"><FolioIcon name="clock" :size="31" /></span>
        <div><span>运行时长</span><strong>{{ runtime?.runtime.enabled ? formatDuration(runtime.runtime.runningSeconds) : "已暂停" }}</strong></div>
      </article>
    </section>

    <section class="case-ranking-panel">
      <div class="case-ranking-toolbar">
        <CaseMonitorCategoryTabs
          :model-value="typeFilter"
          :options="categoryOptions"
          @update:model-value="chooseCategory"
        />
        <CaseMonitorSearch v-model="keyword" />
      </div>
      <h2>推荐排行</h2>

      <div v-if="loading" class="cm-empty">正在读取最新全量报告…</div>
      <div v-else-if="reportError" class="cm-empty">
        报告暂不可用：{{ reportError }}
      </div>
      <template v-else>
        <div class="case-table-scroll">
          <div class="cm-recommendation-header" aria-hidden="true">
            <span>排名</span>
            <span>箱子</span>
            <span class="cm-recommendation-header__info">建议比例 <FolioIcon name="info" :size="12" /></span>
            <span>采用源</span>
            <span class="cm-recommendation-header__info">24h量 <FolioIcon name="info" :size="12" /></span>
            <span>速度</span>
            <span class="cm-recommendation-header__info">稳定性 <FolioIcon name="info" :size="12" /></span>
            <span>最低/持续</span>
            <span>最高/持续</span>
            <span>详情</span>
          </div>
          <CaseMonitorRecommendationRow
            v-for="(item, index) in pagedItems"
            :key="item.marketHashName"
            :item="item"
            :rank="(currentPage - 1) * pageSize + index + 1"
            :selected="selectedName === item.marketHashName"
            @click="openDetail(item)"
          />
          <div v-if="!pagedItems.length" class="cm-empty">
            没有符合当前类别和关键词的箱子
          </div>
        </div>
        <CaseMonitorPagination
          v-model="currentPage"
          :total-items="rankedItems.length"
          :page-size="pageSize"
          @update:page-size="pageSize = $event"
        />
      </template>
    </section>

    <CaseMonitorFeedback
      v-if="notice"
      class="case-monitor-toast"
      :tone="notice.tone"
      @close="notice = null"
    >
      {{ notice.message }}
      <template v-if="notice.showExports" #actions>
        <a :href="exportUrl('summary_csv')">汇总 CSV</a>
        <a :href="exportUrl('buckets_csv')">区间 CSV</a>
        <a :href="exportUrl('markdown')">Markdown</a>
      </template>
    </CaseMonitorFeedback>

    <CaseMonitorDetailDrawer
      :open="Boolean(drawerItem)"
      :item="drawerItem"
      @close="drawerItem = null"
    />
  </main>
</template>

<style scoped>
.case-monitor-page {
  width: 100%;
  min-width: 0;
  min-height: 0;
  height: calc(100vh - 58px);
  height: calc(100dvh - 58px);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  margin: 0;
  padding: 16px 34px 5px;
  background: var(--cm-canvas);
}

.case-monitor-header {
  min-height: 48px;
  display: grid;
  grid-template-columns: minmax(300px, 1fr) minmax(320px, auto) minmax(360px, 1fr);
  align-items: center;
  gap: 18px;
}

.case-monitor-title {
  display: flex;
  align-items: center;
  gap: 20px;
}

.case-monitor-title h1 {
  margin: 0;
  color: var(--cm-ink);
  font-size: 22px;
  font-weight: 700;
  line-height: 1.2;
}

.case-monitor-title :deep(.cm-status-chip) {
  min-height: 24px;
  border: 0;
  padding: 4px 0;
  background: transparent;
}

.case-monitor-safety {
  margin: 0;
  color: #738099;
  font-size: 12px;
  text-align: center;
  white-space: nowrap;
}

.case-monitor-actions {
  display: flex;
  justify-content: flex-end;
  gap: 14px;
}

.case-monitor-actions :deep(.cm-button) {
  min-width: 112px;
}

.case-monitor-controls {
  position: relative;
  min-height: 52px;
  display: flex;
  align-items: center;
  gap: 14px;
  border: 1px solid var(--cm-line);
  border-radius: 6px;
  padding: 8px 16px;
  background: var(--cm-panel);
  box-shadow: 0 1px 3px rgba(30, 48, 75, 0.03);
  white-space: nowrap;
}

.case-control-group {
  position: relative;
  display: flex;
  align-items: center;
  gap: 11px;
}

.case-control-label {
  color: #505d72;
  font-size: 11px;
  font-weight: 600;
}

.case-control-divider {
  width: 1px;
  height: 23px;
  flex: 0 0 auto;
  background: var(--cm-line);
}

.case-custom-calendar {
  margin-left: -31px;
  color: #5d6b82;
  pointer-events: none;
}

.case-custom-range {
  position: absolute;
  top: 39px;
  right: 0;
  z-index: 20;
  display: flex;
  align-items: center;
  gap: 7px;
  border: 1px solid var(--cm-line);
  border-radius: 5px;
  padding: 9px;
  background: #ffffff;
  box-shadow: 0 8px 22px rgba(28, 43, 66, 0.13);
}

.case-custom-range input {
  min-height: 32px;
  border: 1px solid var(--cm-line-strong);
  border-radius: 4px;
  padding: 5px 7px;
  font-size: 11px;
}

.case-cycle-summary {
  color: #5a667b;
  font-size: 11px;
}

.case-time-stat {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #5b687d;
  font-size: 10px;
}

.case-time-stat strong {
  color: #37445b;
  font-size: 11px;
  font-weight: 600;
}

.case-monitor-metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 16px;
  margin-top: 9px;
}

.case-metric {
  min-height: 73px;
  display: flex;
  align-items: center;
  gap: 16px;
  border: 1px solid var(--cm-line);
  border-radius: 6px;
  padding: 10px 18px;
  background: #ffffff;
  box-shadow: 0 1px 3px rgba(30, 48, 75, 0.03);
}

.case-metric__icon {
  width: 54px;
  height: 54px;
  display: grid;
  flex: 0 0 auto;
  place-items: center;
  border-radius: 50%;
}

.case-metric__icon--green {
  color: var(--cm-green);
  background: #eaf5ef;
}

.case-metric__icon--blue {
  color: var(--cm-blue);
  background: #edf3ff;
}

.case-metric__icon--amber {
  color: #ff9700;
  background: #fff2de;
}

.case-metric > div {
  display: grid;
  gap: 3px;
}

.case-metric span {
  color: #59667b;
  font-size: 12px;
}

.case-metric strong {
  color: var(--cm-green);
  font-size: 22px;
  font-weight: 700;
  line-height: 1;
}

.case-metric strong.is-blue {
  color: var(--cm-blue);
}

.case-metric strong.is-amber {
  color: #ff9200;
}

.case-ranking-panel {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  margin-top: 7px;
  overflow: hidden;
  border: 1px solid var(--cm-line);
  border-radius: 6px;
  background: #ffffff;
  box-shadow: 0 1px 3px rgba(30, 48, 75, 0.03);
}

.case-ranking-toolbar {
  min-height: 47px;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 314px;
  align-items: center;
  gap: 18px;
  border-bottom: 1px solid var(--cm-line);
  padding: 8px;
}

.case-ranking-panel h2 {
  min-height: 30px;
  display: flex;
  align-items: center;
  margin: 0;
  padding: 0 13px;
  color: #1f2a3d;
  font-size: 15px;
  font-weight: 700;
}

.case-table-scroll {
  width: 100%;
  flex: 1 1 auto;
  min-height: 0;
  max-height: none;
  overflow: auto;
  scrollbar-width: none;
}

.case-table-scroll::-webkit-scrollbar {
  width: 0;
  height: 0;
}

.case-table-scroll :deep(.cm-recommendation-header) {
  position: sticky;
  top: 0;
  z-index: 3;
  min-height: 31px;
}

.case-monitor-toast {
  position: fixed;
  right: 18px;
  bottom: 18px;
  z-index: 80;
  max-width: min(620px, calc(100vw - 36px));
  box-shadow: 0 8px 24px rgba(22, 36, 58, 0.14);
}

@media (max-width: 1220px) {
  .case-monitor-header {
    grid-template-columns: 1fr auto;
  }

  .case-monitor-safety {
    display: none;
  }

  .case-monitor-controls {
    overflow-x: auto;
  }
}

@media (max-width: 780px) {
  .case-monitor-page {
    min-height: calc(100vh - 58px);
    min-height: calc(100dvh - 58px);
    height: auto;
    overflow: visible;
    padding: 12px 10px 18px;
  }

  .case-ranking-panel,
  .case-table-scroll {
    flex: 0 0 auto;
  }

  .case-monitor-header {
    align-items: stretch;
    grid-template-columns: 1fr;
  }

  .case-monitor-title {
    justify-content: space-between;
  }

  .case-monitor-actions {
    justify-content: flex-start;
    overflow-x: auto;
  }

  .case-monitor-metrics {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }

  .case-metric {
    gap: 10px;
    padding: 9px 10px;
  }

  .case-metric__icon {
    width: 42px;
    height: 42px;
  }

  .case-ranking-toolbar {
    align-items: stretch;
    grid-template-columns: 1fr;
  }
}
</style>
