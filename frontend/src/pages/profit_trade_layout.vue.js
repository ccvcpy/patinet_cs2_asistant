import { onMounted, onUnmounted, ref } from "vue";
import { RouterLink, RouterView } from "vue-router";
import FolioIcon from "../components/FolioIcon.vue";
const apiOnline = ref(null);
const realExecutionAllowed = ref(false);
const busy = ref(false);
const error = ref("");
let statusTimer = null;
const autoRunStorageKey = "profitTrade.autoRun.v1";
function readAutoRunState() {
    try {
        return Boolean(JSON.parse(window.localStorage.getItem(autoRunStorageKey) || "null")?.enabled);
    }
    catch {
        return false;
    }
}
const autoRunActive = ref(readAutoRunState());
function handleRuntimeState(event) {
    const detail = event.detail;
    autoRunActive.value = typeof detail?.active === "boolean" ? detail.active : readAutoRunState();
}
function handleDashboardStatus(event) {
    const detail = event.detail;
    if (typeof detail?.allowRealExecution === "boolean") {
        realExecutionAllowed.value = detail.allowRealExecution;
        apiOnline.value = true;
    }
}
async function readError(response) {
    try {
        const body = await response.json();
        return body.error || body.detail || response.statusText;
    }
    catch {
        return response.statusText;
    }
}
async function refreshSharedStatus() {
    try {
        const response = await fetch("/api/profit-trade/dashboard", { cache: "no-store" });
        if (!response.ok)
            throw new Error(await readError(response));
        const payload = await response.json();
        apiOnline.value = true;
        realExecutionAllowed.value = Boolean(payload.config?.allowRealExecution);
        error.value = "";
    }
    catch (reason) {
        apiOnline.value = false;
        error.value = reason instanceof Error ? reason.message : String(reason);
    }
}
async function emergencyDisable() {
    if (!realExecutionAllowed.value || busy.value)
        return;
    busy.value = true;
    error.value = "";
    try {
        const response = await fetch("/api/profit-trade/config", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ allowRealExecution: false }),
        });
        if (!response.ok)
            throw new Error(await readError(response));
        realExecutionAllowed.value = false;
        apiOnline.value = true;
        window.dispatchEvent(new CustomEvent("profit-trade:config-changed"));
    }
    catch (reason) {
        error.value = `紧急关闭失败：${reason instanceof Error ? reason.message : String(reason)}`;
    }
    finally {
        busy.value = false;
    }
}
onMounted(() => {
    void refreshSharedStatus();
    statusTimer = setInterval(() => void refreshSharedStatus(), 30000);
    window.addEventListener("profit-trade:runtime-state", handleRuntimeState);
    window.addEventListener("profit-trade:dashboard-status", handleDashboardStatus);
});
onUnmounted(() => {
    if (statusTimer !== null)
        clearInterval(statusTimer);
    window.removeEventListener("profit-trade:runtime-state", handleRuntimeState);
    window.removeEventListener("profit-trade:dashboard-status", handleDashboardStatus);
});
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
/** @type {__VLS_StyleScopedClasses['profit-workspace-brand']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-workspace-brand']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-subnav']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-subnav']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-dot']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-dot']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-dot']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-dot']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-dot']} */ ;
/** @type {__VLS_StyleScopedClasses['emergency-stop']} */ ;
// CSS variable injection
// CSS variable injection end
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "profit-trade-workspace" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({
    ...{ class: "profit-workspace-bar" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "profit-workspace-brand" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "profit-workspace-mark" },
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_0 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "scan",
    size: (18),
}));
const __VLS_1 = __VLS_0({
    name: "scan",
    size: (18),
}, ...__VLS_functionalComponentArgsRest(__VLS_0));
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.nav, __VLS_intrinsicElements.nav)({
    ...{ class: "profit-subnav" },
    'aria-label': "Profit Trade 页面",
});
const __VLS_3 = {}.RouterLink;
/** @type {[typeof __VLS_components.RouterLink, typeof __VLS_components.RouterLink, ]} */ ;
// @ts-ignore
const __VLS_4 = __VLS_asFunctionalComponent(__VLS_3, new __VLS_3({
    to: "/profit-trade/overview",
}));
const __VLS_5 = __VLS_4({
    to: "/profit-trade/overview",
}, ...__VLS_functionalComponentArgsRest(__VLS_4));
__VLS_6.slots.default;
var __VLS_6;
const __VLS_7 = {}.RouterLink;
/** @type {[typeof __VLS_components.RouterLink, typeof __VLS_components.RouterLink, ]} */ ;
// @ts-ignore
const __VLS_8 = __VLS_asFunctionalComponent(__VLS_7, new __VLS_7({
    to: "/profit-trade/interruptions",
}));
const __VLS_9 = __VLS_8({
    to: "/profit-trade/interruptions",
}, ...__VLS_functionalComponentArgsRest(__VLS_8));
__VLS_10.slots.default;
var __VLS_10;
const __VLS_11 = {}.RouterLink;
/** @type {[typeof __VLS_components.RouterLink, typeof __VLS_components.RouterLink, ]} */ ;
// @ts-ignore
const __VLS_12 = __VLS_asFunctionalComponent(__VLS_11, new __VLS_11({
    to: "/profit-trade/logs",
}));
const __VLS_13 = __VLS_12({
    to: "/profit-trade/logs",
}, ...__VLS_functionalComponentArgsRest(__VLS_12));
__VLS_14.slots.default;
var __VLS_14;
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "profit-runtime-strip" },
    'aria-label': "Profit Trade 运行状态",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: (['runtime-dot', __VLS_ctx.apiOnline === true ? 'online' : __VLS_ctx.apiOnline === false ? 'offline' : 'unknown']) },
});
(__VLS_ctx.apiOnline === true ? "在线" : __VLS_ctx.apiOnline === false ? "离线" : "检查中");
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: (['runtime-dot', __VLS_ctx.realExecutionAllowed ? 'danger' : 'safe']) },
});
(__VLS_ctx.realExecutionAllowed ? "开放" : "关闭");
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: (['runtime-dot', __VLS_ctx.autoRunActive ? 'online' : 'unknown']) },
});
(__VLS_ctx.autoRunActive ? "运行中" : "未运行");
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.emergencyDisable) },
    ...{ class: "emergency-stop" },
    type: "button",
    disabled: (__VLS_ctx.busy || !__VLS_ctx.realExecutionAllowed),
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_15 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "shield",
    size: (15),
}));
const __VLS_16 = __VLS_15({
    name: "shield",
    size: (15),
}, ...__VLS_functionalComponentArgsRest(__VLS_15));
(__VLS_ctx.busy ? "正在关闭" : "紧急关闭真实执行");
if (__VLS_ctx.error) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "profit-layout-error" },
    });
    (__VLS_ctx.error);
}
const __VLS_18 = {}.RouterView;
/** @type {[typeof __VLS_components.RouterView, typeof __VLS_components.RouterView, ]} */ ;
// @ts-ignore
const __VLS_19 = __VLS_asFunctionalComponent(__VLS_18, new __VLS_18({}));
const __VLS_20 = __VLS_19({}, ...__VLS_functionalComponentArgsRest(__VLS_19));
{
    const { default: __VLS_thisSlot } = __VLS_21.slots;
    const [{ Component }] = __VLS_getSlotParams(__VLS_thisSlot);
    const __VLS_22 = {}.KeepAlive;
    /** @type {[typeof __VLS_components.KeepAlive, typeof __VLS_components.KeepAlive, ]} */ ;
    // @ts-ignore
    const __VLS_23 = __VLS_asFunctionalComponent(__VLS_22, new __VLS_22({}));
    const __VLS_24 = __VLS_23({}, ...__VLS_functionalComponentArgsRest(__VLS_23));
    __VLS_25.slots.default;
    const __VLS_26 = ((Component));
    // @ts-ignore
    const __VLS_27 = __VLS_asFunctionalComponent(__VLS_26, new __VLS_26({}));
    const __VLS_28 = __VLS_27({}, ...__VLS_functionalComponentArgsRest(__VLS_27));
    var __VLS_25;
    __VLS_21.slots['' /* empty slot name completion */];
}
var __VLS_21;
/** @type {__VLS_StyleScopedClasses['profit-trade-workspace']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-workspace-bar']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-workspace-brand']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-workspace-mark']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-subnav']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-runtime-strip']} */ ;
/** @type {__VLS_StyleScopedClasses['emergency-stop']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-layout-error']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            RouterLink: RouterLink,
            RouterView: RouterView,
            FolioIcon: FolioIcon,
            apiOnline: apiOnline,
            realExecutionAllowed: realExecutionAllowed,
            busy: busy,
            error: error,
            autoRunActive: autoRunActive,
            emergencyDisable: emergencyDisable,
        };
    },
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
});
; /* PartiallyEnd: #4569/main.vue */
