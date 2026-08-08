import { computed, onMounted, ref } from "vue";
import FolioIcon from "../components/FolioIcon.vue";
import OperationVisualAtom from "../components/OperationVisualAtom.vue";
const rows = ref([]);
const loading = ref(false);
const loadError = ref("");
const hasRead = ref(false);
const loadedAt = ref("");
const dataSource = ref("");
const accountCount = computed(() => rows.value.length);
const successfulCount = computed(() => rows.value.filter((row) => row.status === "ok").length);
const currencySummaries = computed(() => {
    const groups = new Map();
    for (const row of rows.value) {
        if (!row.currency)
            continue;
        const group = groups.get(row.currency) || {
            currency: row.currency,
            currencyId: row.currencyId,
            accountCount: 0,
            realBalance: 0,
            pendingBalance: 0,
            totalBalance: 0,
        };
        group.accountCount += 1;
        group.realBalance += Number(row.realBalance || 0);
        group.pendingBalance += Number(row.pendingBalance || 0);
        group.totalBalance = group.realBalance + group.pendingBalance;
        groups.set(row.currency, group);
    }
    return [...groups.values()].sort((left, right) => left.currency.localeCompare(right.currency));
});
function formatMoney(value) {
    return value === null ? "--" : value.toFixed(2);
}
function rowCurrency(row) {
    return row.currency || "--";
}
function applyPayload(payload) {
    rows.value = payload.accounts || [];
    hasRead.value = Boolean(payload.hasSnapshot);
    loadedAt.value = payload.updatedAt
        ? new Date(payload.updatedAt).toLocaleString("zh-CN", { hour12: false })
        : "";
    dataSource.value = payload.source || "";
}
async function loadSavedBalances() {
    try {
        const response = await fetch("/api/steam-balances", { cache: "no-store" });
        const payload = await response.json();
        if (!response.ok || !payload.ok)
            return;
        applyPayload(payload);
    }
    catch {
        // The page remains usable when the API is offline; refresh will show the error.
    }
}
async function readBalances() {
    loading.value = true;
    loadError.value = "";
    try {
        const response = await fetch("/api/steam-balances/refresh", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
        });
        const payload = await response.json();
        if (!response.ok || !payload.ok) {
            throw new Error(payload.error || `读取失败（HTTP ${response.status}）`);
        }
        applyPayload(payload);
    }
    catch (error) {
        loadError.value = error instanceof Error ? error.message : String(error);
    }
    finally {
        loading.value = false;
    }
}
onMounted(loadSavedBalances);
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
/** @type {__VLS_StyleScopedClasses['balance-empty']} */ ;
/** @type {__VLS_StyleScopedClasses['read-status']} */ ;
/** @type {__VLS_StyleScopedClasses['read-status']} */ ;
/** @type {__VLS_StyleScopedClasses['read-status']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['page-header']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['page-header']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['read-button']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['read-button']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['balance-message']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['success-message']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['error-message']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-card']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-card']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-card']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['currency-totals']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['currency-totals']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['balance-empty']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['balance-empty']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['balance-empty']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['data-table']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['data-table']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['data-table']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['account-name']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['read-status']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['read-status']} */ ;
/** @type {__VLS_StyleScopedClasses['ok']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['read-status']} */ ;
/** @type {__VLS_StyleScopedClasses['error']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['read-status']} */ ;
/** @type {__VLS_StyleScopedClasses['skipped']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['page-header']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['read-button']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-card']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['page-header']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['page-header']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['page-header']} */ ;
/** @type {__VLS_StyleScopedClasses['eyebrow']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['page-title-cluster']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['page-title-cluster']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['read-button']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['read-button']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['balance-message']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['success-message']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['error-message']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-card']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-card']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-card']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-card']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-card']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['balance-empty']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['balance-empty']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['balance-empty']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['data-table']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['data-table']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['data-table']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['data-table']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['data-table']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['account-name']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['read-status']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['read-status']} */ ;
/** @type {__VLS_StyleScopedClasses['ok']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['read-status']} */ ;
/** @type {__VLS_StyleScopedClasses['error']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['read-status']} */ ;
/** @type {__VLS_StyleScopedClasses['skipped']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['page-header']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['page-header']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['page-title-cluster']} */ ;
/** @type {__VLS_StyleScopedClasses['operation-visual-atom']} */ ;
// CSS variable injection 
// CSS variable injection end 
__VLS_asFunctionalElement(__VLS_intrinsicElements.main, __VLS_intrinsicElements.main)({
    ...{ class: "page steam-balance-page steam-balance-page--folio-refresh steam-balance-page--minimal-v2" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({
    ...{ class: "page-header" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "page-title-cluster" },
});
/** @type {[typeof OperationVisualAtom, ]} */ ;
// @ts-ignore
const __VLS_0 = __VLS_asFunctionalComponent(OperationVisualAtom, new OperationVisualAtom({
    name: "steam-balance",
    size: (68),
}));
const __VLS_1 = __VLS_0({
    name: "steam-balance",
    size: (68),
}, ...__VLS_functionalComponentArgsRest(__VLS_0));
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
    ...{ class: "eyebrow" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h1, __VLS_intrinsicElements.h1)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.readBalances) },
    ...{ class: "primary-button read-button" },
    type: "button",
    disabled: (__VLS_ctx.loading),
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_3 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "refresh",
    ...{ class: ({ spinning: __VLS_ctx.loading }) },
}));
const __VLS_4 = __VLS_3({
    name: "refresh",
    ...{ class: ({ spinning: __VLS_ctx.loading }) },
}, ...__VLS_functionalComponentArgsRest(__VLS_3));
(__VLS_ctx.loading ? "正在读取" : "读取余额");
if (__VLS_ctx.loadError) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "balance-message error-message" },
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_6 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: "error",
    }));
    const __VLS_7 = __VLS_6({
        name: "error",
    }, ...__VLS_functionalComponentArgsRest(__VLS_6));
    (__VLS_ctx.loadError);
}
else if (__VLS_ctx.loadedAt) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "balance-message success-message" },
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_9 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: "success",
    }));
    const __VLS_10 = __VLS_9({
        name: "success",
    }, ...__VLS_functionalComponentArgsRest(__VLS_9));
    (__VLS_ctx.dataSource === "cache" ? "上次读取" : "本次读取");
    (__VLS_ctx.successfulCount);
    (__VLS_ctx.accountCount);
    (__VLS_ctx.loadedAt);
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "metrics-grid" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "metric-card" },
});
/** @type {[typeof OperationVisualAtom, ]} */ ;
// @ts-ignore
const __VLS_12 = __VLS_asFunctionalComponent(OperationVisualAtom, new OperationVisualAtom({
    name: "accounts",
    size: (46),
}));
const __VLS_13 = __VLS_12({
    name: "accounts",
    size: (46),
}, ...__VLS_functionalComponentArgsRest(__VLS_12));
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "metric-copy" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.hasRead ? __VLS_ctx.accountCount : "--");
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "metric-card" },
});
/** @type {[typeof OperationVisualAtom, ]} */ ;
// @ts-ignore
const __VLS_15 = __VLS_asFunctionalComponent(OperationVisualAtom, new OperationVisualAtom({
    name: "steam-balance",
    size: (46),
}));
const __VLS_16 = __VLS_15({
    name: "steam-balance",
    size: (46),
}, ...__VLS_functionalComponentArgsRest(__VLS_15));
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "metric-copy" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
if (!__VLS_ctx.hasRead) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
}
else {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "currency-totals" },
    });
    for (const [group] of __VLS_getVForSourceType((__VLS_ctx.currencySummaries))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({
            key: (group.currency),
        });
        (group.currency);
        (__VLS_ctx.formatMoney(group.realBalance));
    }
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "metric-card" },
});
/** @type {[typeof OperationVisualAtom, ]} */ ;
// @ts-ignore
const __VLS_18 = __VLS_asFunctionalComponent(OperationVisualAtom, new OperationVisualAtom({
    name: "pending-wallet",
    size: (46),
}));
const __VLS_19 = __VLS_18({
    name: "pending-wallet",
    size: (46),
}, ...__VLS_functionalComponentArgsRest(__VLS_18));
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "metric-copy" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
if (!__VLS_ctx.hasRead) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
}
else {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "currency-totals" },
    });
    for (const [group] of __VLS_getVForSourceType((__VLS_ctx.currencySummaries))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({
            key: (group.currency),
        });
        (group.currency);
        (__VLS_ctx.formatMoney(group.pendingBalance));
    }
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "metric-card" },
});
/** @type {[typeof OperationVisualAtom, ]} */ ;
// @ts-ignore
const __VLS_21 = __VLS_asFunctionalComponent(OperationVisualAtom, new OperationVisualAtom({
    name: "total-database",
    size: (46),
}));
const __VLS_22 = __VLS_21({
    name: "total-database",
    size: (46),
}, ...__VLS_functionalComponentArgsRest(__VLS_21));
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "metric-copy" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
if (!__VLS_ctx.hasRead) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
}
else {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "currency-totals" },
    });
    for (const [group] of __VLS_getVForSourceType((__VLS_ctx.currencySummaries))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({
            key: (group.currency),
        });
        (group.currency);
        (__VLS_ctx.formatMoney(group.totalBalance));
    }
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "panel" },
});
if (!__VLS_ctx.hasRead) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "balance-empty" },
    });
    /** @type {[typeof OperationVisualAtom, ]} */ ;
    // @ts-ignore
    const __VLS_24 = __VLS_asFunctionalComponent(OperationVisualAtom, new OperationVisualAtom({
        name: "steam-balance",
        size: (72),
    }));
    const __VLS_25 = __VLS_24({
        name: "steam-balance",
        size: (72),
    }, ...__VLS_functionalComponentArgsRest(__VLS_24));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
}
else {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "table-wrap" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.table, __VLS_intrinsicElements.table)({
        ...{ class: "data-table steam-balance-table" },
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
    for (const [row] of __VLS_getVForSourceType((__VLS_ctx.rows))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.tr, __VLS_intrinsicElements.tr)({
            key: (row.id),
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({
            ...{ class: "account-name" },
        });
        (row.account);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({
            ...{ class: "mono" },
        });
        (row.steamId || "--");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({
            ...{ class: "amount" },
        });
        (__VLS_ctx.rowCurrency(row));
        (__VLS_ctx.formatMoney(row.realBalance));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({
            ...{ class: "amount" },
        });
        (__VLS_ctx.rowCurrency(row));
        (__VLS_ctx.formatMoney(row.pendingBalance));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({
            ...{ class: "amount" },
        });
        (__VLS_ctx.rowCurrency(row));
        (__VLS_ctx.formatMoney(row.totalBalance));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: "read-status" },
            ...{ class: (row.status) },
        });
        /** @type {[typeof FolioIcon, ]} */ ;
        // @ts-ignore
        const __VLS_27 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
            name: (row.status === 'ok' ? 'success' : row.status === 'error' ? 'error' : 'warning'),
            size: (15),
        }));
        const __VLS_28 = __VLS_27({
            name: (row.status === 'ok' ? 'success' : row.status === 'error' ? 'error' : 'warning'),
            size: (15),
        }, ...__VLS_functionalComponentArgsRest(__VLS_27));
        (row.stale ? "上次数据" : row.status === "ok" ? "读取成功" : row.status === "error" ? "读取失败" : "已跳过");
        if (row.error) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
                ...{ class: "row-error" },
            });
            (row.error);
        }
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.tfoot, __VLS_intrinsicElements.tfoot)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.tr, __VLS_intrinsicElements.tr)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({
        colspan: "2",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
    for (const [group] of __VLS_getVForSourceType((__VLS_ctx.currencySummaries))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            key: (group.currency),
            ...{ class: "footer-total" },
        });
        (group.currency);
        (__VLS_ctx.formatMoney(group.realBalance));
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
    for (const [group] of __VLS_getVForSourceType((__VLS_ctx.currencySummaries))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            key: (group.currency),
            ...{ class: "footer-total" },
        });
        (group.currency);
        (__VLS_ctx.formatMoney(group.pendingBalance));
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
    for (const [group] of __VLS_getVForSourceType((__VLS_ctx.currencySummaries))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            key: (group.currency),
            ...{ class: "footer-total" },
        });
        (group.currency);
        (__VLS_ctx.formatMoney(group.totalBalance));
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
    (__VLS_ctx.successfulCount);
    (__VLS_ctx.accountCount);
}
/** @type {__VLS_StyleScopedClasses['page']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['page-header']} */ ;
/** @type {__VLS_StyleScopedClasses['page-title-cluster']} */ ;
/** @type {__VLS_StyleScopedClasses['eyebrow']} */ ;
/** @type {__VLS_StyleScopedClasses['primary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['read-button']} */ ;
/** @type {__VLS_StyleScopedClasses['balance-message']} */ ;
/** @type {__VLS_StyleScopedClasses['error-message']} */ ;
/** @type {__VLS_StyleScopedClasses['balance-message']} */ ;
/** @type {__VLS_StyleScopedClasses['success-message']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-card']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-copy']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-card']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-copy']} */ ;
/** @type {__VLS_StyleScopedClasses['currency-totals']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-card']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-copy']} */ ;
/** @type {__VLS_StyleScopedClasses['currency-totals']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-card']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-copy']} */ ;
/** @type {__VLS_StyleScopedClasses['currency-totals']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['balance-empty']} */ ;
/** @type {__VLS_StyleScopedClasses['table-wrap']} */ ;
/** @type {__VLS_StyleScopedClasses['data-table']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-balance-table']} */ ;
/** @type {__VLS_StyleScopedClasses['account-name']} */ ;
/** @type {__VLS_StyleScopedClasses['mono']} */ ;
/** @type {__VLS_StyleScopedClasses['amount']} */ ;
/** @type {__VLS_StyleScopedClasses['amount']} */ ;
/** @type {__VLS_StyleScopedClasses['amount']} */ ;
/** @type {__VLS_StyleScopedClasses['read-status']} */ ;
/** @type {__VLS_StyleScopedClasses['row-error']} */ ;
/** @type {__VLS_StyleScopedClasses['footer-total']} */ ;
/** @type {__VLS_StyleScopedClasses['footer-total']} */ ;
/** @type {__VLS_StyleScopedClasses['footer-total']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            FolioIcon: FolioIcon,
            OperationVisualAtom: OperationVisualAtom,
            rows: rows,
            loading: loading,
            loadError: loadError,
            hasRead: hasRead,
            loadedAt: loadedAt,
            dataSource: dataSource,
            accountCount: accountCount,
            successfulCount: successfulCount,
            currencySummaries: currencySummaries,
            formatMoney: formatMoney,
            rowCurrency: rowCurrency,
            readBalances: readBalances,
        };
    },
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
});
; /* PartiallyEnd: #4569/main.vue */
