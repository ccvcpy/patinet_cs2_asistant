import FolioIcon from "./FolioIcon.vue";
const rules = [
    {
        id: "existing-unmatched",
        condition: "有旧长期单 + 未成交",
        conditionIcon: "calendar",
        outcome: "冻结：不撤、不改、不新建、不直购",
        outcomeIcon: "lock",
        tone: "freeze",
    },
    {
        id: "existing-matched",
        condition: "有旧长期单 + 已成交",
        conditionIcon: "calendar",
        outcome: "锁老库存 A → C5 上架",
        outcomeIcon: "lock",
        tone: "complete",
    },
    {
        id: "no-existing",
        condition: "无旧长期单",
        conditionIcon: "circle-dashed",
        outcome: "原直购链路",
        outcomeIcon: "rocket",
        tone: "purchase",
    },
];
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
/** @type {__VLS_StyleScopedClasses['crossed-book-rule']} */ ;
/** @type {__VLS_StyleScopedClasses['crossed-book-rule']} */ ;
/** @type {__VLS_StyleScopedClasses['crossed-book-rule']} */ ;
/** @type {__VLS_StyleScopedClasses['crossed-book-rule']} */ ;
/** @type {__VLS_StyleScopedClasses['crossed-book-rule']} */ ;
/** @type {__VLS_StyleScopedClasses['rule-node']} */ ;
/** @type {__VLS_StyleScopedClasses['rule-node']} */ ;
/** @type {__VLS_StyleScopedClasses['rule-connector']} */ ;
/** @type {__VLS_StyleScopedClasses['rule-connector']} */ ;
/** @type {__VLS_StyleScopedClasses['freeze']} */ ;
/** @type {__VLS_StyleScopedClasses['outcome']} */ ;
/** @type {__VLS_StyleScopedClasses['outcome']} */ ;
/** @type {__VLS_StyleScopedClasses['outcome']} */ ;
/** @type {__VLS_StyleScopedClasses['purchase']} */ ;
/** @type {__VLS_StyleScopedClasses['purchase']} */ ;
/** @type {__VLS_StyleScopedClasses['condition']} */ ;
/** @type {__VLS_StyleScopedClasses['crossed-book-rule']} */ ;
/** @type {__VLS_StyleScopedClasses['crossed-book-rule']} */ ;
/** @type {__VLS_StyleScopedClasses['rule-connector']} */ ;
// CSS variable injection 
// CSS variable injection end 
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "crossed-book-shell" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "atomic-component-label" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "crossed-book-rule" },
    'data-testid': "long-buy-crossed-book-rule",
    'aria-label': "交叉盘口安全护栏",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h3, __VLS_intrinsicElements.h3)({});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_0 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "shield",
    size: (18),
}));
const __VLS_1 = __VLS_0({
    name: "shield",
    size: (18),
}, ...__VLS_functionalComponentArgsRest(__VLS_0));
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "safety-badge" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.ol, __VLS_intrinsicElements.ol)({});
for (const [rule] of __VLS_getVForSourceType((__VLS_ctx.rules))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.li, __VLS_intrinsicElements.li)({
        key: (rule.id),
        ...{ class: (rule.tone) },
        'data-testid': (`crossed-book-${rule.id}`),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "rule-node condition" },
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_3 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: (rule.conditionIcon),
        size: (15),
    }));
    const __VLS_4 = __VLS_3({
        name: (rule.conditionIcon),
        size: (15),
    }, ...__VLS_functionalComponentArgsRest(__VLS_3));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (rule.condition);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "rule-connector" },
        'aria-hidden': "true",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "rule-node outcome" },
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_6 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: (rule.outcomeIcon),
        size: (15),
    }));
    const __VLS_7 = __VLS_6({
        name: (rule.outcomeIcon),
        size: (15),
    }, ...__VLS_functionalComponentArgsRest(__VLS_6));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (rule.outcome);
}
/** @type {__VLS_StyleScopedClasses['crossed-book-shell']} */ ;
/** @type {__VLS_StyleScopedClasses['atomic-component-label']} */ ;
/** @type {__VLS_StyleScopedClasses['crossed-book-rule']} */ ;
/** @type {__VLS_StyleScopedClasses['safety-badge']} */ ;
/** @type {__VLS_StyleScopedClasses['rule-node']} */ ;
/** @type {__VLS_StyleScopedClasses['condition']} */ ;
/** @type {__VLS_StyleScopedClasses['rule-connector']} */ ;
/** @type {__VLS_StyleScopedClasses['rule-node']} */ ;
/** @type {__VLS_StyleScopedClasses['outcome']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            FolioIcon: FolioIcon,
            rules: rules,
        };
    },
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
});
; /* PartiallyEnd: #4569/main.vue */
