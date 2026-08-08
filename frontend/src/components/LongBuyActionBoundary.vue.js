import { computed } from "vue";
import FolioIcon from "./FolioIcon.vue";
const props = defineProps();
const boundaryActions = computed(() => [
    {
        id: "profit-cycle",
        label: "Profit Trade 周期与新机会扫描",
        detail: props.config.enabled ? "总功能已开启。" : "Profit Trade 总功能已关闭。",
        allowed: props.config.enabled,
    },
    {
        id: "ordinary-purchase",
        label: "原有直接购买链路",
        detail: props.config.enabled
            ? (props.config.allowRealExecution ? "允许买 B、上架 C5 与后续自动改价。" : "普通真实执行已关闭。")
            : "Profit Trade 总功能已关闭。",
        allowed: props.config.enabled && props.config.allowRealExecution,
    },
    {
        id: "long-buy-scan",
        label: "长期求购方案计算与观察",
        detail: props.config.enabled
            ? (props.config.longBuyEnabled ? "长期求购功能已开启。" : "长期求购功能已关闭。")
            : "Profit Trade 总功能已关闭。",
        allowed: props.state.canObserve,
    },
    {
        id: "long-buy-reconcile",
        label: "核对已有 Steam 长期求购成交",
        detail: props.state.canReconcileExistingOrders
            ? "已有订单继续读取官方成交证据。"
            : "Profit Trade 总功能关闭后暂停核对。",
        allowed: props.state.canReconcileExistingOrders,
    },
    {
        id: "long-buy-c5-followup",
        label: "长期求购成交后锁 A 并上架 C5",
        detail: props.state.canExecuteC5Followup
            ? "普通真实执行已开启，可以推进成交后的做 T 闭环。"
            : "需要总功能、普通真实执行和长期求购功能同时开启。",
        allowed: props.state.canExecuteC5Followup,
    },
    {
        id: "long-buy-steam-write",
        label: "新建、撤销或改价 Steam 长期求购",
        detail: props.state.canWriteSteam
            ? "四层许可已全部满足，仍需通过钱包、ROI 与盘口风控。"
            : "需要四个开关全部开启；长期 Steam 写入必须单独确认。",
        allowed: props.state.canWriteSteam,
    },
]);
const allowedActions = computed(() => boundaryActions.value.filter((item) => item.allowed));
const blockedActions = computed(() => [
    ...boundaryActions.value.filter((item) => !item.allowed),
    {
        id: "crossed-book-freeze",
        label: "交叉盘口旧单未成交时撤旧、改价或直购",
        detail: "始终禁止，保留旧求购等待官方成交证据。",
        allowed: false,
    },
    {
        id: "bypass-risk-gates",
        label: "绕过钱包、ROI 或老库存 A 风控",
        detail: "始终禁止。",
        allowed: false,
    },
]);
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
/** @type {__VLS_StyleScopedClasses['long-buy-action-boundary']} */ ;
/** @type {__VLS_StyleScopedClasses['boundary-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['boundary-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['boundary-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['boundary-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['blocked']} */ ;
/** @type {__VLS_StyleScopedClasses['boundary-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['boundary-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['boundary-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['boundary-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['blocked']} */ ;
/** @type {__VLS_StyleScopedClasses['boundary-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['boundary-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['boundary-grid']} */ ;
// CSS variable injection 
// CSS variable injection end 
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "long-buy-action-boundary" },
    'aria-label': "长期求购动作边界",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "component-kicker" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h3, __VLS_intrinsicElements.h3)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "boundary-grid" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "allowed" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h4, __VLS_intrinsicElements.h4)({});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_0 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "success",
    size: (18),
}));
const __VLS_1 = __VLS_0({
    name: "success",
    size: (18),
}, ...__VLS_functionalComponentArgsRest(__VLS_0));
if (__VLS_ctx.allowedActions.length === 0) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "empty-boundary" },
    });
}
else {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.ul, __VLS_intrinsicElements.ul)({});
    for (const [item] of __VLS_getVForSourceType((__VLS_ctx.allowedActions))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.li, __VLS_intrinsicElements.li)({
            key: (item.id),
            'data-testid': (`allowed-${item.id}`),
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (item.label);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
        (item.detail);
    }
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "blocked" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h4, __VLS_intrinsicElements.h4)({});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_3 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "error",
    size: (18),
}));
const __VLS_4 = __VLS_3({
    name: "error",
    size: (18),
}, ...__VLS_functionalComponentArgsRest(__VLS_3));
__VLS_asFunctionalElement(__VLS_intrinsicElements.ul, __VLS_intrinsicElements.ul)({});
for (const [item] of __VLS_getVForSourceType((__VLS_ctx.blockedActions))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.li, __VLS_intrinsicElements.li)({
        key: (item.id),
        'data-testid': (`blocked-${item.id}`),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (item.label);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (item.detail);
}
/** @type {__VLS_StyleScopedClasses['long-buy-action-boundary']} */ ;
/** @type {__VLS_StyleScopedClasses['component-kicker']} */ ;
/** @type {__VLS_StyleScopedClasses['boundary-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['allowed']} */ ;
/** @type {__VLS_StyleScopedClasses['empty-boundary']} */ ;
/** @type {__VLS_StyleScopedClasses['blocked']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            FolioIcon: FolioIcon,
            allowedActions: allowedActions,
            blockedActions: blockedActions,
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
