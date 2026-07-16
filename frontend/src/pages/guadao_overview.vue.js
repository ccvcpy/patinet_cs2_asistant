import { computed, onActivated, onDeactivated, onMounted, onUnmounted, ref } from "vue";
import { RouterLink } from "vue-router";
import FolioIcon from "../components/FolioIcon.vue";
import { formatCountdown, formatLocal, responseError, unwrapPayload } from "./guadao_shared";
const dashboard = ref(null);
const loading = ref(false);
const actionBusy = ref(false);
const error = ref("");
const notice = ref("");
const confirmAction = ref(null);
let timer = null;
const runtime = computed(() => dashboard.value?.runtime || {});
const cookieGate = computed(() => dashboard.value?.steamAuthHealth || dashboard.value?.cookieGate || {});
const cookieAccounts = computed(() => cookieGate.value.accounts || []);
const enabled = computed(() => Boolean(runtime.value.enabled));
const gateReady = computed(() => (cookieGate.value.totalCount || 0) > 0 && cookieGate.value.validCount === cookieGate.value.totalCount);
const failedCookieAccounts = computed(() => cookieAccounts.value.filter(account => !account.valid && account.status !== "refreshing"));
const cookieProgress = computed(() => {
    const total = Number(cookieGate.value.totalCount || 0);
    return total > 0 ? Math.min(100, Math.max(0, Number(cookieGate.value.validCount || 0) / total * 100)) : 0;
});
const runtimeStatus = computed(() => String(runtime.value.runtimeStatus || runtime.value.status || ""));
const runtimeLabel = computed(() => {
    if (runtime.value.migrationHold)
        return "迁移保护中";
    if (runtimeStatus.value === "closing_only")
        return "存量闭环中";
    if (cookieGate.value.status === "degraded")
        return "降级运行中";
    if (runtime.value.preparing || runtimeStatus.value === "preparing" || (enabled.value && !gateReady.value))
        return "启动准备中";
    return enabled.value ? "运行中" : "已关闭";
});
const runtimeMessage = computed(() => {
    if (runtime.value.migrationHold)
        return "迁移保护期间只读审计，不发送 Steam/C5 真实写操作。";
    if (runtimeStatus.value === "closing_only")
        return "新扫描与新上架已停止；已有挂单、卖出确认、补仓与发货确认继续安全闭环。";
    if (cookieGate.value.status === "degraded")
        return "仅暂停认证异常账号的新动作；其他有效账号与已有安全闭环继续运行。";
    if (enabled.value && !gateReady.value)
        return "新扫描与新上架暂未启动；已有流水继续安全闭环。";
    return runtime.value.lastRunSummary || (enabled.value ? "新扫描与新上架已开放。" : "执行器已关闭，未发现存量闭环任务。");
});
const quietWindow = computed(() => (dashboard.value?.steamScheduler?.circuits || []).find(row => row.scope === "quiet" && row.state === "open"));
const activeCircuits = computed(() => (dashboard.value?.steamScheduler?.circuits || []).filter(row => row.scope !== "quiet" && (row.state === "open" || row.state === "half_open")));
const routeCooldownUntil = computed(() => activeCircuits.value.map(row => row.nextProbeAt || row.cooldownUntil).filter(Boolean).sort()[0] || null);
const confirmCopy = computed(() => {
    if (confirmAction.value === "enable")
        return { title: "开启挂刀执行器", text: "后端将先刷新并验证全部 Steam 账号 Cookie。只有达到全部有效后，才会开放新扫描与新挂刀。", button: "确认开启" };
    if (confirmAction.value === "disable")
        return { title: "关闭挂刀执行器", text: "关闭后停止新扫描、新上架和新的非必要动作；已有挂单同步、卖出确认、补仓、C5 发货确认和结算仍会继续，并可能产生真实 Steam/C5 写操作。", button: "确认关闭" };
    if (confirmAction.value === "retry-auth")
        return { title: "立即重试失败账号", text: "只把当前认证失败或网络状态未知的账号重新排到 Steam Cookie 恢复队列；不会刷新已经有效的账号，也不会绕过共享 Steam 请求调度。", button: "确认重试" };
    return { title: "刷新全部 Steam Cookie", text: "这会为全部本地 Steam 账号重新建立认证刷新批次，并通过共享请求调度依次执行。期间新扫描和新上架仍服从 Cookie 门禁。", button: "确认全部刷新" };
});
async function refresh() {
    loading.value = true;
    try {
        const response = await fetch("/api/guadao/dashboard", { cache: "no-store" });
        if (!response.ok)
            throw new Error(await responseError(response));
        dashboard.value = unwrapPayload(await response.json(), "dashboard");
        error.value = "";
    }
    catch (reason) {
        error.value = reason instanceof Error ? reason.message : String(reason);
    }
    finally {
        loading.value = false;
    }
}
async function post(path, body = {}) {
    actionBusy.value = true;
    notice.value = "";
    try {
        const response = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
        if (!response.ok)
            throw new Error(await responseError(response));
        notice.value = "操作已提交，正在读取最新状态。";
        confirmAction.value = null;
        await refresh();
    }
    catch (reason) {
        error.value = reason instanceof Error ? reason.message : String(reason);
    }
    finally {
        actionBusy.value = false;
    }
}
function submitConfirmed() {
    if (confirmAction.value === "enable")
        void post("/api/guadao/runtime/toggle", { enabled: true });
    else if (confirmAction.value === "disable")
        void post("/api/guadao/runtime/toggle", { enabled: false });
    else if (confirmAction.value === "retry-auth")
        void post("/api/guadao/auth/retry-failed");
    else if (confirmAction.value === "refresh-auth")
        void post("/api/guadao/cookies/refresh");
}
function startPolling() { if (timer === null)
    timer = setInterval(() => void refresh(), 10000); }
function stopPolling() { if (timer !== null)
    clearInterval(timer); timer = null; }
onMounted(() => { void refresh(); startPolling(); });
onActivated(startPolling);
onDeactivated(stopPolling);
onUnmounted(stopPolling);
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
/** @type {__VLS_StyleScopedClasses['overview-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['overview-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-card-top']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-card-top']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-card-top']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-state']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-card']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['switch']} */ ;
/** @type {__VLS_StyleScopedClasses['switch']} */ ;
/** @type {__VLS_StyleScopedClasses['switch']} */ ;
/** @type {__VLS_StyleScopedClasses['on']} */ ;
/** @type {__VLS_StyleScopedClasses['switch']} */ ;
/** @type {__VLS_StyleScopedClasses['on']} */ ;
/** @type {__VLS_StyleScopedClasses['switch']} */ ;
/** @type {__VLS_StyleScopedClasses['on']} */ ;
/** @type {__VLS_StyleScopedClasses['migration-banner']} */ ;
/** @type {__VLS_StyleScopedClasses['migration-banner']} */ ;
/** @type {__VLS_StyleScopedClasses['migration-banner']} */ ;
/** @type {__VLS_StyleScopedClasses['feedback']} */ ;
/** @type {__VLS_StyleScopedClasses['feedback']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-row']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-row']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-row']} */ ;
/** @type {__VLS_StyleScopedClasses['section-title']} */ ;
/** @type {__VLS_StyleScopedClasses['section-title']} */ ;
/** @type {__VLS_StyleScopedClasses['section-title']} */ ;
/** @type {__VLS_StyleScopedClasses['section-title']} */ ;
/** @type {__VLS_StyleScopedClasses['cookie-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['cookie-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['cookie-table']} */ ;
/** @type {__VLS_StyleScopedClasses['pill']} */ ;
/** @type {__VLS_StyleScopedClasses['success']} */ ;
/** @type {__VLS_StyleScopedClasses['pill']} */ ;
/** @type {__VLS_StyleScopedClasses['pill']} */ ;
/** @type {__VLS_StyleScopedClasses['empty-state']} */ ;
/** @type {__VLS_StyleScopedClasses['task-list']} */ ;
/** @type {__VLS_StyleScopedClasses['task-list']} */ ;
/** @type {__VLS_StyleScopedClasses['task-list']} */ ;
/** @type {__VLS_StyleScopedClasses['task-list']} */ ;
/** @type {__VLS_StyleScopedClasses['task-list']} */ ;
/** @type {__VLS_StyleScopedClasses['scheduler-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['scheduler-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['scheduler-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['priority-list']} */ ;
/** @type {__VLS_StyleScopedClasses['priority-list']} */ ;
/** @type {__VLS_StyleScopedClasses['circuit-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['circuit-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['circuit-list']} */ ;
/** @type {__VLS_StyleScopedClasses['circuit-list']} */ ;
/** @type {__VLS_StyleScopedClasses['circuit-list']} */ ;
/** @type {__VLS_StyleScopedClasses['circuit-list']} */ ;
/** @type {__VLS_StyleScopedClasses['circuit-list']} */ ;
/** @type {__VLS_StyleScopedClasses['circuit-list']} */ ;
/** @type {__VLS_StyleScopedClasses['circuit-list']} */ ;
/** @type {__VLS_StyleScopedClasses['circuit-list']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-list']} */ ;
/** @type {__VLS_StyleScopedClasses['log-preview']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-list']} */ ;
/** @type {__VLS_StyleScopedClasses['log-preview']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-list']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-list']} */ ;
/** @type {__VLS_StyleScopedClasses['log-preview']} */ ;
/** @type {__VLS_StyleScopedClasses['log-preview']} */ ;
/** @type {__VLS_StyleScopedClasses['log-preview']} */ ;
/** @type {__VLS_StyleScopedClasses['log-preview']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-dialog']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-dialog']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-dialog']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-card']} */ ;
/** @type {__VLS_StyleScopedClasses['cookie-progress']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-card']} */ ;
/** @type {__VLS_StyleScopedClasses['cookie-legend']} */ ;
/** @type {__VLS_StyleScopedClasses['task-list']} */ ;
/** @type {__VLS_StyleScopedClasses['task-list']} */ ;
/** @type {__VLS_StyleScopedClasses['task-list']} */ ;
/** @type {__VLS_StyleScopedClasses['quiet-window-state']} */ ;
/** @type {__VLS_StyleScopedClasses['quiet-window-state']} */ ;
/** @type {__VLS_StyleScopedClasses['quiet-window-state']} */ ;
/** @type {__VLS_StyleScopedClasses['quiet-window-state']} */ ;
/** @type {__VLS_StyleScopedClasses['quiet-window-state']} */ ;
/** @type {__VLS_StyleScopedClasses['global-ratio']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-list']} */ ;
/** @type {__VLS_StyleScopedClasses['special-rules-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-list']} */ ;
/** @type {__VLS_StyleScopedClasses['log-preview']} */ ;
/** @type {__VLS_StyleScopedClasses['log-preview']} */ ;
/** @type {__VLS_StyleScopedClasses['log-preview']} */ ;
/** @type {__VLS_StyleScopedClasses['log-preview']} */ ;
/** @type {__VLS_StyleScopedClasses['error']} */ ;
// CSS variable injection 
// CSS variable injection end 
__VLS_asFunctionalElement(__VLS_intrinsicElements.main, __VLS_intrinsicElements.main)({
    ...{ class: "page overview-page" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({
    ...{ class: "overview-heading" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
    ...{ class: "eyebrow" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h1, __VLS_intrinsicElements.h1)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "runtime-card" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "runtime-card-top" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: (['runtime-state', __VLS_ctx.enabled ? 'on' : 'off']) },
});
(__VLS_ctx.runtimeLabel);
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.confirmAction = __VLS_ctx.enabled ? 'disable' : 'enable';
        } },
    ...{ class: "switch" },
    ...{ class: ({ on: __VLS_ctx.enabled }) },
    type: "button",
    disabled: (__VLS_ctx.actionBusy),
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
(__VLS_ctx.enabled ? "ON" : "OFF");
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_0 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: (__VLS_ctx.enabled && !__VLS_ctx.gateReady ? 'refresh' : 'shield'),
    size: (14),
}));
const __VLS_1 = __VLS_0({
    name: (__VLS_ctx.enabled && !__VLS_ctx.gateReady ? 'refresh' : 'shield'),
    size: (14),
}, ...__VLS_functionalComponentArgsRest(__VLS_0));
(__VLS_ctx.runtimeMessage);
if (__VLS_ctx.enabled && !__VLS_ctx.gateReady) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "cookie-progress" },
        'aria-label': "Steam Cookie 准备进度",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({
        ...{ style: ({ width: `${__VLS_ctx.cookieProgress}%` }) },
    });
}
if (__VLS_ctx.failedCookieAccounts.length) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "retry-status" },
    });
    (__VLS_ctx.failedCookieAccounts.length);
    (__VLS_ctx.cookieGate.nextRetryAt ? `${__VLS_ctx.formatCountdown(__VLS_ctx.cookieGate.nextRetryAt)}自动重试` : "等待调度");
    (Math.max(...__VLS_ctx.failedCookieAccounts.map(row => Number(row.failureCount || 0))) + 1);
}
if (__VLS_ctx.failedCookieAccounts.length) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "runtime-actions" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.failedCookieAccounts.length))
                    return;
                __VLS_ctx.confirmAction = 'retry-auth';
            } },
        ...{ class: "secondary-button" },
        type: "button",
        disabled: (__VLS_ctx.actionBusy),
    });
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
if (__VLS_ctx.runtime.migrationHold) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "migration-banner" },
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_3 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: "shield",
        size: (17),
    }));
    const __VLS_4 = __VLS_3({
        name: "shield",
        size: (17),
    }, ...__VLS_functionalComponentArgsRest(__VLS_3));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
}
if (__VLS_ctx.error) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "feedback error" },
    });
    (__VLS_ctx.error);
}
else if (__VLS_ctx.notice) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "feedback success" },
    });
    (__VLS_ctx.notice);
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "metric-row" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.dashboard?.summary?.activeListings ?? "—");
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.dashboard?.summary?.pendingRebuys ?? "—");
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.dashboard?.summary?.deliveryPending ?? "—");
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.dashboard?.summary?.issueCount ?? "—");
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.dashboard?.summary?.steamHeatPct == null ? "—" : `${__VLS_ctx.dashboard.summary.steamHeatPct.toFixed(0)}%`);
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "panel cookie-panel" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "section-title" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "cookie-summary" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.cookieGate.validCount || 0);
(__VLS_ctx.cookieGate.totalCount || 0);
if (__VLS_ctx.failedCookieAccounts.length) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.failedCookieAccounts.length))
                    return;
                __VLS_ctx.confirmAction = 'retry-auth';
            } },
        ...{ class: "secondary-button" },
        type: "button",
        disabled: (__VLS_ctx.actionBusy),
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_6 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: "refresh",
        size: (14),
    }));
    const __VLS_7 = __VLS_6({
        name: "refresh",
        size: (14),
    }, ...__VLS_functionalComponentArgsRest(__VLS_6));
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.confirmAction = 'refresh-auth';
        } },
    ...{ class: "secondary-button" },
    type: "button",
    disabled: (__VLS_ctx.actionBusy),
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_9 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "refresh",
    size: (14),
}));
const __VLS_10 = __VLS_9({
    name: "refresh",
    size: (14),
}, ...__VLS_functionalComponentArgsRest(__VLS_9));
if (__VLS_ctx.cookieAccounts.length) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "table-wrap" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.table, __VLS_intrinsicElements.table)({
        ...{ class: "data-table cookie-table" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.thead, __VLS_intrinsicElements.thead)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.tr, __VLS_intrinsicElements.tr)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.th, __VLS_intrinsicElements.th)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.th, __VLS_intrinsicElements.th)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.th, __VLS_intrinsicElements.th)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.th, __VLS_intrinsicElements.th)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.th, __VLS_intrinsicElements.th)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.th, __VLS_intrinsicElements.th)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.tbody, __VLS_intrinsicElements.tbody)({});
    for (const [account] of __VLS_getVForSourceType((__VLS_ctx.cookieAccounts))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.tr, __VLS_intrinsicElements.tr)({
            key: (account.accountId || account.steamId),
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        (account.accountName || account.name || "未命名");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({
            ...{ class: "mono" },
        });
        (account.steamId || "—");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: (['pill', account.valid ? 'success' : account.status === 'unknown' ? 'neutral' : 'warning']) },
        });
        (account.valid ? "有效" : account.status || "未知");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        (__VLS_ctx.formatLocal(account.lastCheckedAt));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({
            ...{ class: ({ 'danger-text': account.error }) },
        });
        (account.error || account.lastResult || "—");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        (account.nextRetryAt ? __VLS_ctx.formatCountdown(account.nextRetryAt) : "运行中监测");
    }
}
else {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "empty-state" },
    });
    (__VLS_ctx.loading ? "正在读取 Cookie 健康状态…" : "后端暂未返回账号 Cookie 状态。");
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "cookie-legend" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "two-column" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "panel" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "section-title compact" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
if (__VLS_ctx.dashboard?.taskQueue?.length) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "task-list" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "task-head" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    for (const [task] of __VLS_getVForSourceType((__VLS_ctx.dashboard.taskQueue.slice(0, 8)))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            key: (task.id),
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.time, __VLS_intrinsicElements.time)({});
        (__VLS_ctx.formatLocal(task.nextAttemptAt));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (task.label || task.taskType || "未命名任务");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        (task.marketHashName || "—");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        (task.accountName || "—");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
        (task.reason || task.lastError || __VLS_ctx.formatCountdown(task.nextAttemptAt) || task.status);
    }
}
else {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "empty-state" },
    });
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "panel scheduler-panel" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "section-title compact" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: (['pill', __VLS_ctx.dashboard?.steamScheduler?.status === 'healthy' ? 'success' : 'neutral']) },
});
(__VLS_ctx.dashboard?.steamScheduler?.status || "状态未知");
__VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({
    ...{ class: "scheduler-summary" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
(__VLS_ctx.dashboard?.steamScheduler?.queueLength ?? "—");
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
(__VLS_ctx.dashboard?.steamScheduler?.activeRequest || "无");
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
(__VLS_ctx.dashboard?.steamScheduler?.requestsPerMinute ?? "—");
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
(__VLS_ctx.formatLocal(__VLS_ctx.routeCooldownUntil));
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "quiet-window-state" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({
    ...{ class: (__VLS_ctx.quietWindow ? 'active' : '') },
});
(__VLS_ctx.quietWindow ? `进行中 · ${__VLS_ctx.formatCountdown(__VLS_ctx.quietWindow.cooldownUntil)}` : "当前未启用");
if (__VLS_ctx.dashboard?.steamScheduler?.priorities?.length) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "priority-list" },
    });
    for (const [row] of __VLS_getVForSourceType((__VLS_ctx.dashboard.steamScheduler.priorities))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            key: (row.priority),
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
        (row.priority);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        (row.label);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (row.queued || 0);
    }
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "circuit-section" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "circuit-heading" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
(__VLS_ctx.activeCircuits.length ? `${__VLS_ctx.activeCircuits.length} 条冷却中` : "当前无 429 熔断");
if (__VLS_ctx.activeCircuits.length) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "circuit-list" },
    });
    for (const [row] of __VLS_getVForSourceType((__VLS_ctx.activeCircuits))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
            key: (row.circuitKey || `${row.accountId}-${row.route}`),
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (row.accountId || "全账号");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: (['pill', row.state === 'half_open' ? 'warning' : 'neutral']) },
        });
        (row.state === "half_open" ? "恢复探测" : "冷却中");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
            ...{ class: "mono" },
        });
        (row.route || "全局 Steam 请求");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (row.consecutive429 || 0);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.formatLocal(row.last429At));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.formatCountdown(row.cooldownUntil || row.nextProbeAt));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.formatLocal(row.nextProbeAt));
    }
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "three-column" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "panel mini-panel special-rules-panel" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "section-title compact" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
const __VLS_12 = {}.RouterLink;
/** @type {[typeof __VLS_components.RouterLink, typeof __VLS_components.RouterLink, ]} */ ;
// @ts-ignore
const __VLS_13 = __VLS_asFunctionalComponent(__VLS_12, new __VLS_12({
    to: "/guadao/settings",
}));
const __VLS_14 = __VLS_13({
    to: "/guadao/settings",
}, ...__VLS_functionalComponentArgsRest(__VLS_13));
__VLS_15.slots.default;
var __VLS_15;
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
    ...{ class: "global-ratio" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.dashboard?.settingsSummary?.guadaoMaxListingRatio == null ? "—" : `${(__VLS_ctx.dashboard.settingsSummary.guadaoMaxListingRatio * 100).toFixed(2)}%`);
if (__VLS_ctx.dashboard?.specialRules?.length) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "mini-list" },
    });
    for (const [rule] of __VLS_getVForSourceType((__VLS_ctx.dashboard.specialRules.slice(0, 4)))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            key: (rule.id),
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        (rule.displayName || rule.marketHashName);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
        (rule.currentRatioPct == null ? "最近观测 —" : `最近观测 ${rule.currentRatioPct.toFixed(2)}% · ${__VLS_ctx.formatLocal(rule.currentRatioObservedAt)}`);
        (rule.enabled === false ? "已停用" : "专用规则");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (rule.maxRatioPct?.toFixed(2));
    }
}
else {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "empty-state small" },
    });
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "panel mini-panel" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "section-title compact" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
const __VLS_16 = {}.RouterLink;
/** @type {[typeof __VLS_components.RouterLink, typeof __VLS_components.RouterLink, ]} */ ;
// @ts-ignore
const __VLS_17 = __VLS_asFunctionalComponent(__VLS_16, new __VLS_16({
    to: "/guadao/issues",
}));
const __VLS_18 = __VLS_17({
    to: "/guadao/issues",
}, ...__VLS_functionalComponentArgsRest(__VLS_17));
__VLS_19.slots.default;
var __VLS_19;
if (__VLS_ctx.dashboard?.issues?.length) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "mini-list" },
    });
    for (const [issue] of __VLS_getVForSourceType((__VLS_ctx.dashboard.issues.slice(0, 3)))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            key: (issue.id || issue.issueId),
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        (issue.title || issue.issueType || issue.reason || issue.status || "待处理问题");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (issue.severity || issue.status || "待处理");
    }
}
else {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "empty-state small" },
    });
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "panel mini-panel" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "section-title compact" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
const __VLS_20 = {}.RouterLink;
/** @type {[typeof __VLS_components.RouterLink, typeof __VLS_components.RouterLink, ]} */ ;
// @ts-ignore
const __VLS_21 = __VLS_asFunctionalComponent(__VLS_20, new __VLS_20({
    to: "/guadao/logs",
}));
const __VLS_22 = __VLS_21({
    to: "/guadao/logs",
}, ...__VLS_functionalComponentArgsRest(__VLS_21));
__VLS_23.slots.default;
var __VLS_23;
if (__VLS_ctx.dashboard?.recentLogs?.length) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "log-preview" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "log-head" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    for (const [log] of __VLS_getVForSourceType((__VLS_ctx.dashboard.recentLogs.slice(0, 4)))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            key: (log.id),
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.time, __VLS_intrinsicElements.time)({});
        (__VLS_ctx.formatLocal(log.timestamp));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        (log.operation || log.service);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (log.accountName || log.marketHashName || log.message);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({
            ...{ class: ({ error: (log.httpStatus || 0) >= 400 }) },
        });
        (log.httpStatus || "—");
    }
}
else {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "empty-state small" },
    });
}
if (__VLS_ctx.confirmAction) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.confirmAction))
                    return;
                __VLS_ctx.confirmAction = null;
            } },
        ...{ class: "modal-backdrop" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "confirm-dialog" },
        role: "dialog",
        'aria-modal': "true",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "dialog-icon" },
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_24 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: (__VLS_ctx.confirmAction === 'disable' ? 'warning' : 'shield'),
        size: (22),
    }));
    const __VLS_25 = __VLS_24({
        name: (__VLS_ctx.confirmAction === 'disable' ? 'warning' : 'shield'),
        size: (22),
    }, ...__VLS_functionalComponentArgsRest(__VLS_24));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
    (__VLS_ctx.confirmCopy.title);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    (__VLS_ctx.confirmCopy.text);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.confirmAction))
                    return;
                __VLS_ctx.confirmAction = null;
            } },
        ...{ class: "secondary-button" },
        type: "button",
        disabled: (__VLS_ctx.actionBusy),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.submitConfirmed) },
        ...{ class: (__VLS_ctx.confirmAction === 'disable' ? 'danger-button' : 'primary-button') },
        type: "button",
        disabled: (__VLS_ctx.actionBusy),
    });
    (__VLS_ctx.actionBusy ? "提交中…" : __VLS_ctx.confirmCopy.button);
}
/** @type {__VLS_StyleScopedClasses['page']} */ ;
/** @type {__VLS_StyleScopedClasses['overview-page']} */ ;
/** @type {__VLS_StyleScopedClasses['overview-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['eyebrow']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-card']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-card-top']} */ ;
/** @type {__VLS_StyleScopedClasses['switch']} */ ;
/** @type {__VLS_StyleScopedClasses['cookie-progress']} */ ;
/** @type {__VLS_StyleScopedClasses['retry-status']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['migration-banner']} */ ;
/** @type {__VLS_StyleScopedClasses['feedback']} */ ;
/** @type {__VLS_StyleScopedClasses['error']} */ ;
/** @type {__VLS_StyleScopedClasses['feedback']} */ ;
/** @type {__VLS_StyleScopedClasses['success']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-row']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['cookie-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['section-title']} */ ;
/** @type {__VLS_StyleScopedClasses['cookie-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['table-wrap']} */ ;
/** @type {__VLS_StyleScopedClasses['data-table']} */ ;
/** @type {__VLS_StyleScopedClasses['cookie-table']} */ ;
/** @type {__VLS_StyleScopedClasses['mono']} */ ;
/** @type {__VLS_StyleScopedClasses['empty-state']} */ ;
/** @type {__VLS_StyleScopedClasses['cookie-legend']} */ ;
/** @type {__VLS_StyleScopedClasses['two-column']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['section-title']} */ ;
/** @type {__VLS_StyleScopedClasses['compact']} */ ;
/** @type {__VLS_StyleScopedClasses['task-list']} */ ;
/** @type {__VLS_StyleScopedClasses['task-head']} */ ;
/** @type {__VLS_StyleScopedClasses['empty-state']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['scheduler-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['section-title']} */ ;
/** @type {__VLS_StyleScopedClasses['compact']} */ ;
/** @type {__VLS_StyleScopedClasses['scheduler-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['quiet-window-state']} */ ;
/** @type {__VLS_StyleScopedClasses['priority-list']} */ ;
/** @type {__VLS_StyleScopedClasses['circuit-section']} */ ;
/** @type {__VLS_StyleScopedClasses['circuit-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['circuit-list']} */ ;
/** @type {__VLS_StyleScopedClasses['mono']} */ ;
/** @type {__VLS_StyleScopedClasses['three-column']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['special-rules-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['section-title']} */ ;
/** @type {__VLS_StyleScopedClasses['compact']} */ ;
/** @type {__VLS_StyleScopedClasses['global-ratio']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-list']} */ ;
/** @type {__VLS_StyleScopedClasses['empty-state']} */ ;
/** @type {__VLS_StyleScopedClasses['small']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['section-title']} */ ;
/** @type {__VLS_StyleScopedClasses['compact']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-list']} */ ;
/** @type {__VLS_StyleScopedClasses['empty-state']} */ ;
/** @type {__VLS_StyleScopedClasses['small']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['section-title']} */ ;
/** @type {__VLS_StyleScopedClasses['compact']} */ ;
/** @type {__VLS_StyleScopedClasses['log-preview']} */ ;
/** @type {__VLS_StyleScopedClasses['log-head']} */ ;
/** @type {__VLS_StyleScopedClasses['empty-state']} */ ;
/** @type {__VLS_StyleScopedClasses['small']} */ ;
/** @type {__VLS_StyleScopedClasses['modal-backdrop']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-dialog']} */ ;
/** @type {__VLS_StyleScopedClasses['dialog-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            RouterLink: RouterLink,
            FolioIcon: FolioIcon,
            formatCountdown: formatCountdown,
            formatLocal: formatLocal,
            dashboard: dashboard,
            loading: loading,
            actionBusy: actionBusy,
            error: error,
            notice: notice,
            confirmAction: confirmAction,
            runtime: runtime,
            cookieGate: cookieGate,
            cookieAccounts: cookieAccounts,
            enabled: enabled,
            gateReady: gateReady,
            failedCookieAccounts: failedCookieAccounts,
            cookieProgress: cookieProgress,
            runtimeLabel: runtimeLabel,
            runtimeMessage: runtimeMessage,
            quietWindow: quietWindow,
            activeCircuits: activeCircuits,
            routeCooldownUntil: routeCooldownUntil,
            confirmCopy: confirmCopy,
            submitConfirmed: submitConfirmed,
        };
    },
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
});
; /* PartiallyEnd: #4569/main.vue */
