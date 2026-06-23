import { computed, ref } from "vue";
import AccountProfitPage from "./pages/account_profit.vue";
import SteamBalancesPage from "./pages/steam_balances.vue";
import GuadaoReportPage from "./pages/guadao_report.vue";
import CaseRatioMonitorPage from "./pages/case_ratio_monitor.vue";
const pages = [
    { key: "account", label: "挂刀余额核对", component: AccountProfitPage },
    { key: "steam", label: "Steam余额统计", component: SteamBalancesPage },
    { key: "guadao", label: "挂刀报表", component: GuadaoReportPage },
    { key: "case-ratio", label: "箱子挂刀比", component: CaseRatioMonitorPage },
];
const activePage = ref("account");
const activeComponent = computed(() => pages.find((page) => page.key === activePage.value)?.component ?? AccountProfitPage);
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "app-shell" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.nav, __VLS_intrinsicElements.nav)({
    ...{ class: "top-nav" },
    'aria-label': "Primary",
});
for (const [page] of __VLS_getVForSourceType((__VLS_ctx.pages))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                __VLS_ctx.activePage = page.key;
            } },
        key: (page.key),
        type: "button",
        ...{ class: "nav-tab" },
        ...{ class: ({ active: __VLS_ctx.activePage === page.key }) },
    });
    (page.label);
}
const __VLS_0 = ((__VLS_ctx.activeComponent));
// @ts-ignore
const __VLS_1 = __VLS_asFunctionalComponent(__VLS_0, new __VLS_0({}));
const __VLS_2 = __VLS_1({}, ...__VLS_functionalComponentArgsRest(__VLS_1));
/** @type {__VLS_StyleScopedClasses['app-shell']} */ ;
/** @type {__VLS_StyleScopedClasses['top-nav']} */ ;
/** @type {__VLS_StyleScopedClasses['nav-tab']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            pages: pages,
            activePage: activePage,
            activeComponent: activeComponent,
        };
    },
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
});
; /* PartiallyEnd: #4569/main.vue */
