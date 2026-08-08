import { computed } from "vue";
import { requiresLongBuyConfigConfirmation, } from "./profit_trade_long_buy_strategy";
const props = defineProps();
const emit = defineEmits();
const switches = computed(() => [
    {
        id: "enabled",
        configKey: "enabled",
        key: "profitTrade.enabled",
        label: "Profit Trade 总功能",
        detail: "控制 Profit Trade 周期是否运行。",
        enabled: props.config.enabled,
    },
    {
        id: "real-execution",
        configKey: "allowRealExecution",
        key: "profitTrade.allowRealExecution",
        label: "普通真实执行",
        detail: "控制原有直购、C5 上架与改价的真实动作。",
        enabled: props.config.allowRealExecution,
    },
    {
        id: "feature",
        configKey: "longBuyEnabled",
        key: "profitTrade.longBuyEnabled",
        label: "长期求购功能",
        detail: "控制新长期求购的算价与观察；已有订单仍做安全成交核对。",
        enabled: props.config.longBuyEnabled,
    },
    {
        id: "steam-write",
        configKey: "longBuyAllowRealExecution",
        key: "profitTrade.longBuyAllowRealExecution",
        label: "长期求购 Steam 写入",
        detail: "单独控制长期求购的新建、撤销与安全重建。",
        enabled: props.config.longBuyAllowRealExecution,
    },
].map((item) => ({
    ...item,
    needsConfirmation: requiresLongBuyConfigConfirmation(item.configKey),
})));
function requestToggle(key, enabled) {
    if (props.updatingKey)
        return;
    emit("toggle", key, !enabled);
}
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
/** @type {__VLS_StyleScopedClasses['long-buy-switch-matrix']} */ ;
/** @type {__VLS_StyleScopedClasses['long-buy-switch-matrix']} */ ;
/** @type {__VLS_StyleScopedClasses['switch-row']} */ ;
/** @type {__VLS_StyleScopedClasses['switch-copy']} */ ;
/** @type {__VLS_StyleScopedClasses['switch-copy']} */ ;
/** @type {__VLS_StyleScopedClasses['switch-copy']} */ ;
/** @type {__VLS_StyleScopedClasses['switch-control']} */ ;
/** @type {__VLS_StyleScopedClasses['switch-control']} */ ;
/** @type {__VLS_StyleScopedClasses['static-switch']} */ ;
/** @type {__VLS_StyleScopedClasses['static-switch']} */ ;
/** @type {__VLS_StyleScopedClasses['static-switch']} */ ;
/** @type {__VLS_StyleScopedClasses['on']} */ ;
/** @type {__VLS_StyleScopedClasses['switch-control']} */ ;
/** @type {__VLS_StyleScopedClasses['switch-control']} */ ;
/** @type {__VLS_StyleScopedClasses['on']} */ ;
/** @type {__VLS_StyleScopedClasses['switch-control']} */ ;
/** @type {__VLS_StyleScopedClasses['effective-result']} */ ;
/** @type {__VLS_StyleScopedClasses['effective-result']} */ ;
/** @type {__VLS_StyleScopedClasses['effective-result']} */ ;
/** @type {__VLS_StyleScopedClasses['effective-result']} */ ;
// CSS variable injection 
// CSS variable injection end 
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "long-buy-switch-matrix" },
    'aria-label': "长期求购开关状态",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "component-kicker" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h3, __VLS_intrinsicElements.h3)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "switch-hint" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "switch-list" },
});
for (const [item] of __VLS_getVForSourceType((__VLS_ctx.switches))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
        key: (item.id),
        'data-testid': (`long-buy-setting-${item.id}`),
        ...{ class: "switch-row" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "switch-copy" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.code, __VLS_intrinsicElements.code)({});
    (item.key);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (item.label);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (item.detail);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                __VLS_ctx.requestToggle(item.configKey, item.enabled);
            } },
        'data-testid': (`long-buy-toggle-${item.id}`),
        'aria-label': (`${item.label}：${item.enabled ? '开启' : '关闭'}`),
        'aria-pressed': (item.enabled),
        ...{ class: "switch-control" },
        type: "button",
        disabled: (Boolean(__VLS_ctx.updatingKey)),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: (['static-switch', { on: item.enabled }]) },
        'aria-hidden': "true",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({
        ...{ class: ({ on: item.enabled }) },
    });
    (item.enabled ? "开启" : "关闭");
    if (item.needsConfirmation) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.em, __VLS_intrinsicElements.em)({});
    }
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
    ...{ class: (['effective-result', `is-${__VLS_ctx.state.mode}`]) },
    'data-testid': "long-buy-effective-result",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.state.mode === "live" ? "可写 Steam" : __VLS_ctx.state.canObserve ? "只观察，不写 Steam" : "未运行");
/** @type {__VLS_StyleScopedClasses['long-buy-switch-matrix']} */ ;
/** @type {__VLS_StyleScopedClasses['component-kicker']} */ ;
/** @type {__VLS_StyleScopedClasses['switch-hint']} */ ;
/** @type {__VLS_StyleScopedClasses['switch-list']} */ ;
/** @type {__VLS_StyleScopedClasses['switch-row']} */ ;
/** @type {__VLS_StyleScopedClasses['switch-copy']} */ ;
/** @type {__VLS_StyleScopedClasses['switch-control']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            switches: switches,
            requestToggle: requestToggle,
        };
    },
    __typeEmits: {},
    __typeProps: {},
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
    __typeEmits: {},
    __typeProps: {},
});
; /* PartiallyEnd: #4569/main.vue */
