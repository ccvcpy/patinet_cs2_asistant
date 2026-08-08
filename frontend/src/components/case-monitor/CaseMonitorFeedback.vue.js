import FolioIcon from "../FolioIcon.vue";
const __VLS_props = withDefaults(defineProps(), {
    dismissible: true,
});
const emit = defineEmits();
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_withDefaultsArg = (function (t) { return t; })({
    dismissible: true,
});
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "cm-feedback" },
    ...{ class: (`cm-feedback--${__VLS_ctx.tone}`) },
    role: "status",
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_0 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: (__VLS_ctx.tone === 'success' ? 'success' : 'error'),
    size: (17),
}));
const __VLS_1 = __VLS_0({
    name: (__VLS_ctx.tone === 'success' ? 'success' : 'error'),
    size: (17),
}, ...__VLS_functionalComponentArgsRest(__VLS_0));
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
var __VLS_3 = {};
if (__VLS_ctx.$slots.actions) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "cm-feedback__actions" },
    });
    var __VLS_5 = {};
}
if (__VLS_ctx.dismissible) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.dismissible))
                    return;
                __VLS_ctx.emit('close');
            } },
        ...{ class: "cm-feedback__close" },
        type: "button",
        'aria-label': "关闭消息",
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_7 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: "x",
        size: (14),
    }));
    const __VLS_8 = __VLS_7({
        name: "x",
        size: (14),
    }, ...__VLS_functionalComponentArgsRest(__VLS_7));
}
/** @type {__VLS_StyleScopedClasses['cm-feedback']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-feedback__actions']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-feedback__close']} */ ;
// @ts-ignore
var __VLS_4 = __VLS_3, __VLS_6 = __VLS_5;
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
const __VLS_component = (await import('vue')).defineComponent({
    setup() {
        return {};
    },
    __typeEmits: {},
    __typeProps: {},
    props: {},
});
export default {};
; /* PartiallyEnd: #4569/main.vue */
