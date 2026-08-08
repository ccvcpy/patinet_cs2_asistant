const __VLS_props = defineProps();
const emit = defineEmits();
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.emit('update:modelValue', !__VLS_ctx.modelValue);
        } },
    ...{ class: "cm-toggle" },
    ...{ class: ({ 'is-on': __VLS_ctx.modelValue }) },
    type: "button",
    role: "switch",
    'aria-checked': (__VLS_ctx.modelValue),
    disabled: (__VLS_ctx.disabled),
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "cm-toggle__track" },
    'aria-hidden': "true",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span)({
    ...{ class: "cm-toggle__thumb" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
(__VLS_ctx.modelValue ? (__VLS_ctx.onLabel || "开") : (__VLS_ctx.offLabel || "关"));
/** @type {__VLS_StyleScopedClasses['cm-toggle']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-toggle__track']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-toggle__thumb']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            emit: emit,
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
