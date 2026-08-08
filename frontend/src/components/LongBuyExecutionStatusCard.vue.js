import { computed } from "vue";
import FolioIcon from "./FolioIcon.vue";
const props = defineProps();
const actionRows = computed(() => [
    { label: "扫描与安全价计算", allowed: props.state.canObserve },
    { label: "核对已有 Steam 求购成交", allowed: props.state.canReconcileExistingOrders },
    { label: "新建、撤销或改价 Steam 求购", allowed: props.state.canWriteSteam },
]);
const statusIcon = computed(() => {
    if (props.state.mode === "live")
        return "success";
    if (props.state.mode === "observe")
        return "warning";
    return "error";
});
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
/** @type {__VLS_StyleScopedClasses['long-buy-status-card']} */ ;
/** @type {__VLS_StyleScopedClasses['long-buy-status-card']} */ ;
/** @type {__VLS_StyleScopedClasses['long-buy-status-card']} */ ;
/** @type {__VLS_StyleScopedClasses['is-live']} */ ;
/** @type {__VLS_StyleScopedClasses['status-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['is-disabled']} */ ;
/** @type {__VLS_StyleScopedClasses['status-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['status-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['status-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['status-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['action-list']} */ ;
/** @type {__VLS_StyleScopedClasses['action-list']} */ ;
/** @type {__VLS_StyleScopedClasses['action-list']} */ ;
/** @type {__VLS_StyleScopedClasses['action-list']} */ ;
/** @type {__VLS_StyleScopedClasses['action-list']} */ ;
/** @type {__VLS_StyleScopedClasses['allowed']} */ ;
// CSS variable injection 
// CSS variable injection end 
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: (['long-buy-status-card', `is-${__VLS_ctx.state.mode}`]) },
    'data-testid': "long-buy-execution-status",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "component-kicker" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "status-heading" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "status-icon" },
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_0 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: (__VLS_ctx.statusIcon),
    size: (20),
}));
const __VLS_1 = __VLS_0({
    name: (__VLS_ctx.statusIcon),
    size: (20),
}, ...__VLS_functionalComponentArgsRest(__VLS_0));
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({
    'data-testid': "long-buy-mode",
});
(__VLS_ctx.state.label);
__VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
(__VLS_ctx.state.detail);
__VLS_asFunctionalElement(__VLS_intrinsicElements.ul, __VLS_intrinsicElements.ul)({
    ...{ class: "action-list" },
});
for (const [row] of __VLS_getVForSourceType((__VLS_ctx.actionRows))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.li, __VLS_intrinsicElements.li)({
        key: (row.label),
        ...{ class: ({ allowed: row.allowed }) },
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_3 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: (row.allowed ? 'success' : 'error'),
        size: (16),
    }));
    const __VLS_4 = __VLS_3({
        name: (row.allowed ? 'success' : 'error'),
        size: (16),
    }, ...__VLS_functionalComponentArgsRest(__VLS_3));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (row.label);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
    (row.allowed ? "允许" : "禁止");
}
/** @type {__VLS_StyleScopedClasses['component-kicker']} */ ;
/** @type {__VLS_StyleScopedClasses['status-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['status-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['action-list']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            FolioIcon: FolioIcon,
            actionRows: actionRows,
            statusIcon: statusIcon,
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
