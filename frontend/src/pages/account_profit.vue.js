import { computed, onMounted, ref } from "vue";
const STORAGE_KEY = "cs-account-check-previous-total";
const previousTotal = ref(1878.31);
const currentRecordedBalance = ref(0);
const realTotal = ref(null);
const savedAt = ref("");
onMounted(() => {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored !== null) {
        const parsed = Number(stored);
        if (Number.isFinite(parsed)) {
            previousTotal.value = parsed;
        }
    }
});
const programTotal = computed(() => previousTotal.value + currentRecordedBalance.value);
const hasRealTotal = computed(() => realTotal.value !== null && Number.isFinite(realTotal.value));
const difference = computed(() => (hasRealTotal.value ? Number(realTotal.value) - programTotal.value : 0));
const isBalanced = computed(() => hasRealTotal.value && Math.abs(difference.value) < 0.005);
const resultText = computed(() => {
    if (!hasRealTotal.value)
        return "等待 Real Total";
    return isBalanced.value ? "相等，程序记录挂刀余额等于真实挂刀余额" : "不相等，需要复查挂刀余额";
});
function formatMoney(value) {
    return value.toFixed(2);
}
function parseCardNumber(value) {
    const target = value.target;
    const raw = target.textContent?.trim() ?? "";
    if (!raw)
        return null;
    const parsed = Number(raw.replace(/[^\d.-]/g, ""));
    return Number.isFinite(parsed) ? parsed : null;
}
function updatePreviousTotal(event) {
    previousTotal.value = parseCardNumber(event) ?? 0;
}
function updateCurrentRecordedBalance(event) {
    currentRecordedBalance.value = parseCardNumber(event) ?? 0;
}
function updateRealTotal(event) {
    realTotal.value = parseCardNumber(event);
}
function saveSnapshot() {
    if (!hasRealTotal.value)
        return;
    const nextPrevious = Number(realTotal.value);
    previousTotal.value = nextPrevious;
    currentRecordedBalance.value = 0;
    realTotal.value = null;
    savedAt.value = new Date().toLocaleString();
    window.localStorage.setItem(STORAGE_KEY, String(nextPrevious));
}
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
__VLS_asFunctionalElement(__VLS_intrinsicElements.main, __VLS_intrinsicElements.main)({
    ...{ class: "page" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({
    ...{ class: "page-header" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
    ...{ class: "eyebrow" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h1, __VLS_intrinsicElements.h1)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.saveSnapshot) },
    ...{ class: "primary-button" },
    type: "button",
    disabled: (!__VLS_ctx.hasRealTotal),
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "metrics-grid" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "metric-card" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({
    ...{ onInput: (__VLS_ctx.updatePreviousTotal) },
    ...{ class: "editable-total" },
    contenteditable: "true",
    inputmode: "decimal",
});
(__VLS_ctx.formatMoney(__VLS_ctx.previousTotal));
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "metric-card" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({
    ...{ onInput: (__VLS_ctx.updateCurrentRecordedBalance) },
    ...{ class: "editable-total" },
    contenteditable: "true",
    inputmode: "decimal",
});
(__VLS_ctx.formatMoney(__VLS_ctx.currentRecordedBalance));
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "metric-card" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.formatMoney(__VLS_ctx.programTotal));
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "metric-card" },
    ...{ class: ({ success: __VLS_ctx.isBalanced, danger: __VLS_ctx.hasRealTotal && !__VLS_ctx.isBalanced }) },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({
    ...{ onInput: (__VLS_ctx.updateRealTotal) },
    ...{ class: "editable-total" },
    contenteditable: "true",
    inputmode: "decimal",
});
(__VLS_ctx.hasRealTotal ? __VLS_ctx.formatMoney(Number(__VLS_ctx.realTotal)) : __VLS_ctx.resultText);
if (__VLS_ctx.savedAt) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "save-note" },
    });
    (__VLS_ctx.savedAt);
}
/** @type {__VLS_StyleScopedClasses['page']} */ ;
/** @type {__VLS_StyleScopedClasses['page-header']} */ ;
/** @type {__VLS_StyleScopedClasses['eyebrow']} */ ;
/** @type {__VLS_StyleScopedClasses['primary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-card']} */ ;
/** @type {__VLS_StyleScopedClasses['editable-total']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-card']} */ ;
/** @type {__VLS_StyleScopedClasses['editable-total']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-card']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-card']} */ ;
/** @type {__VLS_StyleScopedClasses['editable-total']} */ ;
/** @type {__VLS_StyleScopedClasses['save-note']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            previousTotal: previousTotal,
            currentRecordedBalance: currentRecordedBalance,
            realTotal: realTotal,
            savedAt: savedAt,
            programTotal: programTotal,
            hasRealTotal: hasRealTotal,
            isBalanced: isBalanced,
            resultText: resultText,
            formatMoney: formatMoney,
            updatePreviousTotal: updatePreviousTotal,
            updateCurrentRecordedBalance: updateCurrentRecordedBalance,
            updateRealTotal: updateRealTotal,
            saveSnapshot: saveSnapshot,
        };
    },
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
});
; /* PartiallyEnd: #4569/main.vue */
