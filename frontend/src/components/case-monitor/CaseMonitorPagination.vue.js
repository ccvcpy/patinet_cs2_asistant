import { computed } from "vue";
import FolioIcon from "../FolioIcon.vue";
const props = withDefaults(defineProps(), {
    pageSizeOptions: () => [10, 20, 50],
    disabled: false,
    compact: false,
});
const emit = defineEmits();
const totalPages = computed(() => Math.max(1, Math.ceil(props.totalItems / props.pageSize)));
const middlePages = computed(() => {
    const visiblePageCount = props.compact ? 3 : 5;
    if (totalPages.value <= visiblePageCount + 1) {
        return Array.from({ length: totalPages.value }, (_, index) => index + 1);
    }
    const start = Math.max(1, Math.min(props.modelValue - Math.floor(visiblePageCount / 2), totalPages.value - visiblePageCount + 1));
    return Array.from({ length: visiblePageCount }, (_, index) => start + index);
});
function choose(page) {
    const bounded = Math.max(1, Math.min(totalPages.value, page));
    emit("update:modelValue", bounded);
}
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_withDefaultsArg = (function (t) { return t; })({
    pageSizeOptions: () => [10, 20, 50],
    disabled: false,
    compact: false,
});
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
__VLS_asFunctionalElement(__VLS_intrinsicElements.footer, __VLS_intrinsicElements.footer)({
    ...{ class: "cm-pagination" },
    'aria-label': "推荐排行分页",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
(__VLS_ctx.totalItems);
(__VLS_ctx.modelValue);
(__VLS_ctx.totalPages);
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "cm-pagination__actions" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.choose(__VLS_ctx.modelValue - 1);
        } },
    ...{ class: "cm-page-button" },
    type: "button",
    'aria-label': "上一页",
    disabled: (__VLS_ctx.disabled || __VLS_ctx.modelValue <= 1),
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_0 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "chevron-left",
    size: (15),
}));
const __VLS_1 = __VLS_0({
    name: "chevron-left",
    size: (15),
}, ...__VLS_functionalComponentArgsRest(__VLS_0));
if (__VLS_ctx.middlePages[0] > 1) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.middlePages[0] > 1))
                    return;
                __VLS_ctx.choose(1);
            } },
        ...{ class: "cm-page-button" },
        type: "button",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "cm-pagination__ellipsis" },
    });
}
for (const [page] of __VLS_getVForSourceType((__VLS_ctx.middlePages))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                __VLS_ctx.choose(page);
            } },
        key: (page),
        ...{ class: "cm-page-button" },
        ...{ class: ({ 'is-active': page === __VLS_ctx.modelValue }) },
        type: "button",
        disabled: (__VLS_ctx.disabled),
        'aria-current': (page === __VLS_ctx.modelValue ? 'page' : undefined),
    });
    (page);
}
if (__VLS_ctx.middlePages[__VLS_ctx.middlePages.length - 1] < __VLS_ctx.totalPages) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "cm-pagination__ellipsis" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.middlePages[__VLS_ctx.middlePages.length - 1] < __VLS_ctx.totalPages))
                    return;
                __VLS_ctx.choose(__VLS_ctx.totalPages);
            } },
        ...{ class: "cm-page-button" },
        type: "button",
    });
    (__VLS_ctx.totalPages);
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.choose(__VLS_ctx.modelValue + 1);
        } },
    ...{ class: "cm-page-button" },
    type: "button",
    'aria-label': "下一页",
    disabled: (__VLS_ctx.disabled || __VLS_ctx.modelValue >= __VLS_ctx.totalPages),
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_3 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "chevron-right",
    size: (15),
}));
const __VLS_4 = __VLS_3({
    name: "chevron-right",
    size: (15),
}, ...__VLS_functionalComponentArgsRest(__VLS_3));
__VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
    ...{ onChange: (...[$event]) => {
            __VLS_ctx.emit('update:pageSize', Number($event.target.value));
        } },
    ...{ class: "cm-page-size" },
    value: (__VLS_ctx.pageSize),
    'aria-label': "每页条数",
    disabled: (__VLS_ctx.disabled),
});
for (const [size] of __VLS_getVForSourceType((__VLS_ctx.pageSizeOptions))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
        key: (size),
        value: (size),
    });
    (size);
}
/** @type {__VLS_StyleScopedClasses['cm-pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-pagination__actions']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-page-button']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-page-button']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-pagination__ellipsis']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-page-button']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-pagination__ellipsis']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-page-button']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-page-button']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-page-size']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            FolioIcon: FolioIcon,
            emit: emit,
            totalPages: totalPages,
            middlePages: middlePages,
            choose: choose,
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
