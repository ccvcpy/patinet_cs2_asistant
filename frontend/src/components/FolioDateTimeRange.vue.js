import { VueDatePicker } from "@vuepic/vue-datepicker";
import "@vuepic/vue-datepicker/dist/main.css";
import { zhCN } from "date-fns/locale";
const model = defineModel({ required: true });
function startOfToday() {
    const value = new Date();
    value.setHours(0, 0, 0, 0);
    return value;
}
function startOfCurrentMonth() {
    const value = new Date();
    return new Date(value.getFullYear(), value.getMonth(), 1, 0, 0, 0, 0);
}
function daysAgo(days) {
    const value = startOfToday();
    value.setDate(value.getDate() - days);
    return value;
}
function formatDateTime(value) {
    const pad = (part) => String(part).padStart(2, "0");
    return `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(value.getDate())} ${pad(value.getHours())}:${pad(value.getMinutes())}`;
}
function formatRange(value) {
    if (!Array.isArray(value) || !value.length)
        return "";
    if (!value[1])
        return formatDateTime(value[0]);
    return `${formatDateTime(value[0])}  至  ${formatDateTime(value[1])}`;
}
const presets = [
    { label: "今天", value: [startOfToday(), new Date()] },
    { label: "最近 7 天", value: [daysAgo(6), new Date()] },
    { label: "本月", value: [startOfCurrentMonth(), new Date()] },
    { label: "最近 30 天", value: [daysAgo(29), new Date()] },
];
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_defaults = {};
const __VLS_modelEmit = defineEmits();
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
// CSS variable injection 
// CSS variable injection end 
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "folio-date-range" },
});
const __VLS_0 = {}.VueDatePicker;
/** @type {[typeof __VLS_components.VueDatePicker, ]} */ ;
// @ts-ignore
const __VLS_1 = __VLS_asFunctionalComponent(__VLS_0, new __VLS_0({
    modelValue: (__VLS_ctx.model),
    locale: (__VLS_ctx.zhCN),
    range: ({ partialRange: false }),
    multiCalendars: ({ count: 2, solo: true }),
    timeConfig: ({
        enableTimePicker: true,
        enableSeconds: false,
        enableMinutes: true,
        is24: true,
        minutesIncrement: 1,
        timePickerInline: true,
    }),
    formats: ({ input: __VLS_ctx.formatRange, preview: __VLS_ctx.formatRange }),
    presetDates: (__VLS_ctx.presets),
    actionRow: ({
        showSelect: true,
        showCancel: true,
        showNow: false,
        showPreview: true,
        selectBtnLabel: '确认范围',
        cancelBtnLabel: '取消',
    }),
    textInput: ({ enterSubmit: true, tabSubmit: true, openMenu: 'open', rangeSeparator: ' 至 ' }),
    inputAttrs: ({ clearable: false, alwaysClearable: false, required: true, autocomplete: 'off' }),
    weekStart: "1",
    placeholder: "选择开始和结束时间",
    'aria-label': "报表时间范围",
}));
const __VLS_2 = __VLS_1({
    modelValue: (__VLS_ctx.model),
    locale: (__VLS_ctx.zhCN),
    range: ({ partialRange: false }),
    multiCalendars: ({ count: 2, solo: true }),
    timeConfig: ({
        enableTimePicker: true,
        enableSeconds: false,
        enableMinutes: true,
        is24: true,
        minutesIncrement: 1,
        timePickerInline: true,
    }),
    formats: ({ input: __VLS_ctx.formatRange, preview: __VLS_ctx.formatRange }),
    presetDates: (__VLS_ctx.presets),
    actionRow: ({
        showSelect: true,
        showCancel: true,
        showNow: false,
        showPreview: true,
        selectBtnLabel: '确认范围',
        cancelBtnLabel: '取消',
    }),
    textInput: ({ enterSubmit: true, tabSubmit: true, openMenu: 'open', rangeSeparator: ' 至 ' }),
    inputAttrs: ({ clearable: false, alwaysClearable: false, required: true, autocomplete: 'off' }),
    weekStart: "1",
    placeholder: "选择开始和结束时间",
    'aria-label': "报表时间范围",
}, ...__VLS_functionalComponentArgsRest(__VLS_1));
/** @type {__VLS_StyleScopedClasses['folio-date-range']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            VueDatePicker: VueDatePicker,
            zhCN: zhCN,
            model: model,
            formatRange: formatRange,
            presets: presets,
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
