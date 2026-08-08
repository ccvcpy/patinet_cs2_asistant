import FolioIcon from "../FolioIcon.vue";
const __VLS_props = withDefaults(defineProps(), {
    disabled: false,
    expanded: false,
});
const emit = defineEmits();
const intervals = [5, 10, 15, 30];
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_withDefaultsArg = (function (t) { return t; })({
    disabled: false,
    expanded: false,
});
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
if (__VLS_ctx.expanded) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "cm-interval-options" },
    });
    for (const [interval] of __VLS_getVForSourceType((__VLS_ctx.intervals))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
            ...{ onClick: (...[$event]) => {
                    if (!(__VLS_ctx.expanded))
                        return;
                    __VLS_ctx.emit('update:modelValue', interval);
                } },
            key: (interval),
            ...{ class: "cm-interval-option" },
            ...{ class: ({ 'is-active': __VLS_ctx.modelValue === interval }) },
            type: "button",
            disabled: (__VLS_ctx.disabled),
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        (interval);
        /** @type {[typeof FolioIcon, ]} */ ;
        // @ts-ignore
        const __VLS_0 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
            name: "chevron-down",
            size: (11),
        }));
        const __VLS_1 = __VLS_0({
            name: "chevron-down",
            size: (11),
        }, ...__VLS_functionalComponentArgsRest(__VLS_0));
    }
}
else {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
        ...{ onChange: (...[$event]) => {
                if (!!(__VLS_ctx.expanded))
                    return;
                __VLS_ctx.emit('update:modelValue', Number($event.target.value));
            } },
        ...{ class: "cm-interval-select" },
        value: (__VLS_ctx.modelValue),
        disabled: (__VLS_ctx.disabled),
        'aria-label': "采集间隔",
    });
    for (const [interval] of __VLS_getVForSourceType((__VLS_ctx.intervals))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
            key: (interval),
            value: (interval),
        });
        (interval);
    }
}
/** @type {__VLS_StyleScopedClasses['cm-interval-options']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-interval-option']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-interval-select']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            FolioIcon: FolioIcon,
            emit: emit,
            intervals: intervals,
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
