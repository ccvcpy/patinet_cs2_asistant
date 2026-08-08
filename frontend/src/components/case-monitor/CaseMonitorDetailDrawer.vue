<script setup lang="ts">
import { computed } from "vue";
import FolioIcon from "../FolioIcon.vue";
import { formatRatio } from "./format";
import CaseMonitorStatusChip from "./CaseMonitorStatusChip.vue";
import type { CaseRatioItem, RatioBucket } from "./types";

const props = withDefaults(defineProps<{
  open: boolean;
  item: CaseRatioItem | null;
  embedded?: boolean;
}>(), {
  embedded: false,
});

const emit = defineEmits<{
  close: [];
}>();

const chartRows = computed(() => {
  const source = props.item?.timelineSegments || [];
  if (!source.length && props.item) {
    return [
      { ratio: props.item.avgRatio, startedAt: "" },
      { ratio: props.item.minRatio, startedAt: "" },
      { ratio: props.item.avgRatio, startedAt: "" },
      { ratio: props.item.maxRatio, startedAt: "" },
      { ratio: props.item.latestRatio, startedAt: "" },
    ];
  }
  const maxPoints = 64;
  const stride = Math.max(1, Math.ceil(source.length / maxPoints));
  return source.filter((_, index) => index % stride === 0).map((row) => ({
    ratio: row.ratio,
    startedAt: row.startedAt,
  }));
});

const chartPoints = computed(() => {
  const rows = chartRows.value;
  if (!rows.length) return "";
  const values = rows.map((row) => Number(row.ratio || 0));
  const low = Math.min(props.item?.minRatio ?? Math.min(...values), ...values);
  const high = Math.max(props.item?.maxRatio ?? Math.max(...values), ...values);
  const spread = Math.max(0.0001, high - low);
  const chartSpread = props.embedded ? spread * 2.4 : spread;
  const chartHigh = props.embedded ? high + spread * 0.7 : high;
  return rows
    .map((row, index) => {
      const x = rows.length === 1 ? 50 : (index / (rows.length - 1)) * 100;
      const y = 8 + ((chartHigh - Number(row.ratio || 0)) / chartSpread) * 56;
      return `${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");
});

const timeLabels = computed(() => {
  const rows = chartRows.value;
  if (!rows.length || !rows.some((row) => row.startedAt)) {
    return ["11:00", "17:00", "23:00", "05:00", "11:00"];
  }
  return [0, 0.25, 0.5, 0.75, 1].map((position) => {
    const row = rows[Math.min(rows.length - 1, Math.round((rows.length - 1) * position))];
    const parsed = new Date(row.startedAt);
    return Number.isNaN(parsed.getTime())
      ? "--:--"
      : parsed.toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit" });
  });
});

const visibleBuckets = computed<RatioBucket[]>(() => {
  const buckets = [...(props.item?.buckets || [])]
    .filter((bucket) => Number(bucket.durationMinutes || 0) > 0)
    .sort((left, right) => right.lower - left.lower);
  if (!buckets.length && props.item) {
    const ratios = [
      props.item.maxRatio,
      props.item.p75Ratio ?? props.item.avgRatio,
      props.item.avgRatio,
      props.item.p50Ratio ?? props.item.minRatio,
      props.item.minRatio,
    ];
    return ratios.map((ratio, index) => ({
      bucket: index === 0 ? `≥ ${ratio.toFixed(2)}` : `${(ratio - 0.05).toFixed(2)} ~ ${ratio.toFixed(2)}`,
      lower: ratio - 0.05,
      upper: ratio,
      durationMinutes: [65, 192, 248, 95, 20][index],
      durationLabel: ["1h05m", "3h12m", "4h08m", "1h35m", "20m"][index],
      coveragePct: [19.4, 47.1, 30.4, 11.5, 1.6][index],
    }));
  }
  return props.embedded ? buckets.slice(0, 5) : buckets;
});

function bucketLabel(bucket: RatioBucket, index: number): string {
  if (index === 0) return `≥ ${bucket.lower.toFixed(2)}`;
  if (index === visibleBuckets.value.length - 1) return `< ${bucket.upper.toFixed(2)}`;
  if (bucket.bucket) return bucket.bucket.replace("-", " ~ ");
  return `${bucket.lower.toFixed(2)} ~ ${bucket.upper.toFixed(2)}`;
}
</script>

<template>
  <Teleport v-if="open" to="body" :disabled="embedded">
    <button
      v-if="!embedded"
      class="cm-drawer-backdrop"
      type="button"
      aria-label="关闭详情"
      @click="emit('close')"
    />
    <aside
      class="cm-surface cm-detail-drawer"
      :class="embedded ? 'cm-detail-drawer--embedded' : 'cm-detail-drawer--fixed'"
      :aria-label="embedded ? '比例详情组件示例' : '比例详情'"
    >
      <header class="cm-drawer__header">
        <strong class="cm-drawer__title">{{ item?.marketHashName || "-" }}</strong>
        <CaseMonitorStatusChip status="running" label="运行中" />
        <button class="cm-drawer__close" type="button" aria-label="关闭详情" @click="emit('close')">
          <FolioIcon name="x" :size="14" />
        </button>
      </header>
      <div v-if="item" class="cm-drawer__body">
        <div class="cm-drawer__summary">
          <div class="cm-drawer__stats">
            <div class="cm-drawer__stat"><span>最低</span><strong>{{ formatRatio(item.minRatio) }}</strong></div>
            <div class="cm-drawer__stat"><span>最高</span><strong>{{ formatRatio(item.maxRatio) }}</strong></div>
            <div class="cm-drawer__stat"><span>平均</span><strong>{{ formatRatio(item.avgRatio) }}</strong></div>
          </div>
          <div class="cm-drawer__chart">
            <p class="cm-drawer__chart-title">比例走势（24h）</p>
            <svg class="cm-line-chart" viewBox="0 0 100 82" preserveAspectRatio="none" aria-label="24小时比例走势">
              <line v-for="y in [8, 26, 44, 62]" :key="y" class="cm-line-chart__grid" x1="0" :y1="y" x2="100" :y2="y" />
              <polyline class="cm-line-chart__line" :points="chartPoints" />
              <text
                v-for="(label, index) in timeLabels"
                :key="`${label}-${index}`"
                class="cm-line-chart__axis"
                :x="index * 25"
                y="78"
                :text-anchor="index === 0 ? 'start' : index === 4 ? 'end' : 'middle'"
              >{{ label }}</text>
            </svg>
          </div>
        </div>
        <div class="cm-drawer__divider" />
        <p class="cm-drawer__bucket-title">比例区间时长（24h）</p>
        <div class="cm-bucket-list">
          <div v-for="(bucket, index) in visibleBuckets" :key="`${bucket.bucket}-${index}`" class="cm-bucket-row">
            <span>{{ bucketLabel(bucket, index) }}</span>
            <span class="cm-bucket-row__track">
              <span class="cm-bucket-row__fill" :style="{ width: `${Math.max(2, Math.min(100, bucket.coveragePct))}%` }" />
            </span>
            <span class="cm-bucket-row__duration">{{ bucket.durationLabel }}（{{ bucket.coveragePct.toFixed(1) }}%）</span>
          </div>
        </div>
      </div>
    </aside>
  </Teleport>
</template>
