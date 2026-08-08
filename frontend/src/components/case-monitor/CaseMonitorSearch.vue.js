import FolioIcon from "../FolioIcon.vue";
const __VLS_props = withDefaults(defineProps(), {
    placeholder: "搜索中文名或英文名",
});
const emit = defineEmits();
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_withDefaultsArg = (function (t) { return t; })({
    placeholder: "搜索中文名或英文名",
});
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
    ...{ class: "cm-search" },
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_0 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    ...{ class: "cm-search__icon" },
    name: "search",
    size: (16),
}));
const __VLS_1 = __VLS_0({
    ...{ class: "cm-search__icon" },
    name: "search",
    size: (16),
}, ...__VLS_functionalComponentArgsRest(__VLS_0));
__VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
    ...{ onInput: (...[$event]) => {
            __VLS_ctx.emit('update:modelValue', $event.target.value);
        } },
    type: "search",
    value: (__VLS_ctx.modelValue),
    placeholder: (__VLS_ctx.placeholder),
});
if (__VLS_ctx.modelValue) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.modelValue))
                    return;
                __VLS_ctx.emit('update:modelValue', '');
            } },
        ...{ class: "cm-search__clear" },
        type: "button",
        'aria-label': "清空搜索",
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_3 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: "x",
        size: (13),
    }));
    const __VLS_4 = __VLS_3({
        name: "x",
        size: (13),
    }, ...__VLS_functionalComponentArgsRest(__VLS_3));
}
/** @type {__VLS_StyleScopedClasses['cm-search']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-search__icon']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-search__clear']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            FolioIcon: FolioIcon,
            emit: emit,
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
