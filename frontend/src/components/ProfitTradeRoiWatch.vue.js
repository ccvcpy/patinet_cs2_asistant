import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import FolioIcon from "./FolioIcon.vue";
const rows = ref([]);
const total = ref(0);
const page = ref(1);
const pageSize = 12;
const keywordDraft = ref("");
const keyword = ref("");
const status = ref("active");
const sort = ref("roi_desc");
const loading = ref(false);
const error = ref("");
const selected = ref(null);
const history = ref([]);
const historyPage = ref(1);
const historyTotal = ref(0);
const historyLoading = ref(false);
const historyError = ref("");
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)));
const historyPages = computed(() => Math.max(1, Math.ceil(historyTotal.value / 20)));
function money(value) {
    return typeof value === "number" && Number.isFinite(value) ? `¥${value.toFixed(2)}` : "-";
}
function pct(value) {
    if (typeof value !== "number" || !Number.isFinite(value))
        return "-";
    return `${(Math.abs(value) <= 1 ? value * 100 : value).toFixed(2)}%`;
}
function time(value) {
    if (!value)
        return "-";
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("zh-CN", { hour12: false });
}
async function responseError(response) {
    try {
        const payload = await response.json();
        return payload.error || payload.detail || response.statusText;
    }
    catch {
        return response.statusText;
    }
}
function stateLabel(row) {
    const labels = {
        executable: "达到执行门槛", eligible: "达到执行门槛", observe_only: "仅观察，不执行",
        blocked: "风控阻断", manual_review: "异常 ROI，需人工", exited: "已退出观察池",
    };
    return labels[row.executionStatus || "observe_only"] || row.executionStatus || "仅观察，不执行";
}
function stateClass(row) {
    if (row.active === false)
        return "exited";
    if (["executable", "eligible"].includes(row.executionStatus || ""))
        return "ready";
    if (["blocked", "manual_review"].includes(row.executionStatus || ""))
        return "blocked";
    return "observe";
}
async function load() {
    loading.value = true;
    error.value = "";
    const activeFilter = status.value === "all" ? "all" : status.value === "exited" ? "false" : "true";
    const query = new URLSearchParams({ page: String(page.value), pageSize: String(pageSize), active: activeFilter, sort: sort.value });
    if (keyword.value)
        query.set("keyword", keyword.value);
    try {
        const response = await fetch(`/api/profit-trade/roi-watch?${query}`, { cache: "no-store" });
        if (!response.ok)
            throw new Error(await responseError(response));
        const payload = await response.json();
        rows.value = Array.isArray(payload.items) ? payload.items : [];
        total.value = Number(payload.total) || 0;
    }
    catch (reason) {
        rows.value = [];
        total.value = 0;
        error.value = `观察池读取失败：${reason instanceof Error ? reason.message : String(reason)}`;
    }
    finally {
        loading.value = false;
    }
}
function search() { keyword.value = keywordDraft.value.trim(); page.value = 1; void load(); }
function turn(direction) {
    const next = page.value + direction;
    if (next >= 1 && next <= totalPages.value) {
        page.value = next;
        void load();
    }
}
async function openHistory(row) { selected.value = row; historyPage.value = 1; await loadHistory(); }
async function loadHistory() {
    if (!selected.value)
        return;
    historyLoading.value = true;
    historyError.value = "";
    const query = new URLSearchParams({ marketHashName: selected.value.marketHashName, page: String(historyPage.value), pageSize: "20" });
    try {
        const response = await fetch(`/api/profit-trade/roi-watch/history?${query}`, { cache: "no-store" });
        if (!response.ok)
            throw new Error(await responseError(response));
        const payload = await response.json();
        history.value = Array.isArray(payload.items) ? payload.items : [];
        historyTotal.value = Number(payload.total) || 0;
    }
    catch (reason) {
        history.value = [];
        historyTotal.value = 0;
        historyError.value = `历史读取失败：${reason instanceof Error ? reason.message : String(reason)}`;
    }
    finally {
        historyLoading.value = false;
    }
}
function turnHistory(direction) {
    const next = historyPage.value + direction;
    if (next >= 1 && next <= historyPages.value) {
        historyPage.value = next;
        void loadHistory();
    }
}
watch([status, sort], () => { page.value = 1; void load(); });
onMounted(() => { void load(); window.addEventListener("profit-trade:refresh-observability", load); });
onUnmounted(() => window.removeEventListener("profit-trade:refresh-observability", load));
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
/** @type {__VLS_StyleScopedClasses['watch-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-count']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-count']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-toolbar']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-toolbar']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-toolbar']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-toolbar']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-toolbar']} */ ;
/** @type {__VLS_StyleScopedClasses['card-head']} */ ;
/** @type {__VLS_StyleScopedClasses['card-head']} */ ;
/** @type {__VLS_StyleScopedClasses['card-head']} */ ;
/** @type {__VLS_StyleScopedClasses['card-head']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-state']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-state']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-state']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-state']} */ ;
/** @type {__VLS_StyleScopedClasses['price-line']} */ ;
/** @type {__VLS_StyleScopedClasses['price-line']} */ ;
/** @type {__VLS_StyleScopedClasses['price-line']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['price-line']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['history-drawer']} */ ;
/** @type {__VLS_StyleScopedClasses['history-drawer']} */ ;
/** @type {__VLS_StyleScopedClasses['history-drawer']} */ ;
/** @type {__VLS_StyleScopedClasses['history-drawer']} */ ;
/** @type {__VLS_StyleScopedClasses['history-list']} */ ;
/** @type {__VLS_StyleScopedClasses['history-list']} */ ;
/** @type {__VLS_StyleScopedClasses['history-list']} */ ;
/** @type {__VLS_StyleScopedClasses['history-list']} */ ;
/** @type {__VLS_StyleScopedClasses['history-list']} */ ;
/** @type {__VLS_StyleScopedClasses['history-list']} */ ;
/** @type {__VLS_StyleScopedClasses['history-list']} */ ;
/** @type {__VLS_StyleScopedClasses['history-list']} */ ;
/** @type {__VLS_StyleScopedClasses['history-list']} */ ;
// CSS variable injection
// CSS variable injection end
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "roi-watch panel" },
    'aria-labelledby': "roi-watch-title",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "watch-heading" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
    ...{ class: "eyebrow" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({
    id: "roi-watch-title",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "watch-count" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.total);
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.form, __VLS_intrinsicElements.form)({
    ...{ onSubmit: (__VLS_ctx.search) },
    ...{ class: "watch-toolbar" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input, __VLS_intrinsicElements.input)({
    type: "search",
    placeholder: "中文名或 marketHashName",
});
(__VLS_ctx.keywordDraft);
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
    value: (__VLS_ctx.status),
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "active",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "all",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "exited",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
    value: (__VLS_ctx.sort),
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "roi_desc",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "updated_desc",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "price_desc",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ class: "secondary-button" },
    type: "submit",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.load) },
    ...{ class: "secondary-button refresh" },
    type: "button",
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_0 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "refresh",
    size: (15),
}));
const __VLS_1 = __VLS_0({
    name: "refresh",
    size: (15),
}, ...__VLS_functionalComponentArgsRest(__VLS_0));
if (__VLS_ctx.error) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "watch-error" },
    });
    (__VLS_ctx.error);
}
if (__VLS_ctx.loading) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "watch-empty" },
    });
}
else if (!__VLS_ctx.error && __VLS_ctx.rows.length === 0) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "watch-empty" },
    });
}
else {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "watch-grid" },
    });
    for (const [row] of __VLS_getVForSourceType((__VLS_ctx.rows))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
            key: (row.marketHashName),
            ...{ class: "watch-card" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "card-head" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (row.name || row.marketHashName);
        if (row.name && row.name !== row.marketHashName) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
            (row.marketHashName);
        }
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: (['watch-state', __VLS_ctx.stateClass(row)]) },
        });
        (__VLS_ctx.stateLabel(row));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "price-line" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.money(row.steamBuyPrice));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.money(row.c5ListingPrice));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.money(row.c5ExpectedNetPrice));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "watch-metrics" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({
            ...{ class: "positive" },
        });
        (__VLS_ctx.pct(row.expectedRoi));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.pct(row.minRoi));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.pct(row.balanceDiscount));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.money(row.expectedProfit));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (row.tradableCount ?? "-");
        (row.inventoryCount ?? "-");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.time(row.lastObservedAt));
        if (row.executionReason || row.riskReason || row.exitReason) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
                ...{ class: "reason" },
            });
            (row.executionReason || row.riskReason || row.exitReason);
        }
        __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
            ...{ onClick: (...[$event]) => {
                    if (!!(__VLS_ctx.loading))
                        return;
                    if (!!(!__VLS_ctx.error && __VLS_ctx.rows.length === 0))
                        return;
                    __VLS_ctx.openHistory(row);
                } },
            ...{ class: "history-link" },
            type: "button",
        });
    }
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.footer, __VLS_intrinsicElements.footer)({
    ...{ class: "pagination" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
(__VLS_ctx.page);
(__VLS_ctx.totalPages);
(__VLS_ctx.total);
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.turn(-1);
        } },
    ...{ class: "mini-action" },
    type: "button",
    disabled: (__VLS_ctx.page <= 1),
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.turn(1);
        } },
    ...{ class: "mini-action" },
    type: "button",
    disabled: (__VLS_ctx.page >= __VLS_ctx.totalPages),
});
if (__VLS_ctx.selected) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.selected))
                    return;
                __VLS_ctx.selected = null;
            } },
        ...{ class: "history-backdrop" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.aside, __VLS_intrinsicElements.aside)({
        ...{ class: "history-drawer" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "eyebrow" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h3, __VLS_intrinsicElements.h3)({});
    (__VLS_ctx.selected.name || __VLS_ctx.selected.marketHashName);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (__VLS_ctx.selected.marketHashName);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.selected))
                    return;
                __VLS_ctx.selected = null;
            } },
        type: "button",
        'aria-label': "关闭",
    });
    if (__VLS_ctx.historyError) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
            ...{ class: "watch-error" },
        });
        (__VLS_ctx.historyError);
    }
    if (__VLS_ctx.historyLoading) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "watch-empty" },
        });
    }
    else {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "history-list" },
        });
        for (const [event, index] of __VLS_getVForSourceType((__VLS_ctx.history))) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
                key: (`${event.scanId || event.observedAt}-${index}`),
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
            (__VLS_ctx.pct(event.expectedRoi));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
            (event.eventType || (event.active === false ? "退出" : "观察"));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
            (__VLS_ctx.money(event.steamBuyPrice));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
            (__VLS_ctx.money(event.c5ListingPrice));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
            (__VLS_ctx.money(event.c5ExpectedNetPrice));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
            (__VLS_ctx.time(event.observedAt || event.lastObservedAt));
            if (event.executionReason || event.riskReason || event.exitReason) {
                __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
                (event.executionReason || event.riskReason || event.exitReason);
            }
        }
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.footer, __VLS_intrinsicElements.footer)({
        ...{ class: "pagination" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.historyPage);
    (__VLS_ctx.historyPages);
    (__VLS_ctx.historyTotal);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.selected))
                    return;
                __VLS_ctx.turnHistory(-1);
            } },
        ...{ class: "mini-action" },
        type: "button",
        disabled: (__VLS_ctx.historyPage <= 1),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.selected))
                    return;
                __VLS_ctx.turnHistory(1);
            } },
        ...{ class: "mini-action" },
        type: "button",
        disabled: (__VLS_ctx.historyPage >= __VLS_ctx.historyPages),
    });
}
/** @type {__VLS_StyleScopedClasses['roi-watch']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['eyebrow']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-count']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-toolbar']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-error']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-empty']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-empty']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-card']} */ ;
/** @type {__VLS_StyleScopedClasses['card-head']} */ ;
/** @type {__VLS_StyleScopedClasses['price-line']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['positive']} */ ;
/** @type {__VLS_StyleScopedClasses['reason']} */ ;
/** @type {__VLS_StyleScopedClasses['history-link']} */ ;
/** @type {__VLS_StyleScopedClasses['pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-action']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-action']} */ ;
/** @type {__VLS_StyleScopedClasses['history-backdrop']} */ ;
/** @type {__VLS_StyleScopedClasses['history-drawer']} */ ;
/** @type {__VLS_StyleScopedClasses['eyebrow']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-error']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-empty']} */ ;
/** @type {__VLS_StyleScopedClasses['history-list']} */ ;
/** @type {__VLS_StyleScopedClasses['pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-action']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-action']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            FolioIcon: FolioIcon,
            rows: rows,
            total: total,
            page: page,
            keywordDraft: keywordDraft,
            status: status,
            sort: sort,
            loading: loading,
            error: error,
            selected: selected,
            history: history,
            historyPage: historyPage,
            historyTotal: historyTotal,
            historyLoading: historyLoading,
            historyError: historyError,
            totalPages: totalPages,
            historyPages: historyPages,
            money: money,
            pct: pct,
            time: time,
            stateLabel: stateLabel,
            stateClass: stateClass,
            load: load,
            search: search,
            turn: turn,
            openHistory: openHistory,
            turnHistory: turnHistory,
        };
    },
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
});
; /* PartiallyEnd: #4569/main.vue */
