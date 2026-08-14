import { computed, onMounted, ref, watch } from "vue";
import { RouterLink } from "vue-router";
import FolioIcon from "../components/FolioIcon.vue";
import { formatLocal, responseError, unwrapPayload } from "./guadao_shared";
const settings = ref(null);
const loading = ref(false);
const saving = ref(false);
const error = ref("");
const notice = ref("");
const dirty = ref(false);
const confirmSave = ref(false);
const search = ref("");
const suggestions = ref([]);
const searching = ref(false);
const searchHasMore = ref(false);
const searchNextOffset = ref(0);
let searchTimer = null;
const ratioWarning = computed(() => Boolean(settings.value?.specialRules.some(rule => rule.enabled && rule.maxRatioPct > 75)));
const validation = computed(() => { const s = settings.value; if (!s)
    return ["策略尚未加载"]; const errors = []; if (!(s.global.maxListingRatioPct > 0 && s.global.maxListingRatioPct <= 80))
    errors.push("全局最大挂刀比例必须大于 0 且不超过 80%"); for (const rule of s.specialRules) {
    if (!rule.marketHashName.trim())
        errors.push("特殊规则缺少 marketHashName");
    if (!(rule.maxRatioPct > 0))
        errors.push(`${rule.marketHashName} 的专用上限必须大于 0`);
    if (rule.maxRatioPct > 80)
        errors.push(`${rule.marketHashName} 的专用上限不能超过 80%`);
    if (rule.rebuyReferenceFloor != null && !(rule.rebuyReferenceFloor > 0))
        errors.push(`${rule.marketHashName} 的开单参考价下限必须大于 0`);
} if (s.timePolicy.scanMinutes < 1)
    errors.push("新机会扫描间隔不得低于 1 分钟"); if (s.timePolicy.steamSyncMaxStartLagSeconds < 1)
    errors.push("Steam 检查到期后的最多延迟不得低于 1 秒"); if (s.timePolicy.staleListedCheckHours < 1)
    errors.push("超过 48 小时挂单的查找间隔不得低于 1 小时"); if (s.timePolicy.actionConfirmSeconds.some(v => v < 2))
    errors.push("上架后确认间隔不得低于 2 秒"); if (s.timePolicy.soldEvidenceMinutes.some(v => v < 0))
    errors.push("在售挂单与卖出证据复查间隔不能为负数"); if (s.timePolicy.rebuyMinutes.some(v => v < .5) || s.timePolicy.deliveryMinutes.some(v => v < .5))
    errors.push("C5 检查间隔不得低于 30 秒"); const sequences = [s.timePolicy.actionConfirmSeconds, s.timePolicy.soldEvidenceMinutes, s.timePolicy.rebuyMinutes, s.timePolicy.deliveryMinutes]; if (sequences.some(values => values.some((value, index) => index > 0 && value < values[index - 1])))
    errors.push("同一组检查间隔必须从短到长排列"); return errors; });
function numeric(value, fallback = 0) { const parsed = Number(value); return Number.isFinite(parsed) ? parsed : fallback; }
function durationLabel(seconds) { if (seconds == null)
    return "—"; if (seconds % 3600 === 0)
    return `${seconds / 3600} 小时`; if (seconds % 60 === 0)
    return `${seconds / 60} 分钟`; return `${seconds} 秒`; }
function auditText(value) { if (value == null)
    return "—"; const text = typeof value === "string" ? value : JSON.stringify(value); return text.length > 120 ? `${text.slice(0, 117)}…` : text; }
function adaptSettings(raw) { const task = (raw.taskSchedule && typeof raw.taskSchedule === "object" ? raw.taskSchedule : {}); const nestedGlobal = (raw.global && typeof raw.global === "object" ? raw.global : {}); const rules = (Array.isArray(raw.specialRules) ? raw.specialRules : Array.isArray(raw.specialCaseRatioRules) ? raw.specialCaseRatioRules : []); const audit = (Array.isArray(raw.audit) ? raw.audit : []); const tierMinutes = (key) => (Array.isArray(task[key]) ? task[key] : []).map(tier => numeric(tier.intervalSeconds) / 60); return { runtime: (raw.runtime || undefined), global: { maxListingRatioPct: nestedGlobal.maxListingRatioPct == null ? numeric(raw.guadaoMaxListingRatio) * 100 : numeric(nestedGlobal.maxListingRatioPct), steamNetFactorPct: nestedGlobal.steamNetFactorPct == null ? (raw.steamNetFactor == null ? null : numeric(raw.steamNetFactor) * 100) : numeric(nestedGlobal.steamNetFactorPct), maxNewListingsPerCycle: nestedGlobal.maxNewListingsPerCycle == null ? (raw.maxNewListingsPerCycle == null ? null : numeric(raw.maxNewListingsPerCycle)) : numeric(nestedGlobal.maxNewListingsPerCycle), caseMaxOpenCount: nestedGlobal.caseMaxOpenCount == null ? (raw.caseMaxOpenCount == null ? null : numeric(raw.caseMaxOpenCount)) : numeric(nestedGlobal.caseMaxOpenCount), autoListing: nestedGlobal.autoListing == null ? Boolean(raw.autoListEnabled) : Boolean(nestedGlobal.autoListing), autoRebuy: nestedGlobal.autoRebuy == null ? Boolean(raw.autoRebuyEnabled) : Boolean(nestedGlobal.autoRebuy), lastModifiedAt: String(nestedGlobal.lastModifiedAt || "") || null }, specialRules: rules.map(rule => ({ id: String(rule.id || rule.ruleId || ""), marketHashName: String(rule.marketHashName || ""), displayName: String(rule.displayName || rule.nameCn || "") || null, maxRatioPct: rule.maxRatioPct == null ? numeric(rule.maxListingRatio) * 100 : numeric(rule.maxRatioPct), rebuyReferenceFloor: rule.rebuyReferenceFloor == null ? null : numeric(rule.rebuyReferenceFloor), currentRatioPct: rule.currentRatioPct == null ? null : numeric(rule.currentRatioPct), currentRatioObservedAt: String(rule.currentRatioObservedAt || "") || null, enabled: rule.enabled !== false, version: numeric(rule.version, 1), updatedAt: String(rule.updatedAt || "") || null })), timePolicy: { scanMinutes: numeric(task.scanIntervalSeconds) / 60, steamSyncSeconds: numeric(task.steamSyncIntervalSeconds), steamSyncMaxStartLagSeconds: numeric(task.steamSyncMaxStartLagSeconds, 60), staleListedCheckHours: numeric(task.staleListedCheckIntervalSeconds, 86400) / 3600, actionConfirmSeconds: (Array.isArray(task.actionConfirmationDelaysSeconds) ? task.actionConfirmationDelaysSeconds : []).map(value => numeric(value)), soldEvidenceMinutes: (Array.isArray(task.saleEvidenceDelaysSeconds) ? task.saleEvidenceDelaysSeconds : []).map(value => numeric(value) / 60), rebuyMinutes: tierMinutes("rebuyRetryTiers"), deliveryMinutes: tierMinutes("deliveryConfirmationTiers"), staleListedRecheckHours: raw.staleListedRecheckHours == null ? null : numeric(raw.staleListedRecheckHours), staleListedMaxRatioTolerancePct: raw.staleListedMaxRatioTolerancePct == null ? null : numeric(raw.staleListedMaxRatioTolerancePct) }, rawTaskSchedule: JSON.parse(JSON.stringify(task)), steamScheduler: (raw.steamScheduler || undefined), audit: audit.map(row => ({ id: String(row.id || ""), at: String(row.createdAt || row.at || ""), actor: String(row.actor || ""), summary: String(row.reason || row.summary || ""), changes: row.diff ? JSON.stringify(row.diff) : String(row.changes || ""), oldValue: row.oldValue, newValue: row.newValue, diff: row.diff })) }; }
async function load() { loading.value = true; try {
    const response = await fetch("/api/guadao/settings", { cache: "no-store" });
    if (!response.ok)
        throw new Error(await responseError(response));
    settings.value = adaptSettings(unwrapPayload(await response.json(), "settings"));
    dirty.value = false;
    error.value = "";
}
catch (reason) {
    error.value = reason instanceof Error ? reason.message : String(reason);
}
finally {
    loading.value = false;
} }
function changed() { dirty.value = true; notice.value = ""; }
async function save() { if (!settings.value || validation.value.length)
    return; const current = settings.value; const rawTask = { ...current.rawTaskSchedule, scanIntervalSeconds: current.timePolicy.scanMinutes * 60, steamSyncIntervalSeconds: current.timePolicy.steamSyncSeconds, steamSyncMaxStartLagSeconds: current.timePolicy.steamSyncMaxStartLagSeconds, staleListedCheckIntervalSeconds: current.timePolicy.staleListedCheckHours * 3600, actionConfirmationDelaysSeconds: current.timePolicy.actionConfirmSeconds, saleEvidenceDelaysSeconds: current.timePolicy.soldEvidenceMinutes.map(value => value * 60) }; const rebuyRaw = (Array.isArray(rawTask.rebuyRetryTiers) ? rawTask.rebuyRetryTiers : []); const deliveryRaw = (Array.isArray(rawTask.deliveryConfirmationTiers) ? rawTask.deliveryConfirmationTiers : []); rawTask.rebuyRetryTiers = current.timePolicy.rebuyMinutes.map((value, index) => ({ ...rebuyRaw[index], intervalSeconds: value * 60 })); rawTask.deliveryConfirmationTiers = current.timePolicy.deliveryMinutes.map((value, index) => ({ ...deliveryRaw[index], intervalSeconds: value * 60 })); const body = { guadaoMaxListingRatio: current.global.maxListingRatioPct / 100, autoListEnabled: current.global.autoListing, autoRebuyEnabled: current.global.autoRebuy, maxListPerCycle: current.global.maxNewListingsPerCycle, caseMaxOpenGuadaoCount: current.global.caseMaxOpenCount, staleListedRecheckHours: current.timePolicy.staleListedRecheckHours, staleListedMaxRatioTolerancePct: current.timePolicy.staleListedMaxRatioTolerancePct, specialCaseRatioRules: current.specialRules.map(rule => ({ ruleId: rule.id, version: rule.version, marketHashName: rule.marketHashName, nameCn: rule.displayName, maxListingRatio: rule.maxRatioPct / 100, rebuyReferenceFloor: rule.rebuyReferenceFloor, enabled: rule.enabled })), taskSchedule: rawTask, confirmHighRatio: ratioWarning.value }; saving.value = true; try {
    const response = await fetch("/api/guadao/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!response.ok)
        throw new Error(await responseError(response));
    settings.value = adaptSettings(unwrapPayload(await response.json(), "settings"));
    dirty.value = false;
    confirmSave.value = false;
    notice.value = "策略已保存；已开启流水继续使用开单时冻结值，修改只影响未来流水。";
    error.value = "";
}
catch (reason) {
    error.value = reason instanceof Error ? reason.message : String(reason);
}
finally {
    saving.value = false;
} }
function requestSave() { if (validation.value.length) {
    error.value = validation.value.join("；");
    return;
} confirmSave.value = true; }
watch(search, value => { if (searchTimer)
    clearTimeout(searchTimer); suggestions.value = []; searchHasMore.value = false; searchNextOffset.value = 0; if (value.trim().length < 2)
    return; searchTimer = setTimeout(() => void findItems(value.trim()), 300); });
async function findItems(query, append = false) { searching.value = true; try {
    const offset = append ? searchNextOffset.value : 0;
    const response = await fetch(`/api/guadao/items/search?q=${encodeURIComponent(query)}&limit=12&offset=${offset}`, { cache: "no-store" });
    if (!response.ok)
        throw new Error(await responseError(response));
    const payload = unwrapPayload(await response.json());
    const incoming = Array.isArray(payload) ? payload : payload.items || [];
    if (query !== search.value.trim())
        return;
    if (append) {
        const merged = new Map(suggestions.value.map(item => [item.marketHashName, item]));
        for (const item of incoming)
            merged.set(item.marketHashName, item);
        suggestions.value = [...merged.values()];
    }
    else
        suggestions.value = incoming;
    if (!Array.isArray(payload)) {
        searchHasMore.value = Boolean(payload.pagination?.hasMore);
        searchNextOffset.value = Number(payload.pagination?.nextOffset ?? 0);
    }
}
catch (reason) {
    error.value = `物品搜索失败：${reason instanceof Error ? reason.message : String(reason)}`;
}
finally {
    searching.value = false;
} }
function addRule(item) { if (!settings.value)
    return; if (settings.value.specialRules.some(rule => rule.marketHashName === item.marketHashName)) {
    search.value = "";
    suggestions.value = [];
    searchHasMore.value = false;
    return;
} settings.value.specialRules.push({ marketHashName: item.marketHashName, displayName: item.displayName || item.nameCn || item.name || null, maxRatioPct: settings.value.global.maxListingRatioPct, rebuyReferenceFloor: null, enabled: true }); search.value = ""; suggestions.value = []; searchHasMore.value = false; changed(); }
function removeRule(index) { settings.value?.specialRules.splice(index, 1); changed(); }
onMounted(load);
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
/** @type {__VLS_StyleScopedClasses['settings-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['settings-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-card']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-card']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-card']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-card']} */ ;
/** @type {__VLS_StyleScopedClasses['feedback']} */ ;
/** @type {__VLS_StyleScopedClasses['feedback']} */ ;
/** @type {__VLS_StyleScopedClasses['card-title']} */ ;
/** @type {__VLS_StyleScopedClasses['card-title']} */ ;
/** @type {__VLS_StyleScopedClasses['field-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['field-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['field-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['field-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['field-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-switch']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-switch']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-switch']} */ ;
/** @type {__VLS_StyleScopedClasses['on']} */ ;
/** @type {__VLS_StyleScopedClasses['item-search']} */ ;
/** @type {__VLS_StyleScopedClasses['item-search']} */ ;
/** @type {__VLS_StyleScopedClasses['item-search']} */ ;
/** @type {__VLS_StyleScopedClasses['suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['rule-row']} */ ;
/** @type {__VLS_StyleScopedClasses['rule-head']} */ ;
/** @type {__VLS_StyleScopedClasses['rule-row']} */ ;
/** @type {__VLS_StyleScopedClasses['rule-row']} */ ;
/** @type {__VLS_StyleScopedClasses['rule-row']} */ ;
/** @type {__VLS_StyleScopedClasses['rule-row']} */ ;
/** @type {__VLS_StyleScopedClasses['tiny-switch']} */ ;
/** @type {__VLS_StyleScopedClasses['tiny-switch']} */ ;
/** @type {__VLS_StyleScopedClasses['on']} */ ;
/** @type {__VLS_StyleScopedClasses['tiny-switch']} */ ;
/** @type {__VLS_StyleScopedClasses['on']} */ ;
/** @type {__VLS_StyleScopedClasses['rule-note']} */ ;
/** @type {__VLS_StyleScopedClasses['policy-note']} */ ;
/** @type {__VLS_StyleScopedClasses['policy-form']} */ ;
/** @type {__VLS_StyleScopedClasses['policy-form']} */ ;
/** @type {__VLS_StyleScopedClasses['policy-form']} */ ;
/** @type {__VLS_StyleScopedClasses['policy-form']} */ ;
/** @type {__VLS_StyleScopedClasses['policy-form']} */ ;
/** @type {__VLS_StyleScopedClasses['policy-form']} */ ;
/** @type {__VLS_StyleScopedClasses['policy-form']} */ ;
/** @type {__VLS_StyleScopedClasses['multi']} */ ;
/** @type {__VLS_StyleScopedClasses['policy-form']} */ ;
/** @type {__VLS_StyleScopedClasses['scheduler-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['scheduler-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-card']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-card']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-list']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-list']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-list']} */ ;
/** @type {__VLS_StyleScopedClasses['save-bar']} */ ;
/** @type {__VLS_StyleScopedClasses['empty-state']} */ ;
/** @type {__VLS_StyleScopedClasses['save-dialog']} */ ;
/** @type {__VLS_StyleScopedClasses['save-dialog']} */ ;
/** @type {__VLS_StyleScopedClasses['save-dialog']} */ ;
/** @type {__VLS_StyleScopedClasses['save-dialog']} */ ;
/** @type {__VLS_StyleScopedClasses['field-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['rule-head']} */ ;
/** @type {__VLS_StyleScopedClasses['rule-row']} */ ;
/** @type {__VLS_StyleScopedClasses['rule-row']} */ ;
/** @type {__VLS_StyleScopedClasses['rule-row']} */ ;
/** @type {__VLS_StyleScopedClasses['rule-row']} */ ;
/** @type {__VLS_StyleScopedClasses['delivery-boundary-note']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-table']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-head']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-table']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-table']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-table']} */ ;
/** @type {__VLS_StyleScopedClasses['policy-form']} */ ;
/** @type {__VLS_StyleScopedClasses['policy-form']} */ ;
/** @type {__VLS_StyleScopedClasses['policy-form']} */ ;
/** @type {__VLS_StyleScopedClasses['policy-form']} */ ;
/** @type {__VLS_StyleScopedClasses['policy-form']} */ ;
/** @type {__VLS_StyleScopedClasses['policy-form']} */ ;
/** @type {__VLS_StyleScopedClasses['multi']} */ ;
/** @type {__VLS_StyleScopedClasses['policy-form']} */ ;
/** @type {__VLS_StyleScopedClasses['settings-card']} */ ;
/** @type {__VLS_StyleScopedClasses['special-card']} */ ;
/** @type {__VLS_StyleScopedClasses['item-search']} */ ;
/** @type {__VLS_StyleScopedClasses['field-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['policy-form']} */ ;
/** @type {__VLS_StyleScopedClasses['rule-row']} */ ;
/** @type {__VLS_StyleScopedClasses['item-search']} */ ;
/** @type {__VLS_StyleScopedClasses['item-search']} */ ;
/** @type {__VLS_StyleScopedClasses['rule-note']} */ ;
/** @type {__VLS_StyleScopedClasses['policy-form']} */ ;
/** @type {__VLS_StyleScopedClasses['multi']} */ ;
/** @type {__VLS_StyleScopedClasses['policy-form']} */ ;
/** @type {__VLS_StyleScopedClasses['multi']} */ ;
/** @type {__VLS_StyleScopedClasses['policy-form']} */ ;
/** @type {__VLS_StyleScopedClasses['multi']} */ ;
/** @type {__VLS_StyleScopedClasses['policy-note']} */ ;
/** @type {__VLS_StyleScopedClasses['delivery-boundary-note']} */ ;
/** @type {__VLS_StyleScopedClasses['delivery-boundary-note']} */ ;
/** @type {__VLS_StyleScopedClasses['settings-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['policy-form']} */ ;
/** @type {__VLS_StyleScopedClasses['settings-page']} */ ;
/** @type {__VLS_StyleScopedClasses['settings-card']} */ ;
/** @type {__VLS_StyleScopedClasses['policy-form']} */ ;
/** @type {__VLS_StyleScopedClasses['policy-form']} */ ;
/** @type {__VLS_StyleScopedClasses['policy-form']} */ ;
/** @type {__VLS_StyleScopedClasses['multi']} */ ;
/** @type {__VLS_StyleScopedClasses['policy-form']} */ ;
/** @type {__VLS_StyleScopedClasses['delivery-boundary-note']} */ ;
/** @type {__VLS_StyleScopedClasses['suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['catalog-load-more']} */ ;
// CSS variable injection 
// CSS variable injection end 
__VLS_asFunctionalElement(__VLS_intrinsicElements.main, __VLS_intrinsicElements.main)({
    ...{ class: "page settings-page" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({
    ...{ class: "settings-heading" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
    ...{ class: "eyebrow" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h1, __VLS_intrinsicElements.h1)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "runtime-card" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.settings?.runtime?.enabled ? "当前运行中" : "当前已关闭");
const __VLS_0 = {}.RouterLink;
/** @type {[typeof __VLS_components.RouterLink, typeof __VLS_components.RouterLink, ]} */ ;
// @ts-ignore
const __VLS_1 = __VLS_asFunctionalComponent(__VLS_0, new __VLS_0({
    to: "/guadao/overview",
}));
const __VLS_2 = __VLS_1({
    to: "/guadao/overview",
}, ...__VLS_functionalComponentArgsRest(__VLS_1));
__VLS_3.slots.default;
var __VLS_3;
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
if (__VLS_ctx.loading && !__VLS_ctx.settings) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "empty-state" },
    });
}
if (__VLS_ctx.settings) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "settings-grid" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "left-column" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
        ...{ class: "panel settings-card" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "card-title" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "field-grid" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.br, __VLS_intrinsicElements.br)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (__VLS_ctx.formatLocal(__VLS_ctx.settings.global.lastModifiedAt));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
        ...{ onInput: (__VLS_ctx.changed) },
        type: "number",
        min: "0.01",
        max: "80",
        step: "0.01",
    });
    (__VLS_ctx.settings.global.maxListingRatioPct);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.em, __VLS_intrinsicElements.em)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
        value: (__VLS_ctx.settings.global.steamNetFactorPct ?? ''),
        type: "number",
        disabled: true,
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.em, __VLS_intrinsicElements.em)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.br, __VLS_intrinsicElements.br)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
        ...{ onInput: (__VLS_ctx.changed) },
        type: "number",
        min: "0",
        step: "1",
    });
    (__VLS_ctx.settings.global.maxNewListingsPerCycle);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
        ...{ onInput: (__VLS_ctx.changed) },
        type: "number",
        min: "1",
        step: "1",
    });
    (__VLS_ctx.settings.global.caseMaxOpenCount);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
        ...{ class: "toggle-row" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.settings))
                    return;
                __VLS_ctx.settings.global.autoListing = !__VLS_ctx.settings.global.autoListing;
                __VLS_ctx.changed();
            } },
        type: "button",
        ...{ class: (['mini-switch', { on: __VLS_ctx.settings.global.autoListing }]) },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({});
    (__VLS_ctx.settings.global.autoListing ? "ON" : "OFF");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
        ...{ class: "toggle-row" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.settings))
                    return;
                __VLS_ctx.settings.global.autoRebuy = !__VLS_ctx.settings.global.autoRebuy;
                __VLS_ctx.changed();
            } },
        type: "button",
        ...{ class: (['mini-switch', { on: __VLS_ctx.settings.global.autoRebuy }]) },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({});
    (__VLS_ctx.settings.global.autoRebuy ? "ON" : "OFF");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "frozen-note" },
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_4 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: "warning",
        size: (14),
    }));
    const __VLS_5 = __VLS_4({
        name: "warning",
        size: (14),
    }, ...__VLS_functionalComponentArgsRest(__VLS_4));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
        ...{ class: "panel settings-card special-card" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "card-title" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "item-search" },
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_7 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: "scan",
        size: (15),
    }));
    const __VLS_8 = __VLS_7({
        name: "scan",
        size: (15),
    }, ...__VLS_functionalComponentArgsRest(__VLS_7));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
        placeholder: "输入物品中文名或 marketHashName",
    });
    (__VLS_ctx.search);
    if (__VLS_ctx.searching) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    }
    if (__VLS_ctx.suggestions.length) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "suggestions" },
        });
        for (const [item] of __VLS_getVForSourceType((__VLS_ctx.suggestions))) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
                ...{ onClick: (...[$event]) => {
                        if (!(__VLS_ctx.settings))
                            return;
                        if (!(__VLS_ctx.suggestions.length))
                            return;
                        __VLS_ctx.addRule(item);
                    } },
                key: (item.marketHashName),
                type: "button",
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
            (item.displayName || item.nameCn || item.name || item.marketHashName);
            __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
            (item.marketHashName);
        }
        if (__VLS_ctx.searchHasMore) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
                ...{ onClick: (...[$event]) => {
                        if (!(__VLS_ctx.settings))
                            return;
                        if (!(__VLS_ctx.suggestions.length))
                            return;
                        if (!(__VLS_ctx.searchHasMore))
                            return;
                        __VLS_ctx.findItems(__VLS_ctx.search.trim(), true);
                    } },
                ...{ class: "catalog-load-more" },
                type: "button",
                disabled: (__VLS_ctx.searching),
            });
            (__VLS_ctx.searching ? "加载中…" : "加载更多结果");
        }
    }
    if (__VLS_ctx.settings.specialRules.length) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "rules-table" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "rule-head" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        for (const [rule, index] of __VLS_getVForSourceType((__VLS_ctx.settings.specialRules))) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
                key: (rule.id || rule.marketHashName),
                ...{ class: "rule-row" },
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
                ...{ onClick: (...[$event]) => {
                        if (!(__VLS_ctx.settings))
                            return;
                        if (!(__VLS_ctx.settings.specialRules.length))
                            return;
                        rule.enabled = !rule.enabled;
                        __VLS_ctx.changed();
                    } },
                type: "button",
                ...{ class: (['tiny-switch', { on: rule.enabled }]) },
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
            (rule.displayName || rule.marketHashName);
            __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
            (rule.marketHashName);
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
            (__VLS_ctx.settings.global.maxListingRatioPct.toFixed(2));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
                ...{ onInput: (__VLS_ctx.changed) },
                type: "number",
                min: "0.01",
                max: "80",
                step: "0.01",
            });
            (rule.maxRatioPct);
            __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
                ...{ onInput: (__VLS_ctx.changed) },
                type: "number",
                min: "0.01",
                step: "0.01",
                placeholder: "不设置",
            });
            (rule.rebuyReferenceFloor);
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
            (rule.currentRatioPct == null ? "—" : `${rule.currentRatioPct.toFixed(2)}%`);
            __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
            (__VLS_ctx.formatLocal(rule.currentRatioObservedAt));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
            (rule.version || "—");
            __VLS_asFunctionalElement(__VLS_intrinsicElements.time, __VLS_intrinsicElements.time)({});
            (__VLS_ctx.formatLocal(rule.updatedAt));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
                ...{ onClick: (...[$event]) => {
                        if (!(__VLS_ctx.settings))
                            return;
                        if (!(__VLS_ctx.settings.specialRules.length))
                            return;
                        __VLS_ctx.removeRule(index);
                    } },
                ...{ class: "text-danger" },
                type: "button",
            });
        }
    }
    else {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "empty-state compact" },
        });
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "rule-note" },
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_10 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: "success",
        size: (13),
    }));
    const __VLS_11 = __VLS_10({
        name: "success",
        size: (13),
    }, ...__VLS_functionalComponentArgsRest(__VLS_10));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "right-column" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
        ...{ class: "panel settings-card time-card" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "card-title" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "policy-form" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
        ...{ onInput: (__VLS_ctx.changed) },
        min: "1",
        type: "number",
    });
    (__VLS_ctx.settings.timePolicy.scanMinutes);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
        ...{ onInput: (__VLS_ctx.changed) },
        min: "1",
        type: "number",
    });
    (__VLS_ctx.settings.timePolicy.steamSyncMaxStartLagSeconds);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "multi" },
    });
    for (const [_, index] of __VLS_getVForSourceType((__VLS_ctx.settings.timePolicy.actionConfirmSeconds))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
            ...{ onInput: (__VLS_ctx.changed) },
            key: (index),
            min: "2",
            type: "number",
        });
        (__VLS_ctx.settings.timePolicy.actionConfirmSeconds[index]);
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.em, __VLS_intrinsicElements.em)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "multi four" },
    });
    for (const [_, index] of __VLS_getVForSourceType((__VLS_ctx.settings.timePolicy.soldEvidenceMinutes))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
            ...{ onInput: (__VLS_ctx.changed) },
            key: (index),
            min: "0",
            type: "number",
        });
        (__VLS_ctx.settings.timePolicy.soldEvidenceMinutes[index]);
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.em, __VLS_intrinsicElements.em)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "multi" },
    });
    for (const [_, index] of __VLS_getVForSourceType((__VLS_ctx.settings.timePolicy.rebuyMinutes))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
            ...{ onInput: (__VLS_ctx.changed) },
            key: (index),
            min: "0.5",
            step: "0.5",
            type: "number",
        });
        (__VLS_ctx.settings.timePolicy.rebuyMinutes[index]);
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.em, __VLS_intrinsicElements.em)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "multi four" },
    });
    for (const [_, index] of __VLS_getVForSourceType((__VLS_ctx.settings.timePolicy.deliveryMinutes))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
            ...{ onInput: (__VLS_ctx.changed) },
            key: (index),
            min: "0.5",
            step: "0.5",
            type: "number",
        });
        (__VLS_ctx.settings.timePolicy.deliveryMinutes[index]);
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.em, __VLS_intrinsicElements.em)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
        ...{ onInput: (__VLS_ctx.changed) },
        min: "1",
        type: "number",
    });
    (__VLS_ctx.settings.timePolicy.staleListedCheckHours);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
        ...{ onInput: (__VLS_ctx.changed) },
        min: "1",
        type: "number",
    });
    (__VLS_ctx.settings.timePolicy.staleListedRecheckHours);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
        ...{ onInput: (__VLS_ctx.changed) },
        min: "0",
        max: "20",
        step: "0.1",
        type: "number",
    });
    (__VLS_ctx.settings.timePolicy.staleListedMaxRatioTolerancePct);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "policy-note" },
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_13 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: "success",
        size: (13),
    }));
    const __VLS_14 = __VLS_13({
        name: "success",
        size: (13),
    }, ...__VLS_functionalComponentArgsRest(__VLS_13));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "delivery-boundary-note" },
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_16 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: "shield",
        size: (13),
    }));
    const __VLS_17 = __VLS_16({
        name: "shield",
        size: (13),
    }, ...__VLS_functionalComponentArgsRest(__VLS_16));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
        ...{ class: "panel settings-card scheduler-card" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "card-title" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
    if (__VLS_ctx.settings.steamScheduler) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "scheduler-grid" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (__VLS_ctx.settings.steamScheduler.mode === "single_channel" ? "单通道队列" : "—");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (__VLS_ctx.durationLabel(__VLS_ctx.settings.steamScheduler.accountRouteCooldownSeconds));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (__VLS_ctx.durationLabel(__VLS_ctx.settings.steamScheduler.globalCooldownSeconds));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        (__VLS_ctx.durationLabel(__VLS_ctx.settings.steamScheduler.degradedAfterSeconds));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (__VLS_ctx.durationLabel(__VLS_ctx.settings.steamScheduler.degradedProbeSeconds));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (__VLS_ctx.settings.steamScheduler.quietWindowEnabled ? "已启用" : "未启用");
    }
    else {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "empty-state compact" },
        });
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "panel audit-card" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    if (__VLS_ctx.settings.audit?.length) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "audit-table" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "audit-head" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        for (const [row] of __VLS_getVForSourceType((__VLS_ctx.settings.audit.slice(0, 8)))) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
                key: (row.id),
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.time, __VLS_intrinsicElements.time)({});
            (__VLS_ctx.formatLocal(row.at));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
            (row.actor || "本地用户");
            __VLS_asFunctionalElement(__VLS_intrinsicElements.code, __VLS_intrinsicElements.code)({
                title: (JSON.stringify(row.oldValue)),
            });
            (__VLS_ctx.auditText(row.oldValue));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.code, __VLS_intrinsicElements.code)({
                title: (JSON.stringify(row.newValue)),
            });
            (__VLS_ctx.auditText(row.newValue));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.code, __VLS_intrinsicElements.code)({
                title: (JSON.stringify(row.diff)),
            });
            (__VLS_ctx.auditText(row.diff || row.changes));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
            (row.summary || "挂刀策略设置更新");
        }
    }
    else {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "empty-state compact" },
        });
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.footer, __VLS_intrinsicElements.footer)({
        ...{ class: "save-bar" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.load) },
        ...{ class: "secondary-button" },
        type: "button",
        disabled: (!__VLS_ctx.dirty),
    });
    if (__VLS_ctx.validation.length) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        /** @type {[typeof FolioIcon, ]} */ ;
        // @ts-ignore
        const __VLS_19 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
            name: "warning",
            size: (14),
        }));
        const __VLS_20 = __VLS_19({
            name: "warning",
            size: (14),
        }, ...__VLS_functionalComponentArgsRest(__VLS_19));
        (__VLS_ctx.validation[0]);
    }
    else if (__VLS_ctx.dirty) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    }
    else {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.requestSave) },
        ...{ class: "primary-button" },
        type: "button",
        disabled: (!__VLS_ctx.dirty || __VLS_ctx.saving || __VLS_ctx.validation.length > 0),
    });
}
if (__VLS_ctx.confirmSave) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.confirmSave))
                    return;
                __VLS_ctx.confirmSave = false;
            } },
        ...{ class: "modal-backdrop" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "save-dialog" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_22 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: (__VLS_ctx.ratioWarning ? 'warning' : 'shield'),
        size: (22),
    }));
    const __VLS_23 = __VLS_22({
        name: (__VLS_ctx.ratioWarning ? 'warning' : 'shield'),
        size: (22),
    }, ...__VLS_functionalComponentArgsRest(__VLS_22));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
    (__VLS_ctx.ratioWarning ? "确认保存高比例规则" : "确认保存挂刀策略");
    if (__VLS_ctx.ratioWarning) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    }
    else {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.confirmSave))
                    return;
                __VLS_ctx.confirmSave = false;
            } },
        ...{ class: "secondary-button" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.save) },
        ...{ class: "primary-button" },
        disabled: (__VLS_ctx.saving),
    });
    (__VLS_ctx.saving ? "保存中…" : "确认保存");
}
/** @type {__VLS_StyleScopedClasses['page']} */ ;
/** @type {__VLS_StyleScopedClasses['settings-page']} */ ;
/** @type {__VLS_StyleScopedClasses['settings-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['eyebrow']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-card']} */ ;
/** @type {__VLS_StyleScopedClasses['feedback']} */ ;
/** @type {__VLS_StyleScopedClasses['error']} */ ;
/** @type {__VLS_StyleScopedClasses['feedback']} */ ;
/** @type {__VLS_StyleScopedClasses['success']} */ ;
/** @type {__VLS_StyleScopedClasses['empty-state']} */ ;
/** @type {__VLS_StyleScopedClasses['settings-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['left-column']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['settings-card']} */ ;
/** @type {__VLS_StyleScopedClasses['card-title']} */ ;
/** @type {__VLS_StyleScopedClasses['field-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['toggle-row']} */ ;
/** @type {__VLS_StyleScopedClasses['toggle-row']} */ ;
/** @type {__VLS_StyleScopedClasses['frozen-note']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['settings-card']} */ ;
/** @type {__VLS_StyleScopedClasses['special-card']} */ ;
/** @type {__VLS_StyleScopedClasses['card-title']} */ ;
/** @type {__VLS_StyleScopedClasses['item-search']} */ ;
/** @type {__VLS_StyleScopedClasses['suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['catalog-load-more']} */ ;
/** @type {__VLS_StyleScopedClasses['rules-table']} */ ;
/** @type {__VLS_StyleScopedClasses['rule-head']} */ ;
/** @type {__VLS_StyleScopedClasses['rule-row']} */ ;
/** @type {__VLS_StyleScopedClasses['text-danger']} */ ;
/** @type {__VLS_StyleScopedClasses['empty-state']} */ ;
/** @type {__VLS_StyleScopedClasses['compact']} */ ;
/** @type {__VLS_StyleScopedClasses['rule-note']} */ ;
/** @type {__VLS_StyleScopedClasses['right-column']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['settings-card']} */ ;
/** @type {__VLS_StyleScopedClasses['time-card']} */ ;
/** @type {__VLS_StyleScopedClasses['card-title']} */ ;
/** @type {__VLS_StyleScopedClasses['policy-form']} */ ;
/** @type {__VLS_StyleScopedClasses['multi']} */ ;
/** @type {__VLS_StyleScopedClasses['multi']} */ ;
/** @type {__VLS_StyleScopedClasses['four']} */ ;
/** @type {__VLS_StyleScopedClasses['multi']} */ ;
/** @type {__VLS_StyleScopedClasses['multi']} */ ;
/** @type {__VLS_StyleScopedClasses['four']} */ ;
/** @type {__VLS_StyleScopedClasses['policy-note']} */ ;
/** @type {__VLS_StyleScopedClasses['delivery-boundary-note']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['settings-card']} */ ;
/** @type {__VLS_StyleScopedClasses['scheduler-card']} */ ;
/** @type {__VLS_StyleScopedClasses['card-title']} */ ;
/** @type {__VLS_StyleScopedClasses['scheduler-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['empty-state']} */ ;
/** @type {__VLS_StyleScopedClasses['compact']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-card']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-table']} */ ;
/** @type {__VLS_StyleScopedClasses['audit-head']} */ ;
/** @type {__VLS_StyleScopedClasses['empty-state']} */ ;
/** @type {__VLS_StyleScopedClasses['compact']} */ ;
/** @type {__VLS_StyleScopedClasses['save-bar']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['primary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['modal-backdrop']} */ ;
/** @type {__VLS_StyleScopedClasses['save-dialog']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['primary-button']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            RouterLink: RouterLink,
            FolioIcon: FolioIcon,
            formatLocal: formatLocal,
            settings: settings,
            loading: loading,
            saving: saving,
            error: error,
            notice: notice,
            dirty: dirty,
            confirmSave: confirmSave,
            search: search,
            suggestions: suggestions,
            searching: searching,
            searchHasMore: searchHasMore,
            ratioWarning: ratioWarning,
            validation: validation,
            durationLabel: durationLabel,
            auditText: auditText,
            load: load,
            changed: changed,
            save: save,
            requestSave: requestSave,
            findItems: findItems,
            addRule: addRule,
            removeRule: removeRule,
        };
    },
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
});
; /* PartiallyEnd: #4569/main.vue */
