import { computed, ref, watch } from "vue";
import { formatProfitTradeRatio } from "./profit_trade_roi_format";
const props = withDefaults(defineProps(), {
    row: null,
    maxQuantity: 0,
    submitting: false,
    error: "",
});
const emit = defineEmits();
const quantity = ref(1);
const safeMaxQuantity = computed(() => Math.max(0, Math.floor(props.maxQuantity || 0)));
const expectedTotalProfit = computed(() => (typeof props.row?.expectedProfit === "number"
    ? props.row.expectedProfit * quantity.value
    : null));
const belowAutomaticThreshold = computed(() => {
    const roi = props.row?.expectedRoi;
    const minRoi = props.row?.minRoi;
    return typeof roi === "number" && typeof minRoi === "number" && roi < minRoi;
});
watch(() => props.row?.marketHashName, () => {
    // 每次重新打开确认框都从 1 件开始，避免沿用上一件饰品的批量数量。
    quantity.value = 1;
}, { immediate: true });
watch(safeMaxQuantity, (maxQuantity) => {
    quantity.value = Math.min(Math.max(1, quantity.value), Math.max(1, maxQuantity));
});
function money(value) {
    return typeof value === "number" && Number.isFinite(value) ? `¥${value.toFixed(2)}` : "—";
}
function pct(value) {
    return formatProfitTradeRatio(value);
}
function setQuantity(value) {
    const normalizedValue = Number.isFinite(value) ? Math.floor(value) : 1;
    quantity.value = Math.min(Math.max(1, normalizedValue), Math.max(1, safeMaxQuantity.value));
}
function onQuantityInput(event) {
    const input = event.target;
    setQuantity(input.valueAsNumber);
    // `:value` 不会在状态仍为 1 时主动覆盖用户清空后的 DOM 值，这里立即回显归一化结果。
    input.value = String(quantity.value);
}
function close() {
    if (!props.submitting)
        emit("close");
}
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_withDefaultsArg = (function (t) { return t; })({
    row: null,
    maxQuantity: 0,
    submitting: false,
    error: "",
});
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
/** @type {__VLS_StyleScopedClasses['roi-warning']} */ ;
/** @type {__VLS_StyleScopedClasses['roi-ready']} */ ;
/** @type {__VLS_StyleScopedClasses['quantity-section']} */ ;
/** @type {__VLS_StyleScopedClasses['quantity-stepper']} */ ;
/** @type {__VLS_StyleScopedClasses['quantity-stepper']} */ ;
/** @type {__VLS_StyleScopedClasses['quantity-stepper']} */ ;
/** @type {__VLS_StyleScopedClasses['quantity-stepper']} */ ;
/** @type {__VLS_StyleScopedClasses['quantity-stepper']} */ ;
/** @type {__VLS_StyleScopedClasses['quantity-stepper']} */ ;
/** @type {__VLS_StyleScopedClasses['all-button']} */ ;
/** @type {__VLS_StyleScopedClasses['quantity-section']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['safety-note']} */ ;
/** @type {__VLS_StyleScopedClasses['primary']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-execution-backdrop']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-execution-dialog']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-summary']} */ ;
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
if (__VLS_ctx.row) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ onClick: (__VLS_ctx.close) },
        ...{ class: "manual-execution-backdrop" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "manual-execution-dialog" },
        role: "dialog",
        'aria-modal': "true",
        'aria-labelledby': "manual-execution-title",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({
        id: "manual-execution-title",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    (__VLS_ctx.row.name || __VLS_ctx.row.marketHashName);
    if (__VLS_ctx.row.name && __VLS_ctx.row.name !== __VLS_ctx.row.marketHashName) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
        (__VLS_ctx.row.marketHashName);
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.close) },
        type: "button",
        'aria-label': "关闭",
        disabled: (__VLS_ctx.submitting),
    });
    if (__VLS_ctx.belowAutomaticThreshold) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "roi-warning" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        (__VLS_ctx.pct(__VLS_ctx.row.expectedRoi));
        (__VLS_ctx.pct(__VLS_ctx.row.minRoi));
    }
    else {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "roi-ready" },
        });
        (__VLS_ctx.pct(__VLS_ctx.row.expectedRoi));
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "approval-note" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "quantity-section" },
        'aria-labelledby': "manual-execution-quantity",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({
        id: "manual-execution-quantity",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "quantity-row" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "quantity-stepper" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.row))
                    return;
                __VLS_ctx.setQuantity(__VLS_ctx.quantity - 1);
            } },
        type: "button",
        disabled: (__VLS_ctx.submitting || __VLS_ctx.quantity <= 1),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input, __VLS_intrinsicElements.input)({
        ...{ onInput: (__VLS_ctx.onQuantityInput) },
        value: (__VLS_ctx.quantity),
        type: "number",
        min: "1",
        max: (__VLS_ctx.safeMaxQuantity),
        disabled: (__VLS_ctx.submitting),
        'aria-label': "执行数量",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.row))
                    return;
                __VLS_ctx.setQuantity(__VLS_ctx.quantity + 1);
            } },
        type: "button",
        disabled: (__VLS_ctx.submitting || __VLS_ctx.quantity >= __VLS_ctx.safeMaxQuantity),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.row))
                    return;
                __VLS_ctx.setQuantity(__VLS_ctx.safeMaxQuantity);
            } },
        ...{ class: "all-button" },
        type: "button",
        disabled: (__VLS_ctx.submitting || __VLS_ctx.safeMaxQuantity <= 0),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (__VLS_ctx.safeMaxQuantity);
    (__VLS_ctx.row.inventoryCount ?? "—");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "execution-summary" },
        'aria-label': "预计执行结果",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.pct(__VLS_ctx.row.expectedRoi));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.money(__VLS_ctx.row.expectedProfit));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.money(__VLS_ctx.expectedTotalProfit));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "safety-note" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.quantity);
    if (__VLS_ctx.error) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
            ...{ class: "execution-error" },
            role: "alert",
        });
        (__VLS_ctx.error);
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.footer, __VLS_intrinsicElements.footer)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.close) },
        ...{ class: "secondary" },
        type: "button",
        disabled: (__VLS_ctx.submitting),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.row))
                    return;
                __VLS_ctx.emit('confirm', __VLS_ctx.quantity);
            } },
        ...{ class: "primary" },
        type: "button",
        disabled: (__VLS_ctx.submitting || __VLS_ctx.safeMaxQuantity <= 0),
    });
    (__VLS_ctx.submitting ? "提交中…" : `确认执行 ${__VLS_ctx.quantity} 件`);
}
var __VLS_3;
/** @type {__VLS_StyleScopedClasses['manual-execution-backdrop']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-execution-dialog']} */ ;
/** @type {__VLS_StyleScopedClasses['roi-warning']} */ ;
/** @type {__VLS_StyleScopedClasses['roi-ready']} */ ;
/** @type {__VLS_StyleScopedClasses['approval-note']} */ ;
/** @type {__VLS_StyleScopedClasses['quantity-section']} */ ;
/** @type {__VLS_StyleScopedClasses['quantity-row']} */ ;
/** @type {__VLS_StyleScopedClasses['quantity-stepper']} */ ;
/** @type {__VLS_StyleScopedClasses['all-button']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['safety-note']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-error']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary']} */ ;
/** @type {__VLS_StyleScopedClasses['primary']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            emit: emit,
            quantity: quantity,
            safeMaxQuantity: safeMaxQuantity,
            expectedTotalProfit: expectedTotalProfit,
            belowAutomaticThreshold: belowAutomaticThreshold,
            money: money,
            pct: pct,
            setQuantity: setQuantity,
            onQuantityInput: onQuantityInput,
            close: close,
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
