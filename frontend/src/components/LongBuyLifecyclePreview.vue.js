import { computed } from "vue";
import FolioIcon from "./FolioIcon.vue";
const props = defineProps();
const steps = computed(() => [
    { icon: "success", label: "Steam 官方成交确认", status: "ready" },
    { icon: "lock", label: "锁可交易老库存 A", status: "ready" },
    { icon: "report", label: "记录实际 paidTotal", status: "ready" },
    {
        icon: "case",
        label: "C5 上架 A",
        status: props.state.canExecuteC5Followup ? "ready" : "waiting",
    },
]);
const emptyStateText = computed(() => {
    const count = props.activeOrderCount ?? 0;
    if (count > 0)
        return `当前有 ${count} 笔程序管理的长期求购订单。`;
    return "当前没有程序管理的长期求购订单。";
});
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
/** @type {__VLS_StyleScopedClasses['lifecycle-timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['lifecycle-timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['lifecycle-timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['lifecycle-timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['lifecycle-timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['lifecycle-timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['lifecycle-timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['step-marker']} */ ;
/** @type {__VLS_StyleScopedClasses['lifecycle-empty-state']} */ ;
/** @type {__VLS_StyleScopedClasses['long-buy-lifecycle']} */ ;
/** @type {__VLS_StyleScopedClasses['long-buy-lifecycle']} */ ;
/** @type {__VLS_StyleScopedClasses['lifecycle-timeline']} */ ;
// CSS variable injection 
// CSS variable injection end 
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "lifecycle-shell" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "atomic-component-label" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "long-buy-lifecycle" },
    'aria-label': "长期求购成交后生命周期",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "lifecycle-timeline" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.ol, __VLS_intrinsicElements.ol)({});
for (const [step] of __VLS_getVForSourceType((__VLS_ctx.steps))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.li, __VLS_intrinsicElements.li)({
        key: (step.label),
        ...{ class: ({ waiting: step.status === 'waiting' }) },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "step-marker" },
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_0 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: (step.icon),
        size: (15),
    }));
    const __VLS_1 = __VLS_0({
        name: (step.icon),
        size: (15),
    }, ...__VLS_functionalComponentArgsRest(__VLS_0));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (step.label);
    if (step.status === 'waiting') {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    }
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "lifecycle-empty-state" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "empty-icon" },
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_3 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "document",
    size: (26),
}));
const __VLS_4 = __VLS_3({
    name: "document",
    size: (26),
}, ...__VLS_functionalComponentArgsRest(__VLS_3));
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
(__VLS_ctx.emptyStateText);
/** @type {__VLS_StyleScopedClasses['lifecycle-shell']} */ ;
/** @type {__VLS_StyleScopedClasses['atomic-component-label']} */ ;
/** @type {__VLS_StyleScopedClasses['long-buy-lifecycle']} */ ;
/** @type {__VLS_StyleScopedClasses['lifecycle-timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['step-marker']} */ ;
/** @type {__VLS_StyleScopedClasses['lifecycle-empty-state']} */ ;
/** @type {__VLS_StyleScopedClasses['empty-icon']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            FolioIcon: FolioIcon,
            steps: steps,
            emptyStateText: emptyStateText,
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
