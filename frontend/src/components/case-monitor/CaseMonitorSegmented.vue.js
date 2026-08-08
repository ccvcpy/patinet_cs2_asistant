export default ((__VLS_props, __VLS_ctx, __VLS_expose, __VLS_setup = (async () => {
    const __VLS_props = defineProps();
    const emit = defineEmits();
    debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
    const __VLS_fnComponent = (await import('vue')).defineComponent({
        __typeEmits: {},
    });
    const __VLS_ctx = {};
    let __VLS_components;
    let __VLS_directives;
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "cm-segmented" },
    });
    for (const [option] of __VLS_getVForSourceType((__VLS_ctx.options))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
            ...{ onClick: (...[$event]) => {
                    __VLS_ctx.emit('update:modelValue', option.value);
                } },
            key: (String(option.value)),
            ...{ class: "cm-segmented__option" },
            ...{ class: ({ 'is-active': __VLS_ctx.modelValue === option.value }) },
            type: "button",
            disabled: (__VLS_ctx.disabled),
        });
        (option.label);
    }
    /** @type {__VLS_StyleScopedClasses['cm-segmented']} */ ;
    /** @type {__VLS_StyleScopedClasses['cm-segmented__option']} */ ;
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
    return {};
})()) => ({})); /* PartiallyEnd: #4569/main.vue */
