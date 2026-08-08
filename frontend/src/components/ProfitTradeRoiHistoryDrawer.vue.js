import { computed, ref } from "vue";
import { formatProfitTradeRatio } from "./profit_trade_roi_format";
const props = withDefaults(defineProps(), {
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
const emit = defineEmits();
const expandedHistoryKey = ref("");
const activeTrendIndex = ref(null);
const poolTitle = computed(() => props.pool === "selection" ? "全市场选品历史" : "库存做T历史");
const poolDescription = computed(() => props.pool === "selection"
    ? "仅研究记录，不会创建真实流水或买入。"
    : "该历史来自库存品类的真实行情观察。");
const CHART_WIDTH = 660;
const CHART_HEIGHT = 248;
const CHART_LEFT = 58;
const CHART_RIGHT = 18;
const CHART_TOP = 22;
const CHART_BOTTOM = 40;
const chartModel = computed(() => {
    const source = (props.trend?.points || [])
        .map((point) => ({ ...point, timestamp: new Date(point.observedAt).getTime() }))
        .filter((point) => Number.isFinite(point.timestamp) && Number.isFinite(point.expectedRoi))
        .sort((left, right) => left.timestamp - right.timestamp);
    if (source.length === 0) {
        return {
            points: [],
            mainPolyline: "",
            buySegments: [],
            yTicks: [],
            xTicks: [],
            zeroY: null,
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
    const xFor = (timestamp, index) => source.length === 1
        ? CHART_LEFT + plotWidth / 2
        : CHART_LEFT + (timeSpan > 0
            ? ((timestamp - firstTime) / timeSpan) * plotWidth
            : (index / (source.length - 1)) * plotWidth);
    const yFor = (value) => CHART_TOP + ((yMax - value) / (yMax - yMin)) * plotHeight;
    const points = source.map((point, index) => ({
        ...point,
        x: xFor(point.timestamp, index),
        y: yFor(point.expectedRoi),
        buyY: typeof point.buyOrderReferenceRoi === "number" && Number.isFinite(point.buyOrderReferenceRoi)
            ? yFor(point.buyOrderReferenceRoi)
            : null,
    }));
    const buySegments = [];
    let currentSegment = [];
    for (const point of points) {
        if (point.buyY == null) {
            if (currentSegment.length)
                buySegments.push(currentSegment);
            currentSegment = [];
            continue;
        }
        currentSegment.push(point);
    }
    if (currentSegment.length)
        buySegments.push(currentSegment);
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
    if (!points.length)
        return null;
    const index = activeTrendIndex.value == null
        ? points.length - 1
        : Math.min(points.length - 1, Math.max(0, activeTrendIndex.value));
    return points[index];
});
function money(value) {
    return typeof value === "number" && Number.isFinite(value) ? `¥${value.toFixed(2)}` : "—";
}
function pct(value) {
    return formatProfitTradeRatio(value);
}
function time(value) {
    if (!value)
        return "—";
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime())
        ? value
        : parsed.toLocaleString("zh-CN", { hour12: false });
}
function trendTickTime(value, includeTime) {
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime()))
        return value;
    return parsed.toLocaleString("zh-CN", includeTime
        ? { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }
        : { month: "2-digit", day: "2-digit" });
}
function rangeLabel(value) {
    return value === "all" ? "全部" : `${value.slice(0, -1)}天`;
}
function trendSegmentPoints(segment) {
    return segment.map((point) => `${point.x},${point.buyY}`).join(" ");
}
function activateTrendFromPointer(event) {
    const points = chartModel.value.points;
    if (!points.length)
        return;
    const element = event.currentTarget;
    const bounds = element.getBoundingClientRect();
    if (bounds.width <= 0)
        return;
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
function moveTrendFocus(direction) {
    const points = chartModel.value.points;
    if (!points.length)
        return;
    const current = activeTrendIndex.value == null ? points.length - 1 : activeTrendIndex.value;
    activeTrendIndex.value = Math.min(points.length - 1, Math.max(0, current + direction));
}
function focusTrendBoundary(position) {
    const points = chartModel.value.points;
    if (!points.length)
        return;
    activeTrendIndex.value = position === "first" ? 0 : points.length - 1;
}
function orderbook(row) {
    return row.steamOrderbook || {
        sellerFloorPrice: row.steamBuyPrice ?? null,
        sellLevels: [],
        buyLevels: [],
    };
}
function compactListingId(value) {
    const text = String(value || "");
    return text.length > 22 ? `${text.slice(0, 10)}…${text.slice(-8)}` : text;
}
function listingProbeText(row) {
    const probe = row.crossedListingProbe;
    if (!probe)
        return "当时未抓取具体 listing 证据";
    const listingId = compactListingId(probe.listingId);
    if (probe.status === "matched") {
        return `找到 listing ${listingId} · 买家支付 ${money(probe.listingTotal)} · 与卖一一致`;
    }
    if (probe.status === "floor_mismatch") {
        return `找到 listing ${listingId} · 实际 ${money(probe.listingTotal)} · 与卖一不一致`;
    }
    if (probe.status === "empty")
        return "未找到公开 listing · 更像幽灵卖盘或尚未传播";
    if (probe.status === "no_usable_cny_listing")
        return "有 listing 返回，但没有可验证的人民币同物品卖单";
    if (probe.status === "rate_limited")
        return "查询具体 listing 返回 429 · 未重试，已进入冷却";
    if (probe.status === "circuit_open")
        return "listings 正在冷却 · 当时没有额外请求";
    return probe.message || "当时未取得可验证的 listing 证据";
}
function listingProbeClass(row) {
    const status = row.crossedListingProbe?.status || "missing";
    if (status === "matched")
        return "matched";
    if (status === "floor_mismatch")
        return "mismatch";
    return "unresolved";
}
function historyKey(event, index) {
    return `${event.scanId || event.observedAt || "history"}-${index}`;
}
function toggleDepth(event, index) {
    const key = historyKey(event, index);
    expandedHistoryKey.value = expandedHistoryKey.value === key ? "" : key;
}
function roiBasisLabel(stats) {
    if (!stats)
        return "—";
    const one = stats.roiBasis;
    const min = stats.roiBasisMin;
    const max = stats.roiBasisMax;
    if (typeof min === "number" && typeof max === "number") {
        return min === max ? pct(min) : `${pct(min)}～${pct(max)}`;
    }
    return pct(one);
}
function eventLabel(event) {
    if (event.eventType)
        return event.eventType;
    if (event.active === false)
        return "退出观察";
    return "观察";
}
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_withDefaultsArg = (function (t) { return t; })({
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
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
/** @type {__VLS_StyleScopedClasses['history-drawer']} */ ;
/** @type {__VLS_StyleScopedClasses['history-drawer']} */ ;
/** @type {__VLS_StyleScopedClasses['history-drawer']} */ ;
/** @type {__VLS_StyleScopedClasses['history-drawer']} */ ;
/** @type {__VLS_StyleScopedClasses['history-range']} */ ;
/** @type {__VLS_StyleScopedClasses['history-range']} */ ;
/** @type {__VLS_StyleScopedClasses['history-stat-card']} */ ;
/** @type {__VLS_StyleScopedClasses['history-trend-card']} */ ;
/** @type {__VLS_StyleScopedClasses['history-stat-card']} */ ;
/** @type {__VLS_StyleScopedClasses['history-trend-card']} */ ;
/** @type {__VLS_StyleScopedClasses['history-stat-card']} */ ;
/** @type {__VLS_StyleScopedClasses['history-trend-card']} */ ;
/** @type {__VLS_StyleScopedClasses['history-stats']} */ ;
/** @type {__VLS_StyleScopedClasses['history-stats']} */ ;
/** @type {__VLS_StyleScopedClasses['history-stats']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-legend']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-legend']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-legend']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-chart']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-main-line']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-reference-line']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-active']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-active']} */ ;
/** @type {__VLS_StyleScopedClasses['reference']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-inspector']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-inspector']} */ ;
/** @type {__VLS_StyleScopedClasses['history-list']} */ ;
/** @type {__VLS_StyleScopedClasses['event-head']} */ ;
/** @type {__VLS_StyleScopedClasses['event-head']} */ ;
/** @type {__VLS_StyleScopedClasses['history-list']} */ ;
/** @type {__VLS_StyleScopedClasses['history-list']} */ ;
/** @type {__VLS_StyleScopedClasses['history-list']} */ ;
/** @type {__VLS_StyleScopedClasses['history-list']} */ ;
/** @type {__VLS_StyleScopedClasses['history-crossed']} */ ;
/** @type {__VLS_StyleScopedClasses['history-unrecorded']} */ ;
/** @type {__VLS_StyleScopedClasses['history-reason']} */ ;
/** @type {__VLS_StyleScopedClasses['depth-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['depth-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['depth-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['depth-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['depth-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['depth-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['history-trade-link']} */ ;
/** @type {__VLS_StyleScopedClasses['history-trade-link']} */ ;
/** @type {__VLS_StyleScopedClasses['history-trade-link']} */ ;
/** @type {__VLS_StyleScopedClasses['history-trade-link']} */ ;
/** @type {__VLS_StyleScopedClasses['history-trade-link']} */ ;
/** @type {__VLS_StyleScopedClasses['pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['history-overview']} */ ;
/** @type {__VLS_StyleScopedClasses['history-drawer']} */ ;
/** @type {__VLS_StyleScopedClasses['history-stats']} */ ;
/** @type {__VLS_StyleScopedClasses['history-list']} */ ;
/** @type {__VLS_StyleScopedClasses['depth-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-chart']} */ ;
/** @type {__VLS_StyleScopedClasses['history-stats']} */ ;
/** @type {__VLS_StyleScopedClasses['history-trend-card']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-legend']} */ ;
/** @type {__VLS_StyleScopedClasses['history-backdrop']} */ ;
/** @type {__VLS_StyleScopedClasses['history-listing-probe']} */ ;
/** @type {__VLS_StyleScopedClasses['history-listing-probe']} */ ;
/** @type {__VLS_StyleScopedClasses['history-listing-probe']} */ ;
/** @type {__VLS_StyleScopedClasses['history-trend-card']} */ ;
/** @type {__VLS_StyleScopedClasses['history-trend-card']} */ ;
/** @type {__VLS_StyleScopedClasses['history-trend-card']} */ ;
/** @type {__VLS_StyleScopedClasses['history-trend-card']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-legend']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-legend']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-legend']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-legend']} */ ;
/** @type {__VLS_StyleScopedClasses['reference']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-chart']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['zero-line']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-main-line']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-reference-line']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-main-point']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-reference-point']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-active']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-active']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-active']} */ ;
/** @type {__VLS_StyleScopedClasses['reference']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-inspector']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-inspector']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-inspector']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-inspector']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-inspector']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-inspector']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-empty']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-sampled']} */ ;
/** @type {__VLS_StyleScopedClasses['history-trend-card']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-chart']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-inspector']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-inspector']} */ ;
// CSS variable injection 
// CSS variable injection end 
const __VLS_0 = {}.Teleport;
/** @type {[typeof __VLS_components.Teleport, typeof __VLS_components.Teleport, ]} */ ;
// @ts-ignore
const __VLS_1 = __VLS_asFunctionalComponent(__VLS_0, new __VLS_0({
    to: "body",
}));
const __VLS_2 = __VLS_1({
    to: "body",
}, ...__VLS_functionalComponentArgsRest(__VLS_1));
__VLS_3.slots.default;
if (__VLS_ctx.selected) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.selected))
                    return;
                __VLS_ctx.emit('close');
            } },
        ...{ class: "history-backdrop" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.aside, __VLS_intrinsicElements.aside)({
        ...{ class: "history-drawer" },
        'aria-label': "价格与 ROI 历史",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "eyebrow" },
    });
    (__VLS_ctx.poolTitle);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h3, __VLS_intrinsicElements.h3)({});
    (__VLS_ctx.selected.name || __VLS_ctx.selected.marketHashName);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (__VLS_ctx.selected.marketHashName);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "drawer-description" },
    });
    (__VLS_ctx.poolDescription);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.selected))
                    return;
                __VLS_ctx.emit('close');
            } },
        type: "button",
        'aria-label': "关闭",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "history-range" },
        'aria-label': "历史时间范围",
    });
    for (const [value] of __VLS_getVForSourceType(['7d', '30d', '90d', 'all'])) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
            ...{ onClick: (...[$event]) => {
                    if (!(__VLS_ctx.selected))
                        return;
                    __VLS_ctx.emit('change-range', value);
                } },
            key: (value),
            ...{ class: ({ active: __VLS_ctx.range === value }) },
            type: "button",
        });
        (__VLS_ctx.rangeLabel(value));
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "history-overview" },
        'aria-label': "历史统计与 ROI 趋势",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "history-stat-card" },
        'aria-label': "历史统计",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "history-stats" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.pct(__VLS_ctx.stats?.highestRoi));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.pct(__VLS_ctx.stats?.averageRoi));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.roiBasisLabel(__VLS_ctx.stats));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.stats?.validObservationCount ?? 0);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "history-trend-card" },
        'aria-label': "ROI 历史趋势",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "trend-legend" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "main" },
    });
    if (__VLS_ctx.chartModel.hasBuyReference) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: "reference" },
        });
    }
    if (__VLS_ctx.chartModel.points.length) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "trend-chart-shell" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.svg, __VLS_intrinsicElements.svg)({
            ...{ onPointermove: (__VLS_ctx.activateTrendFromPointer) },
            ...{ onPointerleave: (...[$event]) => {
                    if (!(__VLS_ctx.selected))
                        return;
                    if (!(__VLS_ctx.chartModel.points.length))
                        return;
                    __VLS_ctx.activeTrendIndex = null;
                } },
            ...{ onFocus: (...[$event]) => {
                    if (!(__VLS_ctx.selected))
                        return;
                    if (!(__VLS_ctx.chartModel.points.length))
                        return;
                    __VLS_ctx.focusTrendBoundary('last');
                } },
            ...{ onKeydown: (...[$event]) => {
                    if (!(__VLS_ctx.selected))
                        return;
                    if (!(__VLS_ctx.chartModel.points.length))
                        return;
                    __VLS_ctx.moveTrendFocus(-1);
                } },
            ...{ onKeydown: (...[$event]) => {
                    if (!(__VLS_ctx.selected))
                        return;
                    if (!(__VLS_ctx.chartModel.points.length))
                        return;
                    __VLS_ctx.moveTrendFocus(1);
                } },
            ...{ onKeydown: (...[$event]) => {
                    if (!(__VLS_ctx.selected))
                        return;
                    if (!(__VLS_ctx.chartModel.points.length))
                        return;
                    __VLS_ctx.focusTrendBoundary('first');
                } },
            ...{ onKeydown: (...[$event]) => {
                    if (!(__VLS_ctx.selected))
                        return;
                    if (!(__VLS_ctx.chartModel.points.length))
                        return;
                    __VLS_ctx.focusTrendBoundary('last');
                } },
            ...{ class: "trend-chart" },
            viewBox: (`0 0 ${__VLS_ctx.CHART_WIDTH} ${__VLS_ctx.CHART_HEIGHT}`),
            role: "img",
            tabindex: "0",
            'aria-label': "ROI 历史折线图；可使用左右方向键查看每个观测点",
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.g, __VLS_intrinsicElements.g)({
            ...{ class: "trend-grid" },
        });
        for (const [tick] of __VLS_getVForSourceType((__VLS_ctx.chartModel.yTicks))) {
            (tick.value);
            __VLS_asFunctionalElement(__VLS_intrinsicElements.line)({
                x1: (__VLS_ctx.CHART_LEFT),
                x2: (__VLS_ctx.CHART_WIDTH - __VLS_ctx.CHART_RIGHT),
                y1: (tick.y),
                y2: (tick.y),
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.text, __VLS_intrinsicElements.text)({
                x: (__VLS_ctx.CHART_LEFT - 7),
                y: (tick.y + 3),
                'text-anchor': "end",
            });
            (__VLS_ctx.pct(tick.value));
        }
        if (__VLS_ctx.chartModel.zeroY != null) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.line)({
                ...{ class: "zero-line" },
                x1: (__VLS_ctx.CHART_LEFT),
                x2: (__VLS_ctx.CHART_WIDTH - __VLS_ctx.CHART_RIGHT),
                y1: (__VLS_ctx.chartModel.zeroY),
                y2: (__VLS_ctx.chartModel.zeroY),
            });
        }
        for (const [tick] of __VLS_getVForSourceType((__VLS_ctx.chartModel.xTicks))) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.text, __VLS_intrinsicElements.text)({
                key: (`${tick.x}-${tick.label}`),
                x: (tick.x),
                y: (__VLS_ctx.CHART_HEIGHT - 7),
                'text-anchor': "middle",
            });
            (tick.label);
        }
        for (const [segment, index] of __VLS_getVForSourceType((__VLS_ctx.chartModel.buySegments))) {
            (`buy-segment-${index}`);
            if (segment.length > 1) {
                __VLS_asFunctionalElement(__VLS_intrinsicElements.polyline)({
                    ...{ class: "trend-reference-line" },
                    points: (__VLS_ctx.trendSegmentPoints(segment)),
                });
            }
            else {
                __VLS_asFunctionalElement(__VLS_intrinsicElements.circle)({
                    ...{ class: "trend-reference-point" },
                    cx: (segment[0].x),
                    cy: (segment[0].buyY ?? 0),
                    r: "3",
                });
            }
        }
        if (__VLS_ctx.chartModel.points.length > 1) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.polyline)({
                ...{ class: "trend-main-line" },
                points: (__VLS_ctx.chartModel.mainPolyline),
            });
        }
        if (__VLS_ctx.chartModel.points.length > 1) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.circle)({
                ...{ class: "trend-main-start" },
                cx: (__VLS_ctx.chartModel.points[0].x),
                cy: (__VLS_ctx.chartModel.points[0].y),
                r: "2.8",
            });
        }
        if (__VLS_ctx.chartModel.points.length > 1) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.circle)({
                ...{ class: "trend-main-end" },
                cx: (__VLS_ctx.chartModel.points[__VLS_ctx.chartModel.points.length - 1].x),
                cy: (__VLS_ctx.chartModel.points[__VLS_ctx.chartModel.points.length - 1].y),
                r: "4.3",
            });
        }
        else {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.circle)({
                ...{ class: "trend-main-point" },
                cx: (__VLS_ctx.chartModel.points[0].x),
                cy: (__VLS_ctx.chartModel.points[0].y),
                r: "4.3",
            });
        }
        if (__VLS_ctx.activeTrendPoint) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.g, __VLS_intrinsicElements.g)({
                ...{ class: "trend-active" },
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.line)({
                x1: (__VLS_ctx.activeTrendPoint.x),
                x2: (__VLS_ctx.activeTrendPoint.x),
                y1: (__VLS_ctx.CHART_TOP),
                y2: (__VLS_ctx.CHART_HEIGHT - __VLS_ctx.CHART_BOTTOM),
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.circle)({
                cx: (__VLS_ctx.activeTrendPoint.x),
                cy: (__VLS_ctx.activeTrendPoint.y),
                r: "4",
            });
            if (__VLS_ctx.activeTrendPoint.buyY != null) {
                __VLS_asFunctionalElement(__VLS_intrinsicElements.circle)({
                    ...{ class: "reference" },
                    cx: (__VLS_ctx.activeTrendPoint.x),
                    cy: (__VLS_ctx.activeTrendPoint.buyY),
                    r: "3.5",
                });
            }
        }
        if (__VLS_ctx.activeTrendPoint) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
                ...{ class: "trend-inspector" },
                'aria-live': "polite",
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.time, __VLS_intrinsicElements.time)({});
            (__VLS_ctx.time(__VLS_ctx.activeTrendPoint.observedAt));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
            (__VLS_ctx.pct(__VLS_ctx.activeTrendPoint.expectedRoi));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
            (__VLS_ctx.pct(__VLS_ctx.activeTrendPoint.buyOrderReferenceRoi));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
            (__VLS_ctx.pct(__VLS_ctx.activeTrendPoint.roiBasis));
        }
    }
    else {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "trend-empty" },
        });
    }
    if (__VLS_ctx.trend?.sampled) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({
            ...{ class: "trend-sampled" },
        });
        (__VLS_ctx.trend.totalValidPoints);
        (__VLS_ctx.trend.points.length);
    }
    if (__VLS_ctx.error) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
            ...{ class: "history-error" },
        });
        (__VLS_ctx.error);
    }
    if (__VLS_ctx.loading) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "history-empty" },
        });
    }
    else if (__VLS_ctx.history.length === 0) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "history-empty" },
        });
    }
    else {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "history-list" },
        });
        for (const [event, index] of __VLS_getVForSourceType((__VLS_ctx.history))) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
                key: (__VLS_ctx.historyKey(event, index)),
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
                ...{ class: "event-head" },
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
            (__VLS_ctx.pct(event.expectedRoi));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
            (__VLS_ctx.eventLabel(event));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
                ...{ class: "history-reference" },
            });
            (__VLS_ctx.pct(event.buyOrderReferenceRoi));
            (__VLS_ctx.pct(event.roiBasis ?? event.balanceDiscount));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
            (__VLS_ctx.money(__VLS_ctx.orderbook(event).sellerFloorPrice ?? event.steamBuyPrice));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
            (__VLS_ctx.money(__VLS_ctx.orderbook(event).buyerMaxPrice));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
            (__VLS_ctx.money(event.c5ListingPrice));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
            (__VLS_ctx.money(event.c5ExpectedNetPrice));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
            (__VLS_ctx.time(event.observedAt || event.lastObservedAt));
            if (__VLS_ctx.orderbook(event).crossed) {
                __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
                    ...{ class: "history-crossed" },
                });
            }
            if (__VLS_ctx.orderbook(event).crossed) {
                __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
                    ...{ class: (['history-listing-probe', __VLS_ctx.listingProbeClass(event)]) },
                });
                __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
                __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
                (__VLS_ctx.listingProbeText(event));
            }
            else if (__VLS_ctx.orderbook(event).buyerMaxPrice == null) {
                __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
                    ...{ class: "history-unrecorded" },
                });
            }
            __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
                ...{ onClick: (...[$event]) => {
                        if (!(__VLS_ctx.selected))
                            return;
                        if (!!(__VLS_ctx.loading))
                            return;
                        if (!!(__VLS_ctx.history.length === 0))
                            return;
                        __VLS_ctx.toggleDepth(event, index);
                    } },
                ...{ class: "depth-toggle" },
                type: "button",
            });
            (__VLS_ctx.expandedHistoryKey === __VLS_ctx.historyKey(event, index) ? "收起买卖前 5 档" : "查看买卖前 5 档");
            if (__VLS_ctx.expandedHistoryKey === __VLS_ctx.historyKey(event, index)) {
                __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
                    ...{ class: "depth-grid" },
                });
                if (!((__VLS_ctx.orderbook(event).sellLevels?.length || 0) + (__VLS_ctx.orderbook(event).buyLevels?.length || 0))) {
                    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
                        ...{ class: "depth-empty" },
                    });
                }
                else {
                    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
                    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
                    for (const [level, levelIndex] of __VLS_getVForSourceType((__VLS_ctx.orderbook(event).sellLevels || []))) {
                        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
                            key: (`sell-${levelIndex}`),
                        });
                        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
                        (levelIndex + 1);
                        __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
                        (__VLS_ctx.money(level.price));
                        __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
                        (level.count ?? "—");
                    }
                    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
                    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
                    for (const [level, levelIndex] of __VLS_getVForSourceType((__VLS_ctx.orderbook(event).buyLevels || []))) {
                        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
                            key: (`buy-${levelIndex}`),
                        });
                        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
                        (levelIndex + 1);
                        __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
                        (__VLS_ctx.money(level.price));
                        __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
                        (level.count ?? "—");
                    }
                }
            }
            if (event.executionReason || event.riskReason || event.exitReason || event.lastError) {
                __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
                    ...{ class: "history-reason" },
                });
                (event.executionReason || event.riskReason || event.exitReason || event.lastError);
            }
            if (event.relatedTrade && __VLS_ctx.pool === 'inventory') {
                __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
                    ...{ class: "history-trade-link" },
                });
                __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({});
                __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
                __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
                (event.relatedTrade.tradeNo || event.relatedTrade.status || "状态同步中");
                __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
                (event.relatedTrade.tradeId);
                __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
                __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
                __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
                __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
                (__VLS_ctx.money(event.relatedTrade.steamBuyPrice));
                __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
                __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
                __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
                (__VLS_ctx.time(event.relatedTrade.steamBoughtAt));
                __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
                __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
                __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
                (__VLS_ctx.money(event.relatedTrade.c5SoldNetPrice));
                __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
                __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
                __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
                (__VLS_ctx.time(event.relatedTrade.completedAt));
                __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
                __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
                __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
                (__VLS_ctx.money(event.relatedTrade.realizedProfit));
                __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
                __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
                __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
                (__VLS_ctx.pct(event.relatedTrade.realizedRoi));
            }
        }
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.footer, __VLS_intrinsicElements.footer)({
        ...{ class: "pagination" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.page);
    (__VLS_ctx.pages);
    (__VLS_ctx.total);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.selected))
                    return;
                __VLS_ctx.emit('change-page', -1);
            } },
        ...{ class: "mini-action" },
        type: "button",
        disabled: (__VLS_ctx.page <= 1),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.selected))
                    return;
                __VLS_ctx.emit('change-page', 1);
            } },
        ...{ class: "mini-action" },
        type: "button",
        disabled: (__VLS_ctx.page >= __VLS_ctx.pages),
    });
}
var __VLS_3;
/** @type {__VLS_StyleScopedClasses['history-backdrop']} */ ;
/** @type {__VLS_StyleScopedClasses['history-drawer']} */ ;
/** @type {__VLS_StyleScopedClasses['eyebrow']} */ ;
/** @type {__VLS_StyleScopedClasses['drawer-description']} */ ;
/** @type {__VLS_StyleScopedClasses['history-range']} */ ;
/** @type {__VLS_StyleScopedClasses['history-overview']} */ ;
/** @type {__VLS_StyleScopedClasses['history-stat-card']} */ ;
/** @type {__VLS_StyleScopedClasses['history-stats']} */ ;
/** @type {__VLS_StyleScopedClasses['history-trend-card']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-legend']} */ ;
/** @type {__VLS_StyleScopedClasses['main']} */ ;
/** @type {__VLS_StyleScopedClasses['reference']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-chart-shell']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-chart']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['zero-line']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-reference-line']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-reference-point']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-main-line']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-main-start']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-main-end']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-main-point']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-active']} */ ;
/** @type {__VLS_StyleScopedClasses['reference']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-inspector']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-empty']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-sampled']} */ ;
/** @type {__VLS_StyleScopedClasses['history-error']} */ ;
/** @type {__VLS_StyleScopedClasses['history-empty']} */ ;
/** @type {__VLS_StyleScopedClasses['history-empty']} */ ;
/** @type {__VLS_StyleScopedClasses['history-list']} */ ;
/** @type {__VLS_StyleScopedClasses['event-head']} */ ;
/** @type {__VLS_StyleScopedClasses['history-reference']} */ ;
/** @type {__VLS_StyleScopedClasses['history-crossed']} */ ;
/** @type {__VLS_StyleScopedClasses['history-unrecorded']} */ ;
/** @type {__VLS_StyleScopedClasses['depth-toggle']} */ ;
/** @type {__VLS_StyleScopedClasses['depth-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['depth-empty']} */ ;
/** @type {__VLS_StyleScopedClasses['history-reason']} */ ;
/** @type {__VLS_StyleScopedClasses['history-trade-link']} */ ;
/** @type {__VLS_StyleScopedClasses['pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-action']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-action']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            emit: emit,
            expandedHistoryKey: expandedHistoryKey,
            activeTrendIndex: activeTrendIndex,
            poolTitle: poolTitle,
            poolDescription: poolDescription,
            CHART_WIDTH: CHART_WIDTH,
            CHART_HEIGHT: CHART_HEIGHT,
            CHART_LEFT: CHART_LEFT,
            CHART_RIGHT: CHART_RIGHT,
            CHART_TOP: CHART_TOP,
            CHART_BOTTOM: CHART_BOTTOM,
            chartModel: chartModel,
            activeTrendPoint: activeTrendPoint,
            money: money,
            pct: pct,
            time: time,
            rangeLabel: rangeLabel,
            trendSegmentPoints: trendSegmentPoints,
            activateTrendFromPointer: activateTrendFromPointer,
            moveTrendFocus: moveTrendFocus,
            focusTrendBoundary: focusTrendBoundary,
            orderbook: orderbook,
            listingProbeText: listingProbeText,
            listingProbeClass: listingProbeClass,
            historyKey: historyKey,
            toggleDepth: toggleDepth,
            roiBasisLabel: roiBasisLabel,
            eventLabel: eventLabel,
        };
    },
    __typeEmits: {},
    __typeProps: {},
    props: {},
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
    __typeEmits: {},
    __typeProps: {},
    props: {},
});
; /* PartiallyEnd: #4569/main.vue */
