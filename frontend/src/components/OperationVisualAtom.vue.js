import { computed } from "vue";
const props = withDefaults(defineProps(), {
    size: 44,
    alt: "",
});
const src = computed(() => `/images/operations/atoms/${props.name}.webp`);
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_withDefaultsArg = (function (t) { return t; })({
    size: 44,
    alt: "",
});
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
// CSS variable injection 
// CSS variable injection end 
__VLS_asFunctionalElement(__VLS_intrinsicElements.img)({
    ...{ class: "operation-visual-atom" },
    src: (__VLS_ctx.src),
    alt: (__VLS_ctx.alt),
    'aria-hidden': (__VLS_ctx.alt ? undefined : 'true'),
    width: (__VLS_ctx.size),
    height: (__VLS_ctx.size),
    decoding: "async",
});
/** @type {__VLS_StyleScopedClasses['operation-visual-atom']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            src: src,
        };
    },
    __typeProps: {},
    props: {},
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
    __typeProps: {},
    props: {},
});
; /* PartiallyEnd: #4569/main.vue */
