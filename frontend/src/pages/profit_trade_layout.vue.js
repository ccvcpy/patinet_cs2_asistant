import { computed, onMounted, onUnmounted, ref } from "vue";
import { RouterLink, RouterView } from "vue-router";
import FolioIcon from "../components/FolioIcon.vue";
const apiOnline = ref(null);
const realExecutionAllowed = ref(false);
const busy = ref(false);
const error = ref("");
const listingsCircuit = ref({ status: "closed", isBlocking: false });
const runtimeCookies = ref({});
const profitRuntime = ref({});
const cookieExpanded = ref(false);
const nowMs = ref(Date.now());
let statusTimer = null;
let countdownTimer = null;
const circuitRecoveredRecently = computed(() => {
    if (listingsCircuit.value.status !== "closed" || !listingsCircuit.value.lastRecoveredAt)
        return false;
    const recoveredAt = new Date(listingsCircuit.value.lastRecoveredAt).getTime();
    return Number.isFinite(recoveredAt) && nowMs.value - recoveredAt < 5 * 60 * 1000;
});
const circuitVisible = computed(() => Boolean(listingsCircuit.value.isBlocking) || circuitRecoveredRecently.value);
const circuitRemainingLabel = computed(() => {
    const target = listingsCircuit.value.nextProbeAt || listingsCircuit.value.cooldownUntil;
    if (!target)
        return listingsCircuit.value.status === "half_open" ? "等待单次恢复探测" : "-";
    const seconds = Math.max(0, Math.ceil((new Date(target).getTime() - nowMs.value) / 1000));
    const minutes = Math.floor(seconds / 60);
    return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
});
function localTime(value) {
    if (!value)
        return "-";
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("zh-CN", { hour12: false });
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
        const [response, cookieResponse, runtimeResponse] = await Promise.all([
            fetch("/api/profit-trade/dashboard", { cache: "no-store" }),
            fetch("/api/runtime/cookies", { cache: "no-store" }),
            fetch("/api/runtime/state?executor=profit_trade", { cache: "no-store" }),
        ]);
        if (!response.ok)
            throw new Error(await readError(response));
        const payload = await response.json();
        apiOnline.value = true;
        realExecutionAllowed.value = Boolean(payload.config?.allowRealExecution);
        listingsCircuit.value = payload.listingsCircuit || { status: "closed", isBlocking: false };
        if (cookieResponse.ok) {
            const cookiePayload = await cookieResponse.json();
            runtimeCookies.value = cookiePayload.gate && typeof cookiePayload.gate === "object"
                ? cookiePayload.gate
                : cookiePayload.data || cookiePayload;
        }
        if (runtimeResponse.ok) {
            const runtimePayload = await runtimeResponse.json();
            profitRuntime.value = runtimePayload.state || runtimePayload.data || runtimePayload;
        }
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
    countdownTimer = setInterval(() => { nowMs.value = Date.now(); }, 1000);
    window.addEventListener("profit-trade:dashboard-status", handleDashboardStatus);
});
onUnmounted(() => {
    if (statusTimer !== null)
        clearInterval(statusTimer);
    if (countdownTimer !== null)
        clearInterval(countdownTimer);
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
/** @type {__VLS_StyleScopedClasses['profit-cookie-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-cookie-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-cookie-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-cookie-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-cookie-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-cookie-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-cookie-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-cookie-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-cookie-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-cookie-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-cookie-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-cookie-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-cookie-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['circuit-main']} */ ;
/** @type {__VLS_StyleScopedClasses['circuit-main']} */ ;
/** @type {__VLS_StyleScopedClasses['listings-circuit-banner']} */ ;
/** @type {__VLS_StyleScopedClasses['listings-circuit-banner']} */ ;
/** @type {__VLS_StyleScopedClasses['listings-circuit-banner']} */ ;
/** @type {__VLS_StyleScopedClasses['listings-circuit-banner']} */ ;
/** @type {__VLS_StyleScopedClasses['listings-circuit-banner']} */ ;
/** @type {__VLS_StyleScopedClasses['listings-circuit-banner']} */ ;
/** @type {__VLS_StyleScopedClasses['recovered']} */ ;
/** @type {__VLS_StyleScopedClasses['circuit-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['listings-circuit-banner']} */ ;
/** @type {__VLS_StyleScopedClasses['recovered']} */ ;
/** @type {__VLS_StyleScopedClasses['circuit-main']} */ ;
/** @type {__VLS_StyleScopedClasses['listings-circuit-banner']} */ ;
/** @type {__VLS_StyleScopedClasses['recovered']} */ ;
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
    ...{ class: (['runtime-dot', __VLS_ctx.profitRuntime.enabled ? 'online' : 'unknown']) },
});
(__VLS_ctx.profitRuntime.preparing ? "准备中" : __VLS_ctx.profitRuntime.enabled ? "运行中" : "已关闭");
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.cookieExpanded = !__VLS_ctx.cookieExpanded;
        } },
    ...{ class: "runtime-cookie-button" },
    type: "button",
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_15 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "shield",
    size: (14),
}));
const __VLS_16 = __VLS_15({
    name: "shield",
    size: (14),
}, ...__VLS_functionalComponentArgsRest(__VLS_15));
(__VLS_ctx.runtimeCookies.validCount ?? "—");
(__VLS_ctx.runtimeCookies.totalCount ?? "—");
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.emergencyDisable) },
    ...{ class: "emergency-stop" },
    type: "button",
    disabled: (__VLS_ctx.busy || !__VLS_ctx.realExecutionAllowed),
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_18 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "shield",
    size: (15),
}));
const __VLS_19 = __VLS_18({
    name: "shield",
    size: (15),
}, ...__VLS_functionalComponentArgsRest(__VLS_18));
(__VLS_ctx.busy ? "正在关闭" : "紧急关闭真实执行");
if (__VLS_ctx.cookieExpanded) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "profit-cookie-panel" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    const __VLS_21 = {}.RouterLink;
    /** @type {[typeof __VLS_components.RouterLink, typeof __VLS_components.RouterLink, ]} */ ;
    // @ts-ignore
    const __VLS_22 = __VLS_asFunctionalComponent(__VLS_21, new __VLS_21({
        to: "/guadao/overview",
    }));
    const __VLS_23 = __VLS_22({
        to: "/guadao/overview",
    }, ...__VLS_functionalComponentArgsRest(__VLS_22));
    __VLS_24.slots.default;
    var __VLS_24;
    if (__VLS_ctx.runtimeCookies.accounts?.length) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "profit-cookie-grid" },
        });
        for (const [account] of __VLS_getVForSourceType((__VLS_ctx.runtimeCookies.accounts))) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
                key: (account.accountId || account.steamId),
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
            (account.accountName || account.name || "未命名账号");
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
            (account.steamId || "—");
            __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({
                ...{ class: ({ valid: account.valid }) },
            });
            (account.valid ? "有效" : account.error || account.status || "未知");
            __VLS_asFunctionalElement(__VLS_intrinsicElements.time, __VLS_intrinsicElements.time)({});
            (__VLS_ctx.localTime(account.lastCheckedAt));
        }
    }
    else {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    }
}
if (__VLS_ctx.error) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "profit-layout-error" },
    });
    (__VLS_ctx.error);
}
if (__VLS_ctx.circuitVisible) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: (['listings-circuit-banner', { recovered: __VLS_ctx.circuitRecoveredRecently }]) },
        'aria-live': "polite",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "circuit-icon" },
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_25 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: (__VLS_ctx.circuitRecoveredRecently ? 'shield' : 'clock'),
        size: (18),
    }));
    const __VLS_26 = __VLS_25({
        name: (__VLS_ctx.circuitRecoveredRecently ? 'shield' : 'clock'),
        size: (18),
    }, ...__VLS_functionalComponentArgsRest(__VLS_25));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "circuit-main" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.circuitRecoveredRecently ? "Steam listings 已恢复" : __VLS_ctx.listingsCircuit.status === "half_open" ? "Steam listings 等待恢复探测" : "Steam listings 查询冷却中");
    if (!__VLS_ctx.circuitRecoveredRecently) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    }
    else {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    }
    if (!__VLS_ctx.circuitRecoveredRecently) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.listingsCircuit.triggerAccountName || __VLS_ctx.listingsCircuit.triggerAccountId || "-");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.listingsCircuit.consecutive429Count || 0);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.circuitRemainingLabel);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.localTime(__VLS_ctx.listingsCircuit.nextProbeAt || __VLS_ctx.listingsCircuit.cooldownUntil));
    }
    else {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.localTime(__VLS_ctx.listingsCircuit.lastRecoveredAt));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.listingsCircuit.triggerMarketHashName || "-");
    }
}
const __VLS_28 = {}.RouterView;
/** @type {[typeof __VLS_components.RouterView, typeof __VLS_components.RouterView, ]} */ ;
// @ts-ignore
const __VLS_29 = __VLS_asFunctionalComponent(__VLS_28, new __VLS_28({}));
const __VLS_30 = __VLS_29({}, ...__VLS_functionalComponentArgsRest(__VLS_29));
{
    const { default: __VLS_thisSlot } = __VLS_31.slots;
    const [{ Component }] = __VLS_getSlotParams(__VLS_thisSlot);
    const __VLS_32 = {}.KeepAlive;
    /** @type {[typeof __VLS_components.KeepAlive, typeof __VLS_components.KeepAlive, ]} */ ;
    // @ts-ignore
    const __VLS_33 = __VLS_asFunctionalComponent(__VLS_32, new __VLS_32({}));
    const __VLS_34 = __VLS_33({}, ...__VLS_functionalComponentArgsRest(__VLS_33));
    __VLS_35.slots.default;
    const __VLS_36 = ((Component));
    // @ts-ignore
    const __VLS_37 = __VLS_asFunctionalComponent(__VLS_36, new __VLS_36({}));
    const __VLS_38 = __VLS_37({}, ...__VLS_functionalComponentArgsRest(__VLS_37));
    var __VLS_35;
    __VLS_31.slots['' /* empty slot name completion */];
}
var __VLS_31;
/** @type {__VLS_StyleScopedClasses['profit-trade-workspace']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-workspace-bar']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-workspace-brand']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-workspace-mark']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-subnav']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-runtime-strip']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-cookie-button']} */ ;
/** @type {__VLS_StyleScopedClasses['emergency-stop']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-cookie-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-cookie-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-layout-error']} */ ;
/** @type {__VLS_StyleScopedClasses['circuit-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['circuit-main']} */ ;
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
            listingsCircuit: listingsCircuit,
            runtimeCookies: runtimeCookies,
            profitRuntime: profitRuntime,
            cookieExpanded: cookieExpanded,
            circuitRecoveredRecently: circuitRecoveredRecently,
            circuitVisible: circuitVisible,
            circuitRemainingLabel: circuitRemainingLabel,
            localTime: localTime,
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
