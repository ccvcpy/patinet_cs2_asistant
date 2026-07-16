import { RouterLink, RouterView } from "vue-router";
import FolioIcon from "../components/FolioIcon.vue";
const links = [
    { to: "/guadao/overview", label: "运行总览", icon: "scan" },
    { to: "/guadao/operations", label: "流水状态", icon: "report" },
    { to: "/guadao/issues", label: "异常与待处理", icon: "warning" },
    { to: "/guadao/logs", label: "实时日志", icon: "clock" },
    { to: "/guadao/settings", label: "策略设置", icon: "settings" },
    { to: "/guadao/report", label: "挂刀报表", icon: "wallet" },
];
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
/** @type {__VLS_StyleScopedClasses['guadao-subnav']} */ ;
/** @type {__VLS_StyleScopedClasses['guadao-subnav']} */ ;
/** @type {__VLS_StyleScopedClasses['guadao-subnav']} */ ;
/** @type {__VLS_StyleScopedClasses['guadao-subnav']} */ ;
/** @type {__VLS_StyleScopedClasses['guadao-subnav']} */ ;
/** @type {__VLS_StyleScopedClasses['router-link-active']} */ ;
// CSS variable injection 
// CSS variable injection end 
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "guadao-workspace" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.nav, __VLS_intrinsicElements.nav)({
    ...{ class: "guadao-subnav" },
    'aria-label': "挂刀运营页面",
});
for (const [link] of __VLS_getVForSourceType((__VLS_ctx.links))) {
    const __VLS_0 = {}.RouterLink;
    /** @type {[typeof __VLS_components.RouterLink, typeof __VLS_components.RouterLink, ]} */ ;
    // @ts-ignore
    const __VLS_1 = __VLS_asFunctionalComponent(__VLS_0, new __VLS_0({
        key: (link.to),
        to: (link.to),
    }));
    const __VLS_2 = __VLS_1({
        key: (link.to),
        to: (link.to),
    }, ...__VLS_functionalComponentArgsRest(__VLS_1));
    __VLS_3.slots.default;
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_4 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: (link.icon),
        size: (15),
    }));
    const __VLS_5 = __VLS_4({
        name: (link.icon),
        size: (15),
    }, ...__VLS_functionalComponentArgsRest(__VLS_4));
    (link.label);
    var __VLS_3;
}
const __VLS_7 = {}.RouterView;
/** @type {[typeof __VLS_components.RouterView, typeof __VLS_components.RouterView, ]} */ ;
// @ts-ignore
const __VLS_8 = __VLS_asFunctionalComponent(__VLS_7, new __VLS_7({}));
const __VLS_9 = __VLS_8({}, ...__VLS_functionalComponentArgsRest(__VLS_8));
{
    const { default: __VLS_thisSlot } = __VLS_10.slots;
    const [{ Component }] = __VLS_getSlotParams(__VLS_thisSlot);
    const __VLS_11 = {}.KeepAlive;
    /** @type {[typeof __VLS_components.KeepAlive, typeof __VLS_components.KeepAlive, ]} */ ;
    // @ts-ignore
    const __VLS_12 = __VLS_asFunctionalComponent(__VLS_11, new __VLS_11({}));
    const __VLS_13 = __VLS_12({}, ...__VLS_functionalComponentArgsRest(__VLS_12));
    __VLS_14.slots.default;
    const __VLS_15 = ((Component));
    // @ts-ignore
    const __VLS_16 = __VLS_asFunctionalComponent(__VLS_15, new __VLS_15({}));
    const __VLS_17 = __VLS_16({}, ...__VLS_functionalComponentArgsRest(__VLS_16));
    var __VLS_14;
    __VLS_10.slots['' /* empty slot name completion */];
}
var __VLS_10;
/** @type {__VLS_StyleScopedClasses['guadao-workspace']} */ ;
/** @type {__VLS_StyleScopedClasses['guadao-subnav']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            RouterLink: RouterLink,
            RouterView: RouterView,
            FolioIcon: FolioIcon,
            links: links,
        };
    },
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
});
; /* PartiallyEnd: #4569/main.vue */
