<script setup lang="ts">
import { ref } from "vue";
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
import type {
  CaseCategoryOption,
  CaseRatioItem,
  SelectOption,
} from "../components/case-monitor/types";
import "../components/case-monitor/case-monitor.css";

const interval = ref(5);
const disabledInterval = ref(5);
const reportWindow = ref("24");
const refreshLiquidity = ref(true);
const searchEmpty = ref("");
const searchFilled = ref("Paris 2023");
const category = ref("all");
const page = ref(1);
const pageDisabled = ref(1);
const drawerOpen = ref(true);

const reportWindows: SelectOption[] = [
  { value: "24", label: "24小时" },
  { value: "168", label: "7天" },
  { value: "720", label: "30天" },
  { value: "custom", label: "自定义" },
];

const categories: CaseCategoryOption[] = [
  { key: "all", label: "全部", count: 439 },
  { key: "weapon_case", label: "武器箱", count: 42 },
  { key: "capsule", label: "胶囊", count: 231 },
  { key: "souvenir_package", label: "纪念包", count: 145 },
  { key: "other", label: "其他箱类", count: 21 },
];

const sampleTimelineRatios = [
  0.697, 0.698, 0.697, 0.699, 0.7, 0.699, 0.701, 0.699,
  0.698, 0.696, 0.697, 0.695, 0.694, 0.693, 0.69, 0.689,
  0.687, 0.686, 0.688, 0.69, 0.691, 0.692, 0.693, 0.694,
  0.693, 0.694, 0.695, 0.696, 0.697, 0.697, 0.698, 0.699,
];

const sampleItem: CaseRatioItem = {
  marketHashName: "Horizon Case",
  name: "地平线武器箱",
  crateType: "weapon_case",
  crateTypeLabel: "武器箱",
  sampleCount: 288,
  okSampleCount: 288,
  latestRatio: 0.698,
  latestC5SellPrice: 4.72,
  latestSteamListPrice: 7.42,
  latestSteamAfterTaxPrice: 6.76,
  minRatio: 0.68,
  minRatioDurationLabel: "3h12m",
  maxRatio: 0.712,
  maxRatioDurationLabel: "1h05m",
  avgRatio: 0.698,
  p50Ratio: 0.696,
  p75Ratio: 0.701,
  p90Ratio: 0.708,
  conservativeMaxListingRatio: 0.68,
  recommendedMaxListingRatio: 0.698,
  aggressiveMaxListingRatio: 0.712,
  effectiveRecommendedMaxListingRatio: 0.698,
  selectedReferenceRatio: 0.698,
  steamReferenceSource: "seller_wall",
  steamReferenceSourceLabel: "20墙挂价",
  steamReferencePrice: 7.42,
  sellerFloorPrice: 7.35,
  sellerWallListPrice: 7.42,
  buyerMaxPrice: 7.21,
  steamVolume24h: 1284,
  steamVolume7d: 8972,
  steamAvgDailyVolume7d: 1281.7,
  liquidityLabel: "快",
  stddevRatio: 0.008,
  coveragePct: 97.01,
  recommendationScore: 0.98,
  legacySteamMinorUnitCorrectedCount: 0,
  buckets: [
    { bucket: "0.70-0.75", lower: 0.7, upper: 0.75, durationMinutes: 65, durationLabel: "1h05m", coveragePct: 19.4 },
    { bucket: "0.65-0.70", lower: 0.65, upper: 0.7, durationMinutes: 192, durationLabel: "3h12m", coveragePct: 47.1 },
    { bucket: "0.60-0.65", lower: 0.6, upper: 0.65, durationMinutes: 248, durationLabel: "4h08m", coveragePct: 30.4 },
    { bucket: "0.55-0.60", lower: 0.55, upper: 0.6, durationMinutes: 95, durationLabel: "1h35m", coveragePct: 11.5 },
    { bucket: "0.50-0.55", lower: 0.5, upper: 0.55, durationMinutes: 20, durationLabel: "20m", coveragePct: 1.6 },
  ],
  dominantBuckets: [],
  ratioThresholds: [],
  timelineSegments: sampleTimelineRatios.map((ratio, index) => ({
    startedAt: new Date(Date.UTC(2026, 6, 30, 3 + index * 0.75)).toISOString(),
    endedAt: new Date(Date.UTC(2026, 6, 30, 3.25 + index * 0.75)).toISOString(),
    ratio,
    bucket: "0.65-0.70",
    durationLabel: "45m",
    leftPct: index * 3.125,
    widthPct: 3.125,
  })),
};
</script>

<template>
  <main class="cm-surface case-atoms-page">
    <h1>原子组件板</h1>
    <div class="case-atoms-layout">
      <section class="case-atoms-sheet">
        <article class="case-atom-cell case-atom-cell--actions">
          <h2>操作按钮</h2>
          <div class="case-atom-row">
            <CaseMonitorButton tone="primary" icon="refresh">采集一次</CaseMonitorButton>
            <CaseMonitorButton tone="primary" icon="download">
              生成报告
              <FolioIcon name="sparkles" :size="15" />
            </CaseMonitorButton>
            <CaseMonitorButton icon="pause">暂停监控</CaseMonitorButton>
            <CaseMonitorButton tone="success" icon="play">开始监控</CaseMonitorButton>
            <CaseMonitorButton icon="error" disabled>不可用</CaseMonitorButton>
          </div>
        </article>

        <article class="case-atom-cell case-atom-cell--status">
          <h2>运行状态 Chip</h2>
          <div class="case-atom-row">
            <CaseMonitorStatusChip status="running" />
            <CaseMonitorStatusChip status="paused" />
            <CaseMonitorStatusChip status="collecting" label="正在采集 128/439" />
            <CaseMonitorStatusChip status="reporting" />
            <CaseMonitorStatusChip status="failed" />
          </div>
        </article>

        <article class="case-atom-cell">
          <h2>采集间隔（启用状态）</h2>
          <CaseMonitorIntervalPicker v-model="interval" expanded />
          <h2 class="case-atom-subtitle">采集间隔（运行中禁用）</h2>
          <CaseMonitorIntervalPicker v-model="disabledInterval" expanded disabled />
        </article>

        <article class="case-atom-cell">
          <h2>报告窗口（24小时已选中）</h2>
          <CaseMonitorSegmented v-model="reportWindow" :options="reportWindows" />
          <h2 class="case-atom-subtitle">刷新 Steam 成交量</h2>
          <div class="case-toggle-samples">
            <CaseMonitorToggle v-model="refreshLiquidity" />
            <CaseMonitorToggle :model-value="false" @update:model-value="refreshLiquidity = $event" />
          </div>
        </article>

        <article class="case-atom-cell">
          <h2>搜索框</h2>
          <CaseMonitorSearch v-model="searchEmpty" />
          <CaseMonitorSearch v-model="searchFilled" />
        </article>

        <article class="case-atom-cell">
          <h2>分类 Tabs</h2>
          <CaseMonitorCategoryTabs v-model="category" :options="categories" />
          <div class="case-pagination-samples">
            <div class="case-pagination-stack">
              <h2>分页器</h2>
              <CaseMonitorPagination
                v-model="page"
                :total-items="439"
                :page-size="10"
                compact
                @update:page-size="() => undefined"
              />
              <CaseMonitorPagination
                v-model="pageDisabled"
                :total-items="439"
                :page-size="10"
                compact
                disabled
                @update:page-size="() => undefined"
              />
            </div>
            <div>
              <h2>分页大小选择</h2>
              <select class="cm-page-size"><option>每页 10 条</option></select>
            </div>
          </div>
        </article>

        <div class="case-atoms-bottom">
          <article class="case-atom-cell case-atom-cell--rows">
            <h2>表格行（正常）</h2>
            <CaseMonitorRecommendationRow :item="sampleItem" :rank="1" />
            <h2 class="case-atom-subtitle">表格行（选中态）</h2>
            <CaseMonitorRecommendationRow :item="sampleItem" :rank="1" selected />
          </article>

          <article class="case-atom-cell case-atom-cell--feedback">
            <h2>任务反馈条</h2>
            <CaseMonitorFeedback tone="success">采集完成：成功 351，缺 C5 88</CaseMonitorFeedback>
            <CaseMonitorFeedback tone="error">Steam 暂时不可用，已保留上一份报告</CaseMonitorFeedback>
          </article>

          <article class="case-atom-cell case-atom-cell--trigger">
            <h2>比例详情抽屉<br />（打开示例）</h2>
          </article>
        </div>
      </section>

      <CaseMonitorDetailDrawer
        :open="drawerOpen"
        :item="sampleItem"
        embedded
        @close="drawerOpen = false"
      />
    </div>
  </main>
</template>

<style scoped>
.case-atoms-page {
  min-height: 100vh;
  padding: 7px 20px 10px 15px;
  overflow: hidden;
  background: #ffffff;
}

.case-atoms-page > h1 {
  height: 24px;
  margin: 0;
  color: #172136;
  font-size: 17px;
  font-weight: 700;
  line-height: 24px;
}

.case-atoms-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 310px;
  align-items: start;
  gap: 8px;
}

.case-atoms-sheet {
  display: grid;
  grid-template-columns: 1.35fr 1.15fr 0.91fr 1.59fr;
  grid-template-rows: 69px 150px 133px;
  border: 1px solid var(--cm-line);
  border-radius: 5px;
  overflow: hidden;
  background: #ffffff;
}

.case-atom-cell {
  min-width: 0;
  min-height: 0;
  display: grid;
  align-content: start;
  gap: 7px;
  border-right: 1px solid var(--cm-line);
  border-bottom: 1px solid var(--cm-line);
  padding: 8px;
}

.case-atom-cell h2 {
  margin: 0;
  color: #39465c;
  font-size: 10px;
  font-weight: 600;
}

.case-atom-cell .case-atom-subtitle {
  margin-top: 4px;
}

.case-atom-cell--actions,
.case-atom-cell--status {
  min-height: 0;
  grid-column: span 2;
}

.case-atom-cell--status {
  border-right: 0;
}

.case-atom-row {
  display: flex;
  align-items: center;
  gap: 12px;
}

.case-atom-cell :deep(.cm-button) {
  min-height: 30px;
  padding: 5px 12px;
  font-size: 11px;
}

.case-atom-cell :deep(.cm-status-chip) {
  min-height: 27px;
  padding: 5px 10px;
  font-size: 10px;
}

.case-atom-cell :deep(.cm-interval-options) {
  gap: 6px;
}

.case-atom-cell :deep(.cm-interval-option) {
  min-width: 63px;
}

.case-atom-cell :deep(.cm-segmented__option) {
  min-width: 0;
  padding-inline: 8px;
}

.case-toggle-samples {
  display: flex;
  align-items: center;
  gap: 28px;
}

.case-atom-cell :deep(.cm-search + .cm-search) {
  margin-top: 8px;
}

.case-atom-cell :deep(.cm-category-tabs) {
  gap: 3px;
  overflow: visible;
}

.case-atom-cell :deep(.cm-category-tab) {
  min-height: 28px;
  gap: 4px;
  padding: 5px 6px;
  font-size: 9.5px;
}

.case-pagination-samples {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 12px;
}

.case-pagination-stack {
  display: grid;
  gap: 3px;
}

.case-pagination-samples > div:last-child {
  display: grid;
  align-content: start;
  justify-items: end;
  gap: 4px;
}

.case-pagination-samples > div:last-child .cm-page-size {
  width: 90px;
  padding-inline: 8px 20px;
  font-size: 10px;
}

.case-pagination-samples :deep(.cm-pagination) {
  min-height: 30px;
  padding: 0;
}

.case-pagination-samples :deep(.cm-pagination > span),
.case-pagination-samples :deep(.cm-pagination .cm-page-size) {
  display: none;
}

.case-pagination-samples :deep(.cm-pagination__actions) {
  gap: 5px;
}

.case-pagination-samples :deep(.cm-page-button) {
  min-width: 25px;
  height: 25px;
}

.case-atom-cell--rows {
  min-height: 0;
}

.case-atom-cell--rows :deep(.cm-recommendation-row) {
  grid-template-columns: 42px minmax(130px, 2.2fr) 70px 75px 55px 48px 48px 82px 82px 22px;
  min-height: 34px;
  font-size: 9px;
}

.case-atoms-bottom {
  min-width: 0;
  display: grid;
  grid-column: 1 / -1;
  grid-template-columns: 63.3% 25.7% 11%;
}

.case-atom-cell--feedback {
  min-height: 0;
}

.case-atom-cell--feedback :deep(.cm-feedback) {
  min-height: 38px;
  padding: 7px 9px;
  font-size: 10px;
}

.case-atom-cell--feedback :deep(.cm-feedback__actions) {
  display: none;
}

.case-atom-cell--trigger {
  border-right: 0;
}

.case-atoms-layout :deep(.cm-detail-drawer--embedded) {
  width: 310px;
  margin-top: -22px;
}

@media (max-width: 1100px) {
  .case-atoms-page {
    overflow: auto;
  }

  .case-atoms-layout {
    min-width: 1340px;
  }
}
</style>
