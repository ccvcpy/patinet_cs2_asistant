const __VLS_props = withDefaults(defineProps(), { size: 18, strokeWidth: 1.7, label: "" });
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_withDefaultsArg = (function (t) { return t; })({ size: 18, strokeWidth: 1.7, label: "" });
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
// CSS variable injection 
// CSS variable injection end 
__VLS_asFunctionalElement(__VLS_intrinsicElements.svg, __VLS_intrinsicElements.svg)({
    ...{ class: "folio-icon" },
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    'stroke-linecap': "round",
    'stroke-linejoin': "round",
    'stroke-width': (__VLS_ctx.strokeWidth),
    width: (__VLS_ctx.size),
    height: (__VLS_ctx.size),
    'aria-hidden': (__VLS_ctx.label ? undefined : true),
    'aria-label': (__VLS_ctx.label || undefined),
    role: "img",
});
if (__VLS_ctx.name === 'account') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.circle)({
        cx: "12",
        cy: "8",
        r: "3.5",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "M5 20c.6-4 3-6 7-6s6.4 2 7 6",
    });
}
else if (__VLS_ctx.name === 'wallet') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "M4 7h14a2 2 0 0 1 2 2v10H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h12",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "M16 12h6v5h-6a2.5 2.5 0 0 1 0-5Z",
    });
}
else if (__VLS_ctx.name === 'report') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "M5 3h14v18H5zM8 8h8M8 12h8M8 16h5",
    });
}
else if (__VLS_ctx.name === 'case') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "m4 7 8-4 8 4-8 4-8-4Zm0 0v10l8 4 8-4V7M12 11v10",
    });
}
else if (__VLS_ctx.name === 'scan') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "M4 8V4h4M16 4h4v4M20 16v4h-4M8 20H4v-4M8 12h8",
    });
}
else if (__VLS_ctx.name === 'price') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "M20 13 13 20 4 11V4h7l9 9Z",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "M8 8h.01",
    });
}
else if (__VLS_ctx.name === 'clock') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.circle)({
        cx: "12",
        cy: "12",
        r: "9",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "M12 7v5l3 2",
    });
}
else if (__VLS_ctx.name === 'refresh') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "M20 7v5h-5M4 17v-5h5M6.2 8.5A7 7 0 0 1 18.8 7M5.2 17A7 7 0 0 0 17.8 15.5",
    });
}
else if (__VLS_ctx.name === 'play') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "m9 6 9 6-9 6V6Z",
    });
}
else if (__VLS_ctx.name === 'pause') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "M9 7v10M15 7v10",
    });
}
else if (__VLS_ctx.name === 'link') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "m10 14 4-4m-6.5 7.5-1 1a3.5 3.5 0 0 1-5-5l3-3a3.5 3.5 0 0 1 5 0m7-4 1-1a3.5 3.5 0 0 1 5 5l-3 3a3.5 3.5 0 0 1-5 0",
    });
}
else if (__VLS_ctx.name === 'shield') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "M12 3 20 6v5c0 5-3.2 8.3-8 10-4.8-1.7-8-5-8-10V6l8-3Z",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "m9 12 2 2 4-4",
    });
}
else if (__VLS_ctx.name === 'warning') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "m12 3 10 18H2L12 3Z",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "M12 9v5M12 17h.01",
    });
}
else if (__VLS_ctx.name === 'success') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.circle)({
        cx: "12",
        cy: "12",
        r: "9",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "m8 12 2.6 2.6L16.5 9",
    });
}
else if (__VLS_ctx.name === 'error') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.circle)({
        cx: "12",
        cy: "12",
        r: "9",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "m9 9 6 6m0-6-6 6",
    });
}
else if (__VLS_ctx.name === 'edit') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "M4 20h4l11-11a2.8 2.8 0 0 0-4-4L4 16v4Z",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "m13.5 6.5 4 4M12 20h8",
    });
}
else if (__VLS_ctx.name === 'calendar') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.rect)({
        x: "3",
        y: "5",
        width: "18",
        height: "16",
        rx: "2",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "M7 3v4M17 3v4M3 10h18M8 14h.01M12 14h.01M16 14h.01M8 18h.01M12 18h.01",
    });
}
else if (__VLS_ctx.name === 'lock') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.rect)({
        x: "5",
        y: "10",
        width: "14",
        height: "11",
        rx: "2",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "M8 10V7a4 4 0 0 1 8 0v3M12 14v3",
    });
}
else if (__VLS_ctx.name === 'rocket') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "M14 5c2.7-2.7 5.2-2 5.2-2s.7 2.5-2 5.2l-5.4 5.4-3.4-3.4L14 5Z",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "m13 12 1 5-3 3-2-5M10 11l-5-1-3 3 5 1M15.5 6.5h.01M5 19c1.3-2.7 3.3-2.7 4-2",
    });
}
else if (__VLS_ctx.name === 'document') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "M6 3h8l4 4v14H6zM14 3v5h4M9 12h6M9 16h6",
    });
}
else if (__VLS_ctx.name === 'circle-dashed') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.circle)({
        cx: "12",
        cy: "12",
        r: "8.5",
        'stroke-dasharray': "2.2 2.4",
    });
}
else if (__VLS_ctx.name === 'chevron-up') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "m6 15 6-6 6 6",
    });
}
else if (__VLS_ctx.name === 'chevron-down') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "m6 9 6 6 6-6",
    });
}
else if (__VLS_ctx.name === 'chevron-left') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "m15 18-6-6 6-6",
    });
}
else if (__VLS_ctx.name === 'chevron-right') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "m9 18 6-6-6-6",
    });
}
else if (__VLS_ctx.name === 'search') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.circle)({
        cx: "11",
        cy: "11",
        r: "7",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "m20 20-4-4",
    });
}
else if (__VLS_ctx.name === 'download') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "M12 3v12m0 0 5-5m-5 5-5-5M4 19v2h16v-2",
    });
}
else if (__VLS_ctx.name === 'x') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "m6 6 12 12M18 6 6 18",
    });
}
else if (__VLS_ctx.name === 'info') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.circle)({
        cx: "12",
        cy: "12",
        r: "9",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "M12 11v5M12 8h.01",
    });
}
else if (__VLS_ctx.name === 'crown') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "m3 7 4.5 4L12 5l4.5 6L21 7l-2 11H5L3 7Z",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "M5 18h14",
    });
}
else if (__VLS_ctx.name === 'bell') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4",
    });
}
else if (__VLS_ctx.name === 'menu') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "M4 6h16M4 12h16M4 18h16",
    });
}
else if (__VLS_ctx.name === 'grid') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.rect)({
        x: "3",
        y: "3",
        width: "7",
        height: "7",
        rx: "1",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.rect)({
        x: "14",
        y: "3",
        width: "7",
        height: "7",
        rx: "1",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.rect)({
        x: "3",
        y: "14",
        width: "7",
        height: "7",
        rx: "1",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.rect)({
        x: "14",
        y: "14",
        width: "7",
        height: "7",
        rx: "1",
    });
}
else if (__VLS_ctx.name === 'user') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.circle)({
        cx: "12",
        cy: "8",
        r: "3.5",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "M5 21a7 7 0 0 1 14 0",
    });
}
else if (__VLS_ctx.name === 'sparkles') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "m12 3 1.2 3.8L17 8l-3.8 1.2L12 13l-1.2-3.8L7 8l3.8-1.2L12 3ZM19 14l.7 2.3L22 17l-2.3.7L19 20l-.7-2.3L16 17l2.3-.7L19 14ZM5 13l.7 2.3L8 16l-2.3.7L5 19l-.7-2.3L2 16l2.3-.7L5 13Z",
    });
}
else {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.circle)({
        cx: "12",
        cy: "12",
        r: "3",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.path)({
        d: "M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1a1.7 1.7 0 0 0 1.9.3A1.7 1.7 0 0 0 10 3V2.8h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z",
    });
}
/** @type {__VLS_StyleScopedClasses['folio-icon']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {};
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
