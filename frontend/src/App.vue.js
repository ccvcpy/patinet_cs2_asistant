import { RouterLink, RouterView } from "vue-router";
import FolioIcon from "./components/FolioIcon.vue";
const pages = [
    { to: "/account", label: "挂刀余额核对", icon: "account" },
    { to: "/steam", label: "Steam余额统计", icon: "wallet" },
    { to: "/guadao/overview", match: "/guadao", label: "挂刀运营", icon: "report" },
    { to: "/case-ratio", label: "箱子挂刀比", icon: "case" },
    { to: "/profit-trade/overview", match: "/profit-trade", label: "搬砖做T", icon: "scan" },
    { to: "/c5-sweeper", label: "C5扫货", icon: "price" },
];
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "app-shell" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.nav, __VLS_intrinsicElements.nav)({
    ...{ class: "top-nav" },
    'aria-label': "主导航",
});
for (const [page] of __VLS_getVForSourceType((__VLS_ctx.pages))) {
    const __VLS_0 = {}.RouterLink;
    /** @type {[typeof __VLS_components.RouterLink, typeof __VLS_components.RouterLink, ]} */ ;
    // @ts-ignore
    const __VLS_1 = __VLS_asFunctionalComponent(__VLS_0, new __VLS_0({
        key: (page.to),
        to: (page.to),
        ...{ class: "nav-tab" },
        ...{ class: ({ active: page.match ? __VLS_ctx.$route.path.startsWith(page.match) : __VLS_ctx.$route.path === page.to }) },
    }));
    const __VLS_2 = __VLS_1({
        key: (page.to),
        to: (page.to),
        ...{ class: "nav-tab" },
        ...{ class: ({ active: page.match ? __VLS_ctx.$route.path.startsWith(page.match) : __VLS_ctx.$route.path === page.to }) },
    }, ...__VLS_functionalComponentArgsRest(__VLS_1));
    __VLS_3.slots.default;
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_4 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: (page.icon),
        size: (16),
    }));
    const __VLS_5 = __VLS_4({
        name: (page.icon),
        size: (16),
    }, ...__VLS_functionalComponentArgsRest(__VLS_4));
    (page.label);
    var __VLS_3;
}
const __VLS_7 = {}.RouterView;
/** @type {[typeof __VLS_components.RouterView, ]} */ ;
// @ts-ignore
const __VLS_8 = __VLS_asFunctionalComponent(__VLS_7, new __VLS_7({}));
const __VLS_9 = __VLS_8({}, ...__VLS_functionalComponentArgsRest(__VLS_8));
/** @type {__VLS_StyleScopedClasses['app-shell']} */ ;
/** @type {__VLS_StyleScopedClasses['top-nav']} */ ;
/** @type {__VLS_StyleScopedClasses['nav-tab']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            RouterLink: RouterLink,
            RouterView: RouterView,
            FolioIcon: FolioIcon,
            pages: pages,
        };
    },
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
});
; /* PartiallyEnd: #4569/main.vue */
