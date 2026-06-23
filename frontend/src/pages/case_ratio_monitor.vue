<script setup lang="ts">
import { computed, onMounted, ref } from "vue";

type RatioBucket = {
  bucket: string;
  lower: number;
  upper: number;
  durationMinutes: number;
  durationLabel: string;
  coveragePct: number;
};

type RatioThreshold = {
  key: string;
  label: string;
  ratio: number;
  durationLabel: string;
  coveragePct: number;
};

type TimelineSegment = {
  startedAt: string;
  endedAt: string;
  ratio: number;
  bucket: string;
  durationLabel: string;
  leftPct: number;
  widthPct: number;
};

type CaseRatioItem = {
  marketHashName: string;
  name: string;
  crateType: string;
  crateTypeLabel: string;
  sampleCount: number;
  okSampleCount: number;
  latestRatio: number;
  latestC5SellPrice: number | null;
  latestSteamListPrice: number | null;
  latestSteamAfterTaxPrice: number | null;
  minRatio: number;
  minRatioDurationLabel: string;
  maxRatio: number;
  maxRatioDurationLabel: string;
  avgRatio: number;
  p50Ratio: number | null;
  p75Ratio: number | null;
  p90Ratio: number | null;
  conservativeMaxListingRatio: number;
  recommendedMaxListingRatio: number;
  aggressiveMaxListingRatio: number;
  effectiveRecommendedMaxListingRatio: number | null;
  selectedReferenceRatio: number | null;
  steamReferenceSource: string | null;
  steamReferenceSourceLabel: string | null;
  steamReferencePrice: number | null;
  sellerFloorPrice: number | null;
  sellerWallListPrice: number | null;
  buyerMaxPrice: number | null;
  steamVolume24h: number | null;
  steamVolume7d: number | null;
  steamAvgDailyVolume7d: number | null;
  liquidityLabel: string | null;
  stddevRatio: number;
  coveragePct: number;
  recommendationScore: number;
  legacySteamMinorUnitCorrectedCount: number;
  buckets: RatioBucket[];
  dominantBuckets: RatioBucket[];
  ratioThresholds: RatioThreshold[];
  timelineSegments: TimelineSegment[];
};

type CaseRatioReport = {
  generatedAt: string;
  startUtc: string;
  endUtc: string;
  rangeHours: number;
  snapshotCount: number;
  itemCount: number;
  statusCounts: Record<string, number>;
  crateTypeCounts: Record<string, number>;
  crateTypeLabels: Record<string, string>;
  recommendationCrateType: string;
  legacySteamMinorUnitCorrectedCount: number;
  steamLiquidityStatus: string;
  steamLiquidityRefreshedAt: string | null;
  recommendations: CaseRatioItem[];
  items: CaseRatioItem[];
};

const typeOrder = ["weapon_case", "capsule", "souvenir_package", "container", "crate", "other"];

const loading = ref(true);
const error = ref("");
const report = ref<CaseRatioReport | null>(null);
const keyword = ref("");
const selected = ref("");
const typeFilter = ref("all");

const typeOptions = computed(() => {
  const counts = report.value?.crateTypeCounts ?? {};
  const labels = report.value?.crateTypeLabels ?? {};
  const present = Object.keys(counts).filter((key) => counts[key] > 0);
  const keys = typeOrder.filter((key) => present.includes(key));
  for (const key of present) {
    if (!keys.includes(key)) keys.push(key);
  }
  return [
    { key: "all", label: "全部", count: report.value?.itemCount ?? 0 },
    ...keys.map((key) => ({ key, label: labels[key] ?? key, count: counts[key] ?? 0 })),
  ];
});

const visibleItems = computed(() => {
  const source = report.value?.items ?? [];
  const needle = keyword.value.trim().toLowerCase();
  return source.filter((item) => {
    const typeMatched = typeFilter.value === "all" || item.crateType === typeFilter.value;
    const textMatched =
      !needle || `${item.marketHashName} ${item.name} ${item.crateTypeLabel}`.toLowerCase().includes(needle);
    return typeMatched && textMatched;
  });
});

const rankedItems = computed(() =>
  [...visibleItems.value].sort(
    (left, right) =>
      right.recommendationScore - left.recommendationScore ||
      (left.effectiveRecommendedMaxListingRatio ?? left.recommendedMaxListingRatio) -
        (right.effectiveRecommendedMaxListingRatio ?? right.recommendedMaxListingRatio) ||
      (right.steamVolume24h ?? 0) - (left.steamVolume24h ?? 0) ||
      right.coveragePct - left.coveragePct ||
      left.marketHashName.localeCompare(right.marketHashName),
  ),
);

const selectedItem = computed(() => {
  if (selected.value) {
    const found = visibleItems.value.find((item) => item.marketHashName === selected.value);
    if (found) return found;
  }
  return rankedItems.value[0] ?? visibleItems.value[0];
});

const okSnapshotCount = computed(() => report.value?.statusCounts?.ok ?? 0);
const missingSnapshotCount = computed(() => {
  const counts = report.value?.statusCounts ?? {};
  return Object.entries(counts)
    .filter(([key]) => key !== "ok")
    .reduce((sum, [, count]) => sum + count, 0);
});

function formatRatio(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  return value.toFixed(4);
}

function formatMoney(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  const decimals = Math.abs(value) < 1 ? 3 : 2;
  return `CNY ${value.toFixed(decimals)}`;
}

function formatPct(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  return `${value.toFixed(2)}%`;
}

function formatInt(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  return Math.round(value).toLocaleString("zh-CN");
}

function formatTime(value: string): string {
  if (!value) return "-";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("zh-CN", { hour12: false });
}

function ratioColor(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "#6f7d8c";
  if (value <= 0.7) return "#2f7d5b";
  if (value <= 0.75) return "#3f7f94";
  if (value <= 0.8) return "#a77b2f";
  return "#b5534b";
}

function chooseType(key: string): void {
  typeFilter.value = key;
  selected.value = "";
}

function chooseItem(item: CaseRatioItem): void {
  selected.value = item.marketHashName;
}

function timelineStyle(segment: TimelineSegment): Record<string, string> {
  return {
    left: `${Math.max(0, Math.min(100, segment.leftPct))}%`,
    width: `${Math.max(0.25, Math.min(100, segment.widthPct))}%`,
    background: ratioColor(segment.ratio),
  };
}

async function loadReport(): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    const response = await fetch("/guadao_case_ratio_report.json", { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    report.value = (await response.json()) as CaseRatioReport;
  } catch (exc) {
    report.value = null;
    error.value = exc instanceof Error ? exc.message : String(exc);
  } finally {
    loading.value = false;
  }
}

onMounted(loadReport);
</script>

<template>
  <main class="page case-monitor-page">
    <header class="page-header">
      <div>
        <p class="eyebrow">Guadao Ratio Monitor</p>
        <h1>箱子挂刀比监控</h1>
      </div>
      <button class="primary-button" type="button" @click="loadReport">刷新报告</button>
    </header>

    <section v-if="loading" class="panel empty-panel">正在读取报告...</section>
    <section v-else-if="error" class="panel empty-panel">
      未找到可视化数据：{{ error }}
    </section>

    <template v-else-if="report">
      <section class="metrics-grid compact">
        <article class="metric-card">
          <span>报告范围</span>
          <strong>{{ report.rangeHours }}h</strong>
        </article>
        <article class="metric-card">
          <span>有效快照</span>
          <strong>{{ okSnapshotCount }}</strong>
        </article>
        <article class="metric-card">
          <span>缺价快照</span>
          <strong>{{ missingSnapshotCount }}</strong>
        </article>
        <article class="metric-card">
          <span>旧价格修正</span>
          <strong>{{ report.legacySteamMinorUnitCorrectedCount }}</strong>
        </article>
      </section>

      <section class="panel report-meta case-report-meta">
        <div>
          <span class="soft-label">开始</span>
          <strong>{{ formatTime(report.startUtc) }}</strong>
        </div>
        <div>
          <span class="soft-label">结束</span>
          <strong>{{ formatTime(report.endUtc) }}</strong>
        </div>
        <div>
          <span class="soft-label">生成</span>
          <strong>{{ formatTime(report.generatedAt) }}</strong>
        </div>
        <div>
          <span class="soft-label">流动性</span>
          <strong>{{ report.steamLiquidityStatus || "-" }}</strong>
        </div>
        <label>
          <span class="soft-label">筛选</span>
          <input v-model="keyword" type="search" placeholder="market hash name" />
        </label>
      </section>

      <section class="panel case-filter-panel">
        <div class="segmented-control" aria-label="箱子类别">
          <button
            v-for="option in typeOptions"
            :key="option.key"
            type="button"
            class="segment-button"
            :class="{ active: typeFilter === option.key }"
            @click="chooseType(option.key)"
          >
            <span>{{ option.label }}</span>
            <strong>{{ option.count }}</strong>
          </button>
        </div>
      </section>

      <section class="panel">
        <div class="panel-title-row">
          <h2>推荐排行</h2>
          <span class="soft-label">{{ visibleItems.length }} 个条目，建议比例已考虑成交量和采用源</span>
        </div>
        <div class="recommendation-list">
          <button
            v-for="(item, index) in rankedItems.slice(0, 10)"
            :key="item.marketHashName"
            class="recommendation-row case-recommendation-row"
            :class="{ active: selectedItem?.marketHashName === item.marketHashName }"
            type="button"
            @click="chooseItem(item)"
          >
            <span class="rank">{{ index + 1 }}</span>
            <span class="item-name">
              {{ item.marketHashName }}
              <small>{{ item.crateTypeLabel }}</small>
            </span>
            <span>{{ formatRatio(item.effectiveRecommendedMaxListingRatio ?? item.recommendedMaxListingRatio) }}</span>
            <span>{{ item.steamReferenceSourceLabel ?? "20墙" }}</span>
            <span>{{ formatInt(item.steamVolume24h) }}</span>
            <span>{{ item.liquidityLabel ?? "-" }}</span>
          </button>
        </div>
      </section>

      <section v-if="selectedItem" class="panel focus-panel">
        <div class="focus-heading">
          <div>
            <p class="eyebrow">{{ selectedItem.crateTypeLabel }}</p>
            <h2>{{ selectedItem.marketHashName }}</h2>
          </div>
          <strong>{{ formatRatio(selectedItem.effectiveRecommendedMaxListingRatio ?? selectedItem.recommendedMaxListingRatio) }}</strong>
        </div>

        <div class="focus-grid case-focus-grid">
          <div>
            <span class="soft-label">建议比例</span>
            <strong>{{ formatRatio(selectedItem.effectiveRecommendedMaxListingRatio ?? selectedItem.recommendedMaxListingRatio) }}</strong>
          </div>
          <div>
            <span class="soft-label">采用源</span>
            <strong>{{ selectedItem.steamReferenceSourceLabel ?? "20墙挂价" }}</strong>
          </div>
          <div>
            <span class="soft-label">参考价格</span>
            <strong>{{ formatMoney(selectedItem.steamReferencePrice ?? selectedItem.latestSteamListPrice) }}</strong>
          </div>
          <div>
            <span class="soft-label">24h成交量</span>
            <strong>{{ formatInt(selectedItem.steamVolume24h) }} / {{ selectedItem.liquidityLabel ?? "-" }}</strong>
          </div>
          <div>
            <span class="soft-label">C5最低在售</span>
            <strong>{{ formatMoney(selectedItem.latestC5SellPrice) }}</strong>
          </div>
          <div>
            <span class="soft-label">20墙挂价</span>
            <strong>{{ formatMoney(selectedItem.sellerWallListPrice ?? selectedItem.latestSteamListPrice) }}</strong>
          </div>
        </div>

        <div class="price-source-grid">
          <div>
            <span class="soft-label">最低在售</span>
            <strong>{{ formatMoney(selectedItem.sellerFloorPrice) }}</strong>
          </div>
          <div>
            <span class="soft-label">20墙挂价</span>
            <strong>{{ formatMoney(selectedItem.sellerWallListPrice ?? selectedItem.latestSteamListPrice) }}</strong>
          </div>
          <div>
            <span class="soft-label">最高求购</span>
            <strong>{{ formatMoney(selectedItem.buyerMaxPrice) }}</strong>
          </div>
          <div>
            <span class="soft-label">7d日均成交</span>
            <strong>{{ formatInt(selectedItem.steamAvgDailyVolume7d) }}</strong>
          </div>
        </div>

        <div class="threshold-grid">
          <article
            v-for="threshold in selectedItem.ratioThresholds"
            :key="threshold.key"
            class="threshold-card"
          >
            <span>{{ threshold.label }}</span>
            <strong>{{ formatRatio(threshold.ratio) }}</strong>
            <em>{{ threshold.durationLabel }} / {{ formatPct(threshold.coveragePct) }}</em>
          </article>
        </div>

        <div class="ratio-timeline" aria-label="比例时间线">
          <span
            v-for="segment in selectedItem.timelineSegments"
            :key="`${segment.startedAt}-${segment.ratio}`"
            class="timeline-segment"
            :style="timelineStyle(segment)"
            :title="`${formatTime(segment.startedAt)} - ${formatTime(segment.endedAt)} | ${formatRatio(segment.ratio)} | ${segment.durationLabel}`"
          />
        </div>

        <div class="bucket-bars detailed-buckets">
          <div v-for="bucket in selectedItem.buckets" :key="bucket.bucket" class="bucket-row detailed-bucket-row">
            <span>{{ bucket.bucket }}</span>
            <div class="bar-track">
              <div
                class="bar-fill"
                :style="{ width: `${Math.max(1, bucket.coveragePct)}%`, background: ratioColor(bucket.lower) }"
              />
            </div>
            <strong>{{ bucket.durationLabel }}</strong>
            <em>{{ formatPct(bucket.coveragePct) }}</em>
          </div>
        </div>
      </section>

      <section class="panel">
        <div class="table-wrap">
          <table class="data-table case-ratio-table">
            <thead>
              <tr>
                <th>类别</th>
                <th>箱子</th>
                <th>建议</th>
                <th>采用源</th>
                <th>24h量</th>
                <th>速度</th>
                <th>稳健墙</th>
                <th>当前</th>
                <th>最低/持续</th>
                <th>最高/持续</th>
                <th>低卖/求购</th>
                <th>C5</th>
                <th>Steam挂价</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in visibleItems" :key="item.marketHashName" @click="chooseItem(item)">
                <td>{{ item.crateTypeLabel }}</td>
                <td>{{ item.marketHashName }}</td>
                <td>{{ formatRatio(item.effectiveRecommendedMaxListingRatio ?? item.recommendedMaxListingRatio) }}</td>
                <td>{{ item.steamReferenceSourceLabel ?? "20墙" }}</td>
                <td>{{ formatInt(item.steamVolume24h) }}</td>
                <td>{{ item.liquidityLabel ?? "-" }}</td>
                <td>{{ formatRatio(item.recommendedMaxListingRatio) }}</td>
                <td>{{ formatRatio(item.latestRatio) }}</td>
                <td>{{ formatRatio(item.minRatio) }} / {{ item.minRatioDurationLabel }}</td>
                <td>{{ formatRatio(item.maxRatio) }} / {{ item.maxRatioDurationLabel }}</td>
                <td>{{ formatMoney(item.sellerFloorPrice) }} / {{ formatMoney(item.buyerMaxPrice) }}</td>
                <td>{{ formatMoney(item.latestC5SellPrice) }}</td>
                <td>{{ formatMoney(item.sellerWallListPrice ?? item.latestSteamListPrice) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>
  </main>
</template>
