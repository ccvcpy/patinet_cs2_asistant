import LongBuyActionBoundary from "./LongBuyActionBoundary.vue";
import LongBuyCrossedBookSafetyRule from "./LongBuyCrossedBookSafetyRule.vue";
import LongBuyExecutionStatusCard from "./LongBuyExecutionStatusCard.vue";
import LongBuyExecutionSwitchMatrix from "./LongBuyExecutionSwitchMatrix.vue";
import LongBuyLifecyclePreview from "./LongBuyLifecyclePreview.vue";
import LongBuyRiskHint from "./LongBuyRiskHint.vue";
import { resolveProfitTradeLongBuyStrategyState, } from "./profit_trade_long_buy_strategy";
import { computed } from "vue";
const props = defineProps();
const emit = defineEmits();
const state = computed(() => resolveProfitTradeLongBuyStrategyState(props.config));
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
/** @type {__VLS_StyleScopedClasses['strategy-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['strategy-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['strategy-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['strategy-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['strategy-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['strategy-mode']} */ ;
/** @type {__VLS_StyleScopedClasses['strategy-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['strategy-mode']} */ ;
/** @type {__VLS_StyleScopedClasses['strategy-top-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['strategy-top-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['strategy-bottom-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['long-buy-strategy-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['strategy-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['strategy-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['strategy-mode']} */ ;
/** @type {__VLS_StyleScopedClasses['strategy-top-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['strategy-top-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['strategy-bottom-grid']} */ ;
// CSS variable injection 
// CSS variable injection end 
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "long-buy-strategy-panel" },
    'aria-labelledby': "long-buy-strategy-title",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({
    ...{ class: "strategy-heading" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({
    id: "long-buy-strategy-title",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: (['strategy-mode', `is-${__VLS_ctx.state.mode}`]) },
});
(__VLS_ctx.state.label);
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "strategy-top-grid" },
});
/** @type {[typeof LongBuyExecutionStatusCard, ]} */ ;
// @ts-ignore
const __VLS_0 = __VLS_asFunctionalComponent(LongBuyExecutionStatusCard, new LongBuyExecutionStatusCard({
    state: (__VLS_ctx.state),
}));
const __VLS_1 = __VLS_0({
    state: (__VLS_ctx.state),
}, ...__VLS_functionalComponentArgsRest(__VLS_0));
/** @type {[typeof LongBuyExecutionSwitchMatrix, ]} */ ;
// @ts-ignore
const __VLS_3 = __VLS_asFunctionalComponent(LongBuyExecutionSwitchMatrix, new LongBuyExecutionSwitchMatrix({
    ...{ 'onToggle': {} },
    config: (__VLS_ctx.config),
    state: (__VLS_ctx.state),
    updatingKey: (__VLS_ctx.updatingKey),
}));
const __VLS_4 = __VLS_3({
    ...{ 'onToggle': {} },
    config: (__VLS_ctx.config),
    state: (__VLS_ctx.state),
    updatingKey: (__VLS_ctx.updatingKey),
}, ...__VLS_functionalComponentArgsRest(__VLS_3));
let __VLS_6;
let __VLS_7;
let __VLS_8;
const __VLS_9 = {
    onToggle: ((key, nextEnabled) => __VLS_ctx.emit('toggle', key, nextEnabled))
};
var __VLS_5;
/** @type {[typeof LongBuyActionBoundary, ]} */ ;
// @ts-ignore
const __VLS_10 = __VLS_asFunctionalComponent(LongBuyActionBoundary, new LongBuyActionBoundary({
    config: (__VLS_ctx.config),
    state: (__VLS_ctx.state),
}));
const __VLS_11 = __VLS_10({
    config: (__VLS_ctx.config),
    state: (__VLS_ctx.state),
}, ...__VLS_functionalComponentArgsRest(__VLS_10));
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "strategy-bottom-grid" },
});
/** @type {[typeof LongBuyCrossedBookSafetyRule, ]} */ ;
// @ts-ignore
const __VLS_13 = __VLS_asFunctionalComponent(LongBuyCrossedBookSafetyRule, new LongBuyCrossedBookSafetyRule({}));
const __VLS_14 = __VLS_13({}, ...__VLS_functionalComponentArgsRest(__VLS_13));
/** @type {[typeof LongBuyLifecyclePreview, ]} */ ;
// @ts-ignore
const __VLS_16 = __VLS_asFunctionalComponent(LongBuyLifecyclePreview, new LongBuyLifecyclePreview({
    state: (__VLS_ctx.state),
    activeOrderCount: (__VLS_ctx.activeOrderCount ?? 0),
}));
const __VLS_17 = __VLS_16({
    state: (__VLS_ctx.state),
    activeOrderCount: (__VLS_ctx.activeOrderCount ?? 0),
}, ...__VLS_functionalComponentArgsRest(__VLS_16));
/** @type {[typeof LongBuyRiskHint, ]} */ ;
// @ts-ignore
const __VLS_19 = __VLS_asFunctionalComponent(LongBuyRiskHint, new LongBuyRiskHint({
    config: (__VLS_ctx.config),
    state: (__VLS_ctx.state),
}));
const __VLS_20 = __VLS_19({
    config: (__VLS_ctx.config),
    state: (__VLS_ctx.state),
}, ...__VLS_functionalComponentArgsRest(__VLS_19));
/** @type {__VLS_StyleScopedClasses['long-buy-strategy-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['strategy-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['strategy-top-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['strategy-bottom-grid']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            LongBuyActionBoundary: LongBuyActionBoundary,
            LongBuyCrossedBookSafetyRule: LongBuyCrossedBookSafetyRule,
            LongBuyExecutionStatusCard: LongBuyExecutionStatusCard,
            LongBuyExecutionSwitchMatrix: LongBuyExecutionSwitchMatrix,
            LongBuyLifecyclePreview: LongBuyLifecyclePreview,
            LongBuyRiskHint: LongBuyRiskHint,
            emit: emit,
            state: state,
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
