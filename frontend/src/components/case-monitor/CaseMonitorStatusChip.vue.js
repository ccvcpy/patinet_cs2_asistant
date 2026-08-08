import { computed } from "vue";
const props = withDefaults(defineProps(), {
    label: "",
});
const tone = computed(() => {
    const status = props.status.toLowerCase();
    if (["running", "idle", "enabled", "completed"].includes(status))
        return "running";
    if (["collecting", "queued"].includes(status))
        return "collecting";
    if (["reporting"].includes(status))
        return "reporting";
    if (["error", "failed", "offline", "interrupted"].includes(status))
        return "error";
    return "paused";
});
const resolvedLabel = computed(() => {
    if (props.label)
        return props.label;
    const labels = {
        running: "监控运行中",
        idle: "监控运行中",
        enabled: "监控运行中",
        paused: "已暂停",
        stopped: "已暂停",
        collecting: "正在采集",
        queued: "等待执行",
        reporting: "正在生成报告",
        completed: "已完成",
        error: "采集失败",
        failed: "采集失败",
        offline: "后端离线",
        interrupted: "任务已中断",
    };
    return labels[props.status.toLowerCase()] || props.status;
});
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_withDefaultsArg = (function (t) { return t; })({
    label: "",
});
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "cm-status-chip" },
    ...{ class: (`cm-status-chip--${__VLS_ctx.tone}`) },
});
(__VLS_ctx.resolvedLabel);
/** @type {__VLS_StyleScopedClasses['cm-status-chip']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            tone: tone,
            resolvedLabel: resolvedLabel,
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
