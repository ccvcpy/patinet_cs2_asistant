import { computed } from "vue";
import FolioIcon from "../FolioIcon.vue";
import { formatRatio } from "./format";
import CaseMonitorStatusChip from "./CaseMonitorStatusChip.vue";
const props = withDefaults(defineProps(), {
    embedded: false,
});
const emit = defineEmits();
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
    if (!rows.length)
        return "";
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
const visibleBuckets = computed(() => {
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
function bucketLabel(bucket, index) {
    if (index === 0)
        return `≥ ${bucket.lower.toFixed(2)}`;
    if (index === visibleBuckets.value.length - 1)
        return `< ${bucket.upper.toFixed(2)}`;
    if (bucket.bucket)
        return bucket.bucket.replace("-", " ~ ");
    return `${bucket.lower.toFixed(2)} ~ ${bucket.upper.toFixed(2)}`;
}
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_withDefaultsArg = (function (t) { return t; })({
    embedded: false,
});
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
if (__VLS_ctx.open) {
    const __VLS_0 = {}.Teleport;
    /** @type {[typeof __VLS_components.Teleport, typeof __VLS_components.Teleport, ]} */ ;
    // @ts-ignore
    const __VLS_1 = __VLS_asFunctionalComponent(__VLS_0, new __VLS_0({
        to: "body",
        disabled: (__VLS_ctx.embedded),
    }));
    const __VLS_2 = __VLS_1({
        to: "body",
        disabled: (__VLS_ctx.embedded),
    }, ...__VLS_functionalComponentArgsRest(__VLS_1));
    __VLS_3.slots.default;
    if (!__VLS_ctx.embedded) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.button)({
            ...{ onClick: (...[$event]) => {
                    if (!(__VLS_ctx.open))
                        return;
                    if (!(!__VLS_ctx.embedded))
                        return;
                    __VLS_ctx.emit('close');
                } },
            ...{ class: "cm-drawer-backdrop" },
            type: "button",
            'aria-label': "关闭详情",
        });
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.aside, __VLS_intrinsicElements.aside)({
        ...{ class: "cm-surface cm-detail-drawer" },
        ...{ class: (__VLS_ctx.embedded ? 'cm-detail-drawer--embedded' : 'cm-detail-drawer--fixed') },
        'aria-label': (__VLS_ctx.embedded ? '比例详情组件示例' : '比例详情'),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({
        ...{ class: "cm-drawer__header" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({
        ...{ class: "cm-drawer__title" },
    });
    (__VLS_ctx.item?.marketHashName || "-");
    /** @type {[typeof CaseMonitorStatusChip, ]} */ ;
    // @ts-ignore
    const __VLS_4 = __VLS_asFunctionalComponent(CaseMonitorStatusChip, new CaseMonitorStatusChip({
        status: "running",
        label: "运行中",
    }));
    const __VLS_5 = __VLS_4({
        status: "running",
        label: "运行中",
    }, ...__VLS_functionalComponentArgsRest(__VLS_4));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.open))
                    return;
                __VLS_ctx.emit('close');
            } },
        ...{ class: "cm-drawer__close" },
        type: "button",
        'aria-label': "关闭详情",
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_7 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: "x",
        size: (14),
    }));
    const __VLS_8 = __VLS_7({
        name: "x",
        size: (14),
    }, ...__VLS_functionalComponentArgsRest(__VLS_7));
    if (__VLS_ctx.item) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "cm-drawer__body" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "cm-drawer__summary" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "cm-drawer__stats" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "cm-drawer__stat" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (__VLS_ctx.formatRatio(__VLS_ctx.item.minRatio));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "cm-drawer__stat" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (__VLS_ctx.formatRatio(__VLS_ctx.item.maxRatio));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "cm-drawer__stat" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (__VLS_ctx.formatRatio(__VLS_ctx.item.avgRatio));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "cm-drawer__chart" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
            ...{ class: "cm-drawer__chart-title" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.svg, __VLS_intrinsicElements.svg)({
            ...{ class: "cm-line-chart" },
            viewBox: "0 0 100 82",
            preserveAspectRatio: "none",
            'aria-label': "24小时比例走势",
        });
        for (const [y] of __VLS_getVForSourceType(([8, 26, 44, 62]))) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.line)({
                key: (y),
                ...{ class: "cm-line-chart__grid" },
                x1: "0",
                y1: (y),
                x2: "100",
                y2: (y),
            });
        }
        __VLS_asFunctionalElement(__VLS_intrinsicElements.polyline)({
            ...{ class: "cm-line-chart__line" },
            points: (__VLS_ctx.chartPoints),
        });
        for (const [label, index] of __VLS_getVForSourceType((__VLS_ctx.timeLabels))) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.text, __VLS_intrinsicElements.text)({
                key: (`${label}-${index}`),
                ...{ class: "cm-line-chart__axis" },
                x: (index * 25),
                y: "78",
                'text-anchor': (index === 0 ? 'start' : index === 4 ? 'end' : 'middle'),
            });
            (label);
        }
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div)({
            ...{ class: "cm-drawer__divider" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
            ...{ class: "cm-drawer__bucket-title" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "cm-bucket-list" },
        });
        for (const [bucket, index] of __VLS_getVForSourceType((__VLS_ctx.visibleBuckets))) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
                key: (`${bucket.bucket}-${index}`),
                ...{ class: "cm-bucket-row" },
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
            (__VLS_ctx.bucketLabel(bucket, index));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
                ...{ class: "cm-bucket-row__track" },
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span)({
                ...{ class: "cm-bucket-row__fill" },
                ...{ style: ({ width: `${Math.max(2, Math.min(100, bucket.coveragePct))}%` }) },
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
                ...{ class: "cm-bucket-row__duration" },
            });
            (bucket.durationLabel);
            (bucket.coveragePct.toFixed(1));
        }
    }
    var __VLS_3;
}
/** @type {__VLS_StyleScopedClasses['cm-drawer-backdrop']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-surface']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-detail-drawer']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-drawer__header']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-drawer__title']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-drawer__close']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-drawer__body']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-drawer__summary']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-drawer__stats']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-drawer__stat']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-drawer__stat']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-drawer__stat']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-drawer__chart']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-drawer__chart-title']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-line-chart']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-line-chart__grid']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-line-chart__line']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-line-chart__axis']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-drawer__divider']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-drawer__bucket-title']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-bucket-list']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-bucket-row']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-bucket-row__track']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-bucket-row__fill']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-bucket-row__duration']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            FolioIcon: FolioIcon,
            formatRatio: formatRatio,
            CaseMonitorStatusChip: CaseMonitorStatusChip,
            emit: emit,
            chartPoints: chartPoints,
            timeLabels: timeLabels,
            visibleBuckets: visibleBuckets,
            bucketLabel: bucketLabel,
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
