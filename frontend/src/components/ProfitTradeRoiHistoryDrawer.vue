<script setup lang="ts">
import { computed, ref } from "vue";
import { formatProfitTradeRatio } from "./profit_trade_roi_format";
import type {
  ProfitTradeHistoryRange,
  ProfitTradeHistoryStats,
  ProfitTradeHistoryTrend,
  ProfitTradeHistoryTrendPoint,
  ProfitTradeSteamOrderbook,
  ProfitTradeWatchHistoryItem,
  ProfitTradeWatchItem,
  ProfitTradeWatchPool,
} from "./profit_trade_roi_types";

const props = withDefaults(defineProps<{
  selected: ProfitTradeWatchItem | null;
  pool?: ProfitTradeWatchPool;
  history?: ProfitTradeWatchHistoryItem[];
  total?: number;
  page?: number;
  pages?: number;
  loading?: boolean;
  error?: string;
  stats?: ProfitTradeHistoryStats | null;
  trend?: ProfitTradeHistoryTrend | null;
  range?: ProfitTradeHistoryRange;
}>(), {
  pool: "inventory",
  history: () => [],
  total: 0,
  page: 1,
  pages: 1,
  loading: false,
  error: "",
  stats: null,
  trend: () => ({ totalValidPoints: 0, sampled: false, points: [] }),
  range: "7d",
});

const emit = defineEmits<{
  close: [];
  "change-range": [range: ProfitTradeHistoryRange];
  "change-page": [direction: -1 | 1];
}>();

const expandedHistoryKey = ref("");
const activeTrendIndex = ref<number | null>(null);
const poolTitle = computed(() => props.pool === "selection" ? "全市场选品历史" : "库存做T历史");
const poolDescription = computed(() => props.pool === "selection"
  ? "仅研究记录，不会创建真实流水或买入。"
  : "该历史来自库存品类的真实行情观察。",
);

const CHART_WIDTH = 660;
const CHART_HEIGHT = 248;
const CHART_LEFT = 58;
const CHART_RIGHT = 18;
const CHART_TOP = 22;
const CHART_BOTTOM = 40;

type RenderTrendPoint = ProfitTradeHistoryTrendPoint & {
  timestamp: number;
  x: number;
  y: number;
  buyY: number | null;
};

const chartModel = computed(() => {
  const source = (props.trend?.points || [])
    .map((point) => ({ ...point, timestamp: new Date(point.observedAt).getTime() }))
    .filter((point) => Number.isFinite(point.timestamp) && Number.isFinite(point.expectedRoi))
    .sort((left, right) => left.timestamp - right.timestamp);
  if (source.length === 0) {
    return {
      points: [] as RenderTrendPoint[],
      mainPolyline: "",
      buySegments: [] as RenderTrendPoint[][],
      yTicks: [] as { value: number; y: number }[],
      xTicks: [] as { label: string; x: number }[],
      zeroY: null as number | null,
      hasBuyReference: false,
    };
  }

  const values = source.flatMap((point) => [
    point.expectedRoi,
    ...(typeof point.buyOrderReferenceRoi === "number" && Number.isFinite(point.buyOrderReferenceRoi)
      ? [point.buyOrderReferenceRoi]
      : []),
  ]);
  const rawMin = Math.min(...values);
  const rawMax = Math.max(...values);
  const padding = rawMin === rawMax
    ? Math.max(Math.abs(rawMin) * 0.12, 0.01)
    : Math.max((rawMax - rawMin) * 0.12, 0.002);
  const yMin = rawMin - padding;
  const yMax = rawMax + padding;
  const plotWidth = CHART_WIDTH - CHART_LEFT - CHART_RIGHT;
  const plotHeight = CHART_HEIGHT - CHART_TOP - CHART_BOTTOM;
  const firstTime = source[0].timestamp;
  const lastTime = source[source.length - 1].timestamp;
  const timeSpan = lastTime - firstTime;
  const xFor = (timestamp: number, index: number) => source.length === 1
    ? CHART_LEFT + plotWidth / 2
    : CHART_LEFT + (
      timeSpan > 0
        ? ((timestamp - firstTime) / timeSpan) * plotWidth
        : (index / (source.length - 1)) * plotWidth
    );
  const yFor = (value: number) => CHART_TOP + ((yMax - value) / (yMax - yMin)) * plotHeight;
  const points: RenderTrendPoint[] = source.map((point, index) => ({
    ...point,
    x: xFor(point.timestamp, index),
    y: yFor(point.expectedRoi),
    buyY: typeof point.buyOrderReferenceRoi === "number" && Number.isFinite(point.buyOrderReferenceRoi)
      ? yFor(point.buyOrderReferenceRoi)
      : null,
  }));
  const buySegments: RenderTrendPoint[][] = [];
  let currentSegment: RenderTrendPoint[] = [];
  for (const point of points) {
    if (point.buyY == null) {
      if (currentSegment.length) buySegments.push(currentSegment);
      currentSegment = [];
      continue;
    }
    currentSegment.push(point);
  }
  if (currentSegment.length) buySegments.push(currentSegment);
  const yTicks = [yMax, (yMax + yMin) / 2, yMin].map((value) => ({ value, y: yFor(value) }));
  const tickIndexes = source.length === 1
    ? [0]
    : Array.from(new Set([0, Math.floor((source.length - 1) / 2), source.length - 1]));
  const showTime = timeSpan < 24 * 60 * 60 * 1000;
  const xTicks = tickIndexes.map((index) => ({
    label: trendTickTime(source[index].observedAt, showTime),
    x: points[index].x,
  }));
  return {
    points,
    mainPolyline: points.map((point) => `${point.x},${point.y}`).join(" "),
    buySegments,
    yTicks,
    xTicks,
    zeroY: yMin <= 0 && yMax >= 0 ? yFor(0) : null,
    hasBuyReference: buySegments.length > 0,
  };
});

const activeTrendPoint = computed(() => {
  const points = chartModel.value.points;
  if (!points.length) return null;
  const index = activeTrendIndex.value == null
    ? points.length - 1
    : Math.min(points.length - 1, Math.max(0, activeTrendIndex.value));
  return points[index];
});

function money(value?: number | null): string {
  return typeof value === "number" && Number.isFinite(value) ? `¥${value.toFixed(2)}` : "—";
}

function pct(value?: number | null): string {
  return formatProfitTradeRatio(value);
}

function time(value?: string | null): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleString("zh-CN", { hour12: false });
}

function trendTickTime(value: string, includeTime: boolean): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("zh-CN", includeTime
    ? { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }
    : { month: "2-digit", day: "2-digit" });
}

function rangeLabel(value: ProfitTradeHistoryRange): string {
  return value === "all" ? "全部" : `${value.slice(0, -1)}天`;
}

function trendSegmentPoints(segment: RenderTrendPoint[]): string {
  return segment.map((point) => `${point.x},${point.buyY}`).join(" ");
}

function activateTrendFromPointer(event: PointerEvent): void {
  const points = chartModel.value.points;
  if (!points.length) return;
  const element = event.currentTarget as SVGElement;
  const bounds = element.getBoundingClientRect();
  if (bounds.width <= 0) return;
  const chartX = ((event.clientX - bounds.left) / bounds.width) * CHART_WIDTH;
  let nearestIndex = 0;
  let nearestDistance = Number.POSITIVE_INFINITY;
  points.forEach((point, index) => {
    const distance = Math.abs(point.x - chartX);
    if (distance < nearestDistance) {
      nearestDistance = distance;
      nearestIndex = index;
    }
  });
  activeTrendIndex.value = nearestIndex;
}

function moveTrendFocus(direction: -1 | 1): void {
  const points = chartModel.value.points;
  if (!points.length) return;
  const current = activeTrendIndex.value == null ? points.length - 1 : activeTrendIndex.value;
  activeTrendIndex.value = Math.min(points.length - 1, Math.max(0, current + direction));
}

function focusTrendBoundary(position: "first" | "last"): void {
  const points = chartModel.value.points;
  if (!points.length) return;
  activeTrendIndex.value = position === "first" ? 0 : points.length - 1;
}

function orderbook(row: ProfitTradeWatchHistoryItem): ProfitTradeSteamOrderbook {
  return row.steamOrderbook || {
    sellerFloorPrice: row.steamBuyPrice ?? null,
    sellLevels: [],
    buyLevels: [],
  };
}

function compactListingId(value?: string | null): string {
  const text = String(value || "");
  return text.length > 22 ? `${text.slice(0, 10)}…${text.slice(-8)}` : text;
}

function listingProbeText(row: ProfitTradeWatchHistoryItem): string {
  const probe = row.crossedListingProbe;
  if (!probe) return "当时未抓取具体 listing 证据";
  const listingId = compactListingId(probe.listingId);
  if (probe.status === "matched") {
    return `找到 listing ${listingId} · 买家支付 ${money(probe.listingTotal)} · 与卖一一致`;
  }
  if (probe.status === "floor_mismatch") {
    return `找到 listing ${listingId} · 实际 ${money(probe.listingTotal)} · 与卖一不一致`;
  }
  if (probe.status === "empty") return "未找到公开 listing · 更像幽灵卖盘或尚未传播";
  if (probe.status === "no_usable_cny_listing") return "有 listing 返回，但没有可验证的人民币同物品卖单";
  if (probe.status === "rate_limited") return "查询具体 listing 返回 429 · 未重试，已进入冷却";
  if (probe.status === "circuit_open") return "listings 正在冷却 · 当时没有额外请求";
  return probe.message || "当时未取得可验证的 listing 证据";
}

function listingProbeClass(row: ProfitTradeWatchHistoryItem): string {
  const status = row.crossedListingProbe?.status || "missing";
  if (status === "matched") return "matched";
  if (status === "floor_mismatch") return "mismatch";
  return "unresolved";
}

function historyKey(event: ProfitTradeWatchHistoryItem, index: number): string {
  return `${event.scanId || event.observedAt || "history"}-${index}`;
}

function toggleDepth(event: ProfitTradeWatchHistoryItem, index: number): void {
  const key = historyKey(event, index);
  expandedHistoryKey.value = expandedHistoryKey.value === key ? "" : key;
}

function roiBasisLabel(stats?: ProfitTradeHistoryStats | null): string {
  if (!stats) return "—";
  const one = stats.roiBasis;
  const min = stats.roiBasisMin;
  const max = stats.roiBasisMax;
  if (typeof min === "number" && typeof max === "number") {
    return min === max ? pct(min) : `${pct(min)}～${pct(max)}`;
  }
  return pct(one);
}

function eventLabel(event: ProfitTradeWatchHistoryItem): string {
  if (event.eventType) return event.eventType;
  if (event.active === false) return "退出观察";
  return "观察";
}
</script>

<template>
  <Teleport to="body">
    <div v-if="selected" class="history-backdrop" @click.self="emit('close')">
    <aside class="history-drawer" aria-label="价格与 ROI 历史">
      <header>
        <div>
          <p class="eyebrow">{{ poolTitle }}</p>
          <h3>{{ selected.name || selected.marketHashName }}</h3>
          <small>{{ selected.marketHashName }}</small>
          <p class="drawer-description">{{ poolDescription }}</p>
        </div>
        <button type="button" aria-label="关闭" @click="emit('close')">×</button>
      </header>

      <div class="history-range" aria-label="历史时间范围">
        <button v-for="value in ['7d', '30d', '90d', 'all'] as ProfitTradeHistoryRange[]" :key="value" :class="{ active: range === value }" type="button" @click="emit('change-range', value)">
          {{ rangeLabel(value) }}
        </button>
      </div>

      <section class="history-overview" aria-label="历史统计与 ROI 趋势">
        <section class="history-stat-card" aria-label="历史统计">
          <header><strong>历史统计</strong><small>完整时间范围</small></header>
          <div class="history-stats">
            <div><small>历史最高 ROI</small><strong>{{ pct(stats?.highestRoi) }}</strong></div>
            <div><small>平均 ROI</small><strong>{{ pct(stats?.averageRoi) }}</strong></div>
            <div><small>ROI 基底</small><strong>{{ roiBasisLabel(stats) }}</strong></div>
            <div><small>有效观测</small><strong>{{ stats?.validObservationCount ?? 0 }}</strong></div>
          </div>
        </section>

        <section class="history-trend-card" aria-label="ROI 历史趋势">
          <header>
            <div><strong>ROI 历史趋势</strong><small>已保存行情，不会重新请求 Steam</small></div>
            <div class="trend-legend"><span class="main">当前 ROI</span><span v-if="chartModel.hasBuyReference" class="reference">求购参考 ROI</span></div>
          </header>
          <div v-if="chartModel.points.length" class="trend-chart-shell">
            <svg
              class="trend-chart"
              :viewBox="`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`"
              role="img"
              tabindex="0"
              aria-label="ROI 历史折线图；可使用左右方向键查看每个观测点"
              @pointermove="activateTrendFromPointer"
              @pointerleave="activeTrendIndex = null"
              @focus="focusTrendBoundary('last')"
              @keydown.left.prevent="moveTrendFocus(-1)"
              @keydown.right.prevent="moveTrendFocus(1)"
              @keydown.home.prevent="focusTrendBoundary('first')"
              @keydown.end.prevent="focusTrendBoundary('last')"
            >
              <g class="trend-grid">
                <template v-for="tick in chartModel.yTicks" :key="tick.value">
                  <line :x1="CHART_LEFT" :x2="CHART_WIDTH - CHART_RIGHT" :y1="tick.y" :y2="tick.y" />
                  <text :x="CHART_LEFT - 7" :y="tick.y + 3" text-anchor="end">{{ pct(tick.value) }}</text>
                </template>
                <line v-if="chartModel.zeroY != null" class="zero-line" :x1="CHART_LEFT" :x2="CHART_WIDTH - CHART_RIGHT" :y1="chartModel.zeroY" :y2="chartModel.zeroY" />
                <text v-for="tick in chartModel.xTicks" :key="`${tick.x}-${tick.label}`" :x="tick.x" :y="CHART_HEIGHT - 7" text-anchor="middle">{{ tick.label }}</text>
              </g>
              <template v-for="(segment, index) in chartModel.buySegments" :key="`buy-segment-${index}`">
                <polyline v-if="segment.length > 1" class="trend-reference-line" :points="trendSegmentPoints(segment)" />
                <circle v-else class="trend-reference-point" :cx="segment[0].x" :cy="segment[0].buyY ?? 0" r="3" />
              </template>
              <polyline v-if="chartModel.points.length > 1" class="trend-main-line" :points="chartModel.mainPolyline" />
              <circle v-if="chartModel.points.length > 1" class="trend-main-start" :cx="chartModel.points[0].x" :cy="chartModel.points[0].y" r="2.8" />
              <circle v-if="chartModel.points.length > 1" class="trend-main-end" :cx="chartModel.points[chartModel.points.length - 1].x" :cy="chartModel.points[chartModel.points.length - 1].y" r="4.3" />
              <circle v-else class="trend-main-point" :cx="chartModel.points[0].x" :cy="chartModel.points[0].y" r="4.3" />
              <g v-if="activeTrendPoint" class="trend-active">
                <line :x1="activeTrendPoint.x" :x2="activeTrendPoint.x" :y1="CHART_TOP" :y2="CHART_HEIGHT - CHART_BOTTOM" />
                <circle :cx="activeTrendPoint.x" :cy="activeTrendPoint.y" r="4" />
                <circle v-if="activeTrendPoint.buyY != null" class="reference" :cx="activeTrendPoint.x" :cy="activeTrendPoint.buyY" r="3.5" />
              </g>
            </svg>
            <div v-if="activeTrendPoint" class="trend-inspector" aria-live="polite">
              <time>{{ time(activeTrendPoint.observedAt) }}</time>
              <span>当前 ROI <strong>{{ pct(activeTrendPoint.expectedRoi) }}</strong></span>
              <span>求购参考 <strong>{{ pct(activeTrendPoint.buyOrderReferenceRoi) }}</strong></span>
              <span>ROI 基底 <strong>{{ pct(activeTrendPoint.roiBasis) }}</strong></span>
            </div>
          </div>
          <div v-else class="trend-empty">当前时间范围暂无有效 ROI 历史</div>
          <small v-if="trend?.sampled" class="trend-sampled">从 {{ trend.totalValidPoints }} 条有效观测中保留 {{ trend.points.length }} 个关键趋势点</small>
        </section>
      </section>

      <p v-if="error" class="history-error">{{ error }}</p>
      <div v-if="loading" class="history-empty">正在读取历史…</div>
      <div v-else-if="history.length === 0" class="history-empty">当前时间范围没有已保存的观察历史。</div>
      <div v-else class="history-list">
        <article v-for="(event, index) in history" :key="historyKey(event, index)">
          <div class="event-head"><strong>{{ pct(event.expectedRoi) }}</strong><span>{{ eventLabel(event) }}</span></div>
          <div class="history-reference">
            求购参考 ROI {{ pct(event.buyOrderReferenceRoi) }} · ROI 基底 {{ pct(event.roiBasis ?? event.balanceDiscount) }}
          </div>
          <dl>
            <div><dt>Steam 买入</dt><dd>{{ money(orderbook(event).sellerFloorPrice ?? event.steamBuyPrice) }}</dd></div>
            <div><dt>Steam 最高求购</dt><dd>{{ money(orderbook(event).buyerMaxPrice) }}</dd></div>
            <div><dt>C5 挂价</dt><dd>{{ money(event.c5ListingPrice) }}</dd></div>
            <div><dt>C5 预计到手</dt><dd>{{ money(event.c5ExpectedNetPrice) }}</dd></div>
            <div><dt>观察时间</dt><dd>{{ time(event.observedAt || event.lastObservedAt) }}</dd></div>
          </dl>
          <p v-if="orderbook(event).crossed" class="history-crossed">盘口交叉 · 可能存在缓存或传播延迟</p>
          <div
            v-if="orderbook(event).crossed"
            :class="['history-listing-probe', listingProbeClass(event)]"
          >
            <strong>Listing 证据</strong>
            <span>{{ listingProbeText(event) }}</span>
          </div>
          <p v-else-if="orderbook(event).buyerMaxPrice == null" class="history-unrecorded">历史未记录买盘</p>
          <button class="depth-toggle" type="button" @click="toggleDepth(event, index)">
            {{ expandedHistoryKey === historyKey(event, index) ? "收起买卖前 5 档" : "查看买卖前 5 档" }}
          </button>
          <section v-if="expandedHistoryKey === historyKey(event, index)" class="depth-grid">
            <p v-if="!((orderbook(event).sellLevels?.length || 0) + (orderbook(event).buyLevels?.length || 0))" class="depth-empty">此条历史未保存档位；展开不会重新请求 Steam。</p>
            <template v-else>
              <div>
                <strong>卖盘前 5 档</strong>
                <p v-for="(level, levelIndex) in orderbook(event).sellLevels || []" :key="`sell-${levelIndex}`"><span>{{ levelIndex + 1 }}</span><b>{{ money(level.price) }}</b><small>× {{ level.count ?? "—" }}</small></p>
              </div>
              <div>
                <strong>买盘前 5 档</strong>
                <p v-for="(level, levelIndex) in orderbook(event).buyLevels || []" :key="`buy-${levelIndex}`"><span>{{ levelIndex + 1 }}</span><b>{{ money(level.price) }}</b><small>× {{ level.count ?? "—" }}</small></p>
              </div>
            </template>
          </section>
          <p v-if="event.executionReason || event.riskReason || event.exitReason || event.lastError" class="history-reason">{{ event.executionReason || event.riskReason || event.exitReason || event.lastError }}</p>
          <section v-if="event.relatedTrade && pool === 'inventory'" class="history-trade-link">
            <header><span>该快照已转化为真实流水</span><strong>{{ event.relatedTrade.tradeNo || event.relatedTrade.status || "状态同步中" }}</strong></header>
            <small>trade {{ event.relatedTrade.tradeId }}</small>
            <dl>
              <div><dt>Steam 实际买入</dt><dd>{{ money(event.relatedTrade.steamBuyPrice) }}</dd></div>
              <div><dt>买入时间</dt><dd>{{ time(event.relatedTrade.steamBoughtAt) }}</dd></div>
              <div><dt>C5 实际到手</dt><dd>{{ money(event.relatedTrade.c5SoldNetPrice) }}</dd></div>
              <div><dt>完成时间</dt><dd>{{ time(event.relatedTrade.completedAt) }}</dd></div>
              <div><dt>实际收益</dt><dd>{{ money(event.relatedTrade.realizedProfit) }}</dd></div>
              <div><dt>实际 ROI</dt><dd>{{ pct(event.relatedTrade.realizedRoi) }}</dd></div>
            </dl>
          </section>
        </article>
      </div>

      <footer class="pagination">
        <span>第 {{ page }} / {{ pages }} 页，共 {{ total }} 条</span>
        <div><button class="mini-action" type="button" :disabled="page <= 1" @click="emit('change-page', -1)">上一页</button><button class="mini-action" type="button" :disabled="page >= pages" @click="emit('change-page', 1)">下一页</button></div>
      </footer>
    </aside>
    </div>
  </Teleport>
</template>

<style scoped>
.history-backdrop{position:fixed;inset:0;z-index:40;display:flex;justify-content:flex-end;background:rgba(19,31,25,.25)}.history-drawer{width:min(860px,68vw);height:100%;overflow:auto;padding:22px;border-left:1px solid #dce3dc;background:#f7f9f6;box-shadow:-15px 0 35px rgba(21,47,34,.13)}.history-drawer>header{display:flex;justify-content:space-between;gap:20px;padding-bottom:14px;border-bottom:1px solid #dfe4df}.history-drawer h3{margin:0}.history-drawer header small{color:#768079}.history-drawer header button{width:32px;height:32px;border:1px solid #d9dfd9;border-radius:7px;background:#fff;font-size:19px}.drawer-description{margin:7px 0 0;color:#69766f;font-size:11px}.history-range{display:flex;gap:6px;margin:13px 0}.history-range button{border:1px solid #d9e1d9;border-radius:999px;padding:5px 9px;color:#617168;background:#fff;font-size:10px;font-weight:700}.history-range button.active{border-color:#8db89b;color:#176440;background:#eaf5ed}.history-overview{display:grid;grid-template-columns:minmax(210px,.72fr) minmax(0,1.28fr);gap:10px}.history-stat-card,.history-trend-card{min-width:0;border:1px solid #e0e7e1;border-radius:10px;padding:11px;background:#fff}.history-stat-card>header,.history-trend-card>header{display:flex;align-items:flex-start;justify-content:space-between;gap:9px;margin-bottom:9px}.history-stat-card>header strong,.history-trend-card>header strong{color:#25352c;font-size:11px}.history-stat-card>header small,.history-trend-card>header small{display:block;margin-top:2px;color:#7a847e;font-size:8px}.history-stats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.history-stats>div{display:grid;gap:3px;padding:9px;border:1px solid #e0e7e1;border-radius:8px;background:#fafcfa}.history-stats small{color:#748078;font-size:9px}.history-stats strong{color:#256b4b;font-size:14px}.trend-legend{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:7px;color:#66736b;font-size:8px}.trend-legend span{display:inline-flex;align-items:center;gap:4px;white-space:nowrap}.trend-legend span::before{width:13px;height:2px;content:"";background:#2d8559}.trend-legend .reference::before{height:0;border-top:2px dashed #8ca397;background:transparent}.trend-chart-shell{min-width:0}.trend-chart{display:block;width:100%;min-height:165px;border-radius:7px;background:#fbfdfb;outline:none}.trend-chart:focus-visible{box-shadow:0 0 0 2px rgba(45,133,89,.22)}.trend-grid line{stroke:#e8ede9;stroke-width:1}.trend-grid .zero-line{stroke:#b9c5bd;stroke-dasharray:4 4}.trend-grid text{fill:#7a857e;font-size:8px}.trend-main-line,.trend-reference-line{fill:none;stroke-linecap:round;stroke-linejoin:round}.trend-main-line{stroke:#2d8559;stroke-width:2.7}.trend-reference-line{stroke:#8ca397;stroke-width:1.7;stroke-dasharray:5 4}.trend-main-point{fill:#2d8559}.trend-reference-point{fill:#fff;stroke:#8ca397;stroke-width:2}.trend-active line{stroke:#8fa498;stroke-width:1;stroke-dasharray:3 3}.trend-active circle{fill:#fff;stroke:#2d8559;stroke-width:2}.trend-active circle.reference{stroke:#8ca397}.trend-inspector{display:flex;flex-wrap:wrap;gap:5px 11px;margin-top:4px;border-top:1px solid #edf1ed;padding-top:7px;color:#68766d;font-size:8px}.trend-inspector time{width:100%;color:#7b857f}.trend-inspector strong{color:#2a6e4d}.trend-empty{display:grid;place-items:center;min-height:170px;border:1px dashed #dce4dd;border-radius:7px;color:#77817b;background:#fbfdfb;font-size:10px}.trend-sampled{display:block;margin-top:6px;color:#7b857f;font-size:8px}.history-error{margin:10px 0;padding:9px 11px;border:1px solid #e7b8b2;border-radius:7px;color:#8d3d34;background:#fff7f5;font-size:12px}.history-empty{display:grid;place-items:center;min-height:90px;margin-top:12px;color:#77817b;border:1px dashed #d8ded8;border-radius:8px}.history-list{display:grid;gap:8px;margin:14px 0}.history-list article{padding:12px;border:1px solid #dfe5df;border-radius:8px;background:#fff}.event-head{display:flex;justify-content:space-between}.event-head strong{color:#236a4c;font-size:17px}.event-head span,.history-list dt{color:#79827d;font-size:11px}.history-reference{margin:6px 0;color:#5b7063;font-size:10px}.history-list dl{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.history-list dl>div{display:flex;justify-content:space-between;gap:8px}.history-list dd{margin:0;font-size:11px;font-weight:650;text-align:right}.history-crossed,.history-unrecorded,.history-reason{margin:8px 0;padding:6px 8px;border-radius:6px;font-size:10px}.history-crossed{color:#765a18;background:#faefc9}.history-unrecorded{color:#6f7873;background:#edf0ed}.history-reason{color:#745148;background:#fbf2ed}.depth-toggle{margin-top:6px;border:0;padding:0;color:#236a4c;background:transparent;font-size:10px;font-weight:700}.depth-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:8px}.depth-grid>div{padding:8px;border:1px solid #e1e6e1;border-radius:6px;background:#f8faf8}.depth-grid>div>strong{font-size:10px}.depth-grid p{display:grid;grid-template-columns:18px 1fr auto;gap:6px;margin:5px 0 0;color:#47534d;font-size:9px}.depth-grid p span,.depth-grid p small{color:#78827c}.depth-grid p b{text-align:right}.depth-empty{grid-column:1/-1;display:block !important;margin:0 !important;padding:8px;border:1px dashed #d8ded8;border-radius:6px;color:#748078 !important;font-size:10px !important}.history-trade-link{margin-top:10px;border:1px solid #c9dfd1;border-radius:9px;padding:10px;background:#f0f7f3}.history-trade-link>header{display:flex;align-items:center;justify-content:space-between;gap:10px}.history-trade-link>header span{color:#5e7769;font-size:10px;font-weight:700}.history-trade-link>header strong{color:#236a4c;font-size:12px}.history-trade-link>small{display:block;margin:3px 0 8px;color:#728078;font-size:9px}.history-trade-link dl{margin:0}.pagination{display:flex;justify-content:space-between;align-items:center;margin-top:13px;color:#6f7872;font-size:11px}.pagination>div{display:flex;gap:6px}@media (max-width:1000px){.history-overview{grid-template-columns:1fr}}@media (max-width:760px){.history-drawer{width:100%;padding:17px}.history-stats{grid-template-columns:1fr 1fr}.history-list dl{grid-template-columns:1fr}.depth-grid{grid-template-columns:1fr}.trend-chart{min-height:145px}}@media (max-width:460px){.history-stats{grid-template-columns:1fr}.history-trend-card>header{display:grid}.trend-legend{justify-content:flex-start}}
.history-backdrop{z-index:1200}
.history-listing-probe{display:grid;gap:2px;margin:-4px 0 8px;padding:7px 9px;border-left:3px solid #c9a548;border-radius:6px;color:#65552c;background:#fff8e6;font-size:10px;line-height:1.4}.history-listing-probe strong{font-size:9px}.history-listing-probe.matched{border-left-color:#4f9870;color:#315f45;background:#edf7f0}.history-listing-probe.mismatch{border-left-color:#d1874d;color:#744f2e;background:#fff4e9}
/* F2 Hairline Line 的时间轴与端点信息结构；按本项目 FOLIO 绿色体系提高可读性。 */
.history-trend-card{padding:14px;border-color:#dbe6de}
.history-trend-card>header{margin-bottom:12px}
.history-trend-card>header strong{color:var(--folio-ink);font-size:14px;font-weight:700;letter-spacing:-.01em}
.history-trend-card>header small{margin-top:4px;color:var(--folio-muted-strong);font-size:11px;font-weight:500;line-height:1.45}
.trend-legend{gap:11px;color:#4d6357;font-size:11px;font-weight:650}
.trend-legend span{gap:6px}
.trend-legend span::before{width:19px;height:3px;background:#19744d}
.trend-legend .reference::before{height:0;border-top:2px dashed #6f9481;background:transparent}
.trend-chart{min-height:205px;border:1px solid #edf2ee;border-radius:9px;background:#fbfdfb}
.trend-grid line{stroke:#dce6df;stroke-width:1}
.trend-grid .zero-line{stroke:#90a898;stroke-width:1.2;stroke-dasharray:5 4}
.trend-grid text{fill:#4e6056;font-size:10px;font-weight:650;paint-order:stroke;stroke:#fbfdfb;stroke-width:3px}
.trend-main-line{stroke:#19744d;stroke-width:3}
.trend-reference-line{stroke:#6f9481;stroke-width:2;stroke-dasharray:7 5}
.trend-main-start{fill:#fff;stroke:#19744d;stroke-width:2}
.trend-main-end,.trend-main-point{fill:#19744d;stroke:#fff;stroke-width:2}
.trend-reference-point{fill:#fff;stroke:#6f9481;stroke-width:2}
.trend-active line{stroke:#5c7d6c;stroke-width:1.2;stroke-dasharray:4 4}
.trend-active circle{stroke:#19744d;stroke-width:2.4}
.trend-active circle.reference{stroke:#6f9481}
.trend-inspector{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px 10px;margin-top:9px;border:1px solid #dbe7df;border-radius:8px;padding:9px 10px;color:#4b5e53;background:#f5f9f6;font-size:11px;font-weight:550;line-height:1.45}
.trend-inspector time{grid-column:1/-1;width:auto;color:#53655a;font-size:10px;font-weight:650}
.trend-inspector span{display:flex;justify-content:space-between;gap:6px;min-width:0}
.trend-inspector strong{color:#176a46;font-size:12px;font-weight:750;font-variant-numeric:tabular-nums;white-space:nowrap}
.trend-inspector span:nth-of-type(2) strong{color:#527565}
.trend-inspector span:nth-of-type(3) strong{color:#325a47}
.trend-empty{min-height:205px;color:var(--folio-muted-strong);font-size:11px;font-weight:550}
.trend-sampled{margin-top:8px;color:var(--folio-muted-strong);font-size:10px;font-weight:550;line-height:1.45}
@media(max-width:760px){.history-trend-card{padding:12px}.trend-chart{min-height:185px}.trend-inspector{grid-template-columns:1fr;font-size:11px}.trend-inspector time{grid-column:auto}}
</style>
