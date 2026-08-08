import { computed } from "vue";
import FolioIcon from "../FolioIcon.vue";
import { formatInteger, formatRatio, recommendedRatio, speedLabel, stabilityLabel, stabilityTone, } from "./format";
const props = defineProps();
const sourceLabel = computed(() => {
    const label = String(props.item.steamReferenceSourceLabel || "");
    if (!label || /wall|墙/i.test(label))
        return "20墙挂价";
    if (/buy|求购/i.test(label))
        return "最高求购";
    return label;
});
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ class: "cm-recommendation-row" },
    ...{ class: ({ 'is-selected': __VLS_ctx.selected }) },
    type: "button",
    'aria-label': (`查看 ${__VLS_ctx.item.marketHashName} 详情`),
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "cm-recommendation-row__rank" },
});
if (__VLS_ctx.rank === 1) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "cm-rank-badge" },
    });
    (__VLS_ctx.rank);
}
else {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.rank);
}
if (__VLS_ctx.rank <= 3) {
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_0 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        ...{ class: "cm-rank-crown" },
        ...{ class: (`cm-rank-crown--${__VLS_ctx.rank}`) },
        name: "crown",
        size: (15),
        strokeWidth: (2),
    }));
    const __VLS_1 = __VLS_0({
        ...{ class: "cm-rank-crown" },
        ...{ class: (`cm-rank-crown--${__VLS_ctx.rank}`) },
        name: "crown",
        size: (15),
        strokeWidth: (2),
    }, ...__VLS_functionalComponentArgsRest(__VLS_0));
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "cm-recommendation-row__name" },
    title: (__VLS_ctx.item.marketHashName),
});
(__VLS_ctx.item.marketHashName);
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "cm-recommendation-row__ratio" },
});
(__VLS_ctx.formatRatio(__VLS_ctx.recommendedRatio(__VLS_ctx.item)));
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
(__VLS_ctx.sourceLabel);
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
(__VLS_ctx.formatInteger(__VLS_ctx.item.steamVolume24h));
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "cm-dot-value" },
});
(__VLS_ctx.speedLabel(__VLS_ctx.item));
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "cm-dot-value" },
    ...{ class: ({
            'cm-dot-value--medium': __VLS_ctx.stabilityTone(__VLS_ctx.item) === 'medium',
            'cm-dot-value--low': __VLS_ctx.stabilityTone(__VLS_ctx.item) === 'low',
        }) },
});
(__VLS_ctx.stabilityLabel(__VLS_ctx.item));
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
(__VLS_ctx.formatRatio(__VLS_ctx.item.minRatio));
(__VLS_ctx.item.minRatioDurationLabel);
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
(__VLS_ctx.formatRatio(__VLS_ctx.item.maxRatio));
(__VLS_ctx.item.maxRatioDurationLabel);
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "cm-recommendation-row__detail" },
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_3 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "chevron-right",
    size: (16),
}));
const __VLS_4 = __VLS_3({
    name: "chevron-right",
    size: (16),
}, ...__VLS_functionalComponentArgsRest(__VLS_3));
/** @type {__VLS_StyleScopedClasses['cm-recommendation-row']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-recommendation-row__rank']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-rank-badge']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-rank-crown']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-recommendation-row__name']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-recommendation-row__ratio']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-dot-value']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-dot-value']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-recommendation-row__detail']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            FolioIcon: FolioIcon,
            formatInteger: formatInteger,
            formatRatio: formatRatio,
            recommendedRatio: recommendedRatio,
            speedLabel: speedLabel,
            stabilityLabel: stabilityLabel,
            stabilityTone: stabilityTone,
            sourceLabel: sourceLabel,
        };
    },
    __typeProps: {},
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
    __typeProps: {},
});
; /* PartiallyEnd: #4569/main.vue */
