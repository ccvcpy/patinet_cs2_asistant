import { computed } from "vue";
import { RouterLink, RouterView, useRoute } from "vue-router";
import FolioIcon from "./components/FolioIcon.vue";
const route = useRoute();
const isCaseMonitorAtoms = computed(() => route.path === "/case-ratio/components");
const operationsTheme = computed(() => !route.path.startsWith("/guadao") && !route.path.startsWith("/profit-trade"));
const pages = [
    { to: "/account", label: "挂刀执行-测试工具", icon: "account" },
    { to: "/steam", label: "Steam余额统计", icon: "wallet" },
    { to: "/guadao/overview", match: "/guadao", label: "挂刀运营", icon: "report" },
    { to: "/case-ratio", label: "箱子挂刀比", icon: "case" },
    { to: "/profit-trade/overview", match: "/profit-trade", label: "搬砖做T", icon: "scan" },
    { to: "/c5-t-monitor", label: "C5 扫描 & 库存运营", icon: "scan" },
    { to: "/c5-sweeper", label: "C5扫货", icon: "price" },
];
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
/** @type {__VLS_StyleScopedClasses['top-nav']} */ ;
/** @type {__VLS_StyleScopedClasses['top-nav--unified']} */ ;
/** @type {__VLS_StyleScopedClasses['top-nav--unified']} */ ;
/** @type {__VLS_StyleScopedClasses['top-nav--unified']} */ ;
/** @type {__VLS_StyleScopedClasses['top-nav--unified']} */ ;
/** @type {__VLS_StyleScopedClasses['top-nav--unified']} */ ;
/** @type {__VLS_StyleScopedClasses['nav-tab']} */ ;
/** @type {__VLS_StyleScopedClasses['top-nav--unified']} */ ;
/** @type {__VLS_StyleScopedClasses['nav-tab']} */ ;
/** @type {__VLS_StyleScopedClasses['top-nav--unified']} */ ;
/** @type {__VLS_StyleScopedClasses['nav-tab']} */ ;
/** @type {__VLS_StyleScopedClasses['active']} */ ;
/** @type {__VLS_StyleScopedClasses['top-nav--unified']} */ ;
/** @type {__VLS_StyleScopedClasses['nav-brand']} */ ;
/** @type {__VLS_StyleScopedClasses['top-nav--unified']} */ ;
/** @type {__VLS_StyleScopedClasses['nav-brand']} */ ;
// CSS variable injection 
// CSS variable injection end 
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "app-shell" },
    ...{ class: ({ 'app-shell--minimal-v2': __VLS_ctx.operationsTheme }) },
});
if (!__VLS_ctx.isCaseMonitorAtoms) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.nav, __VLS_intrinsicElements.nav)({
        ...{ class: "top-nav top-nav--unified" },
        'aria-label': "主导航",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "nav-brand" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "nav-brand-mark" },
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_0 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: "scan",
        size: (15),
    }));
    const __VLS_1 = __VLS_0({
        name: "scan",
        size: (15),
    }, ...__VLS_functionalComponentArgsRest(__VLS_0));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    for (const [page] of __VLS_getVForSourceType((__VLS_ctx.pages))) {
        const __VLS_3 = {}.RouterLink;
        /** @type {[typeof __VLS_components.RouterLink, typeof __VLS_components.RouterLink, ]} */ ;
        // @ts-ignore
        const __VLS_4 = __VLS_asFunctionalComponent(__VLS_3, new __VLS_3({
            key: (page.to),
            to: (page.to),
            ...{ class: "nav-tab" },
            ...{ class: ({ active: page.match ? __VLS_ctx.$route.path.startsWith(page.match) : __VLS_ctx.$route.path === page.to }) },
        }));
        const __VLS_5 = __VLS_4({
            key: (page.to),
            to: (page.to),
            ...{ class: "nav-tab" },
            ...{ class: ({ active: page.match ? __VLS_ctx.$route.path.startsWith(page.match) : __VLS_ctx.$route.path === page.to }) },
        }, ...__VLS_functionalComponentArgsRest(__VLS_4));
        __VLS_6.slots.default;
        /** @type {[typeof FolioIcon, ]} */ ;
        // @ts-ignore
        const __VLS_7 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
            name: (page.icon),
            size: (16),
        }));
        const __VLS_8 = __VLS_7({
            name: (page.icon),
            size: (16),
        }, ...__VLS_functionalComponentArgsRest(__VLS_7));
        (page.label);
        var __VLS_6;
    }
}
const __VLS_10 = {}.RouterView;
/** @type {[typeof __VLS_components.RouterView, ]} */ ;
// @ts-ignore
const __VLS_11 = __VLS_asFunctionalComponent(__VLS_10, new __VLS_10({}));
const __VLS_12 = __VLS_11({}, ...__VLS_functionalComponentArgsRest(__VLS_11));
/** @type {__VLS_StyleScopedClasses['app-shell']} */ ;
/** @type {__VLS_StyleScopedClasses['top-nav']} */ ;
/** @type {__VLS_StyleScopedClasses['top-nav--unified']} */ ;
/** @type {__VLS_StyleScopedClasses['nav-brand']} */ ;
/** @type {__VLS_StyleScopedClasses['nav-brand-mark']} */ ;
/** @type {__VLS_StyleScopedClasses['nav-tab']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            RouterLink: RouterLink,
            RouterView: RouterView,
            FolioIcon: FolioIcon,
            isCaseMonitorAtoms: isCaseMonitorAtoms,
            operationsTheme: operationsTheme,
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
