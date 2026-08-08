import FolioIcon from "../FolioIcon.vue";
const __VLS_props = withDefaults(defineProps(), {
    tone: "quiet",
    icon: undefined,
    loading: false,
    disabled: false,
    type: "button",
});
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_withDefaultsArg = (function (t) { return t; })({
    tone: "quiet",
    icon: undefined,
    loading: false,
    disabled: false,
    type: "button",
});
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ class: "cm-button" },
    ...{ class: (`cm-button--${__VLS_ctx.tone}`) },
    type: (__VLS_ctx.type),
    disabled: (__VLS_ctx.disabled || __VLS_ctx.loading),
});
if (__VLS_ctx.loading || __VLS_ctx.icon) {
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_0 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        ...{ class: ({ 'cm-button__spinner': __VLS_ctx.loading }) },
        name: (__VLS_ctx.loading ? 'refresh' : __VLS_ctx.icon),
        size: (17),
        strokeWidth: (1.8),
    }));
    const __VLS_1 = __VLS_0({
        ...{ class: ({ 'cm-button__spinner': __VLS_ctx.loading }) },
        name: (__VLS_ctx.loading ? 'refresh' : __VLS_ctx.icon),
        size: (17),
        strokeWidth: (1.8),
    }, ...__VLS_functionalComponentArgsRest(__VLS_0));
}
var __VLS_3 = {};
/** @type {__VLS_StyleScopedClasses['cm-button']} */ ;
// @ts-ignore
var __VLS_4 = __VLS_3;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            FolioIcon: FolioIcon,
        };
    },
    __typeProps: {},
    props: {},
});
const __VLS_component = (await import('vue')).defineComponent({
    setup() {
        return {};
    },
    __typeProps: {},
    props: {},
});
export default {};
; /* PartiallyEnd: #4569/main.vue */
