import { computed, ref } from "vue";
import { formatProfitTradeRatio } from "./profit_trade_roi_format";
const props = withDefaults(defineProps(), {
    pool: "inventory",
    listingsCooling: false,
    manualExecutionDisabledReason: "",
});
const emit = defineEmits();
const depthOpen = ref(false);
const isSelection = computed(() => props.pool === "selection");
const hasSavedDepth = computed(() => {
    const snapshot = orderbook(props.row);
    return Boolean((snapshot.sellLevels?.length || 0) + (snapshot.buyLevels?.length || 0));
});
const longBuyOrder = computed(() => props.row.longBuyOrder || null);
const longBuyProposal = computed(() => props.row.longBuyProposal || null);
const excludedOwnBuyPrices = computed(() => {
    const values = props.row.excludedOwnBuyPrices
        || longBuyProposal.value?.excludedOwnBuyPrices
        || [];
    return values.filter((value) => typeof value === "number" && Number.isFinite(value));
});
const hasNonPositiveSellerRoi = computed(() => (typeof props.row.expectedRoi === "number" && props.row.expectedRoi <= 0));
const retainedByLongBuyOrder = computed(() => (!isSelection.value
    && Boolean(longBuyOrder.value)
    && hasNonPositiveSellerRoi.value));
const retainedByEligibleLongBuyProposal = computed(() => (!isSelection.value
    && !longBuyOrder.value
    && longBuyProposal.value?.eligible === true
    && hasNonPositiveSellerRoi.value));
const longBuyRetentionReason = computed(() => {
    if (retainedByLongBuyOrder.value) {
        return "当前卖盘 ROI 已不大于 0，但因仍有程序管理的长期求购而保留在观察区。";
    }
    if (retainedByEligibleLongBuyProposal.value) {
        return "当前卖盘 ROI 已不大于 0，但本轮长期求购拟建方案仍合格，故保留在观察区持续观察。";
    }
    return null;
});
function money(value) {
    return typeof value === "number" && Number.isFinite(value) ? `¥${value.toFixed(2)}` : "—";
}
function pct(value) {
    return formatProfitTradeRatio(value);
}
function time(value) {
    if (!value)
        return "—";
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime())
        ? value
        : parsed.toLocaleString("zh-CN", { hour12: false });
}
function orderbook(row) {
    return row.steamOrderbook || {
        sellerFloorPrice: row.steamBuyPrice ?? null,
        sellLevels: [],
        buyLevels: [],
    };
}
function compactListingId(value) {
    const text = String(value || "");
    return text.length > 18 ? `${text.slice(0, 8)}…${text.slice(-6)}` : text;
}
function compactOrderId(value) {
    const text = String(value || "");
    if (!text)
        return "待同步";
    return text.length > 22 ? `${text.slice(0, 10)}…${text.slice(-7)}` : text;
}
function longBuyStateLabel(value) {
    const labels = {
        creating: "创建结果核对中",
        active: "长期求购中",
        partial: "已部分成交",
        cancel_pending: "撤单终态核对中",
        filled: "已全部成交",
        auto_cancelled: "Steam 自动撤单",
        cancelled: "已安全撤单",
        terminal_uncertain: "终态不确定",
        failed: "创建失败",
    };
    return labels[value || ""] || value || "状态待同步";
}
function longBuyStateClass(value) {
    if (["active", "partial"].includes(value || ""))
        return "active";
    if (["creating", "cancel_pending"].includes(value || ""))
        return "pending";
    if (value === "terminal_uncertain")
        return "uncertain";
    return "terminal";
}
function longBuyDecisionLabel(value) {
    const labels = {
        standard_no_competitor: "无有效外部求购，按标准安全价排队",
        standard_safe_price: "标准安全价已取得足够价格优势",
        aggressive_competitor_advantage: "在激进 ROI 硬底线内取得竞争优势",
        standard_low_queue: "无法安全超过最高求购，按标准价低位排队",
    };
    return labels[value || ""] || value || "等待完整行情";
}
function listingProbeText(row) {
    const probe = row.crossedListingProbe;
    if (!probe)
        return "本次观察尚未记录具体 listing 证据";
    const listingId = compactListingId(probe.listingId);
    if (probe.status === "matched") {
        return `找到 listing ${listingId} · 买家支付 ${money(probe.listingTotal)} · 与卖一一致`;
    }
    if (probe.status === "floor_mismatch") {
        return `找到 listing ${listingId} · 实际 ${money(probe.listingTotal)} · 与卖一不一致`;
    }
    if (probe.status === "empty")
        return "未找到公开 listing · 更像幽灵卖盘或尚未传播";
    if (probe.status === "no_usable_cny_listing")
        return "有 listing 返回，但没有可验证的人民币同物品卖单";
    if (probe.status === "rate_limited")
        return "查询具体 listing 返回 429 · 未重试，已进入冷却";
    if (probe.status === "circuit_open")
        return "listings 正在冷却 · 本次没有额外请求";
    return probe.message || "本次未取得可验证的 listing 证据";
}
function listingProbeClass(row) {
    const status = row.crossedListingProbe?.status || "";
    if (status === "matched")
        return "matched";
    if (status === "floor_mismatch")
        return "mismatch";
    return "unresolved";
}
function finiteNumber(value) {
    return typeof value === "number" && Number.isFinite(value) ? value : null;
}
function c5PurchaseSellRatio(row) {
    const savedRatio = finiteNumber(row.c5PurchaseSellRatio);
    if (savedRatio !== null)
        return savedRatio;
    const purchasePrice = finiteNumber(row.c5PurchaseMaxPrice);
    const sellPrice = finiteNumber(row.c5CurrentSellPrice);
    if (purchasePrice !== null && sellPrice !== null && sellPrice > 0) {
        return purchasePrice / sellPrice;
    }
    const rawReason = row.executionReason || row.riskReason || "";
    const match = rawReason.match(/purchase\/sell ratio\s+([\d.]+)/i);
    if (!match)
        return null;
    const parsed = Number(match[1]);
    return Number.isFinite(parsed) ? parsed : null;
}
function c5MinimumPurchaseSellRatio(row) {
    const savedRatio = finiteNumber(row.c5MinPurchaseSellRatio);
    if (savedRatio !== null)
        return savedRatio;
    const rawReason = row.executionReason || row.riskReason || "";
    const match = rawReason.match(/purchase\/sell ratio\s+[\d.]+\s*<\s*([\d.]+)/i);
    if (!match)
        return null;
    const parsed = Number(match[1]);
    return Number.isFinite(parsed) ? parsed : null;
}
function c5PurchaseGapExplanation(row) {
    const rawReason = row.executionReason || row.riskReason || "";
    if (row.riskStatus !== "blocked_c5_purchase_price_gap"
        && !/purchase\/sell ratio/i.test(rawReason))
        return null;
    const purchasePrice = finiteNumber(row.c5PurchaseMaxPrice);
    const sellPrice = finiteNumber(row.c5CurrentSellPrice);
    const actualRatio = c5PurchaseSellRatio(row);
    const minimumRatio = c5MinimumPurchaseSellRatio(row);
    const formula = purchasePrice !== null && sellPrice !== null && actualRatio !== null
        ? `最高求购 ${money(purchasePrice)} ÷ 当前在售价 ${money(sellPrice)} = ${pct(actualRatio)}`
        : actualRatio !== null
            ? `C5 最高求购 / 当前在售价 = ${pct(actualRatio)}`
            : "C5 最高求购价与当前在售价的比例未达到要求";
    const threshold = minimumRatio !== null ? pct(minimumRatio) : "系统最低要求";
    return {
        formula,
        detail: `低于最低要求 ${threshold}，买卖价差过大，当前求购承接不足。`,
        tooltip: `C5 求购承接不足：${formula}，低于最低要求 ${threshold}`,
    };
}
const c5RiskExplanation = computed(() => c5PurchaseGapExplanation(props.row));
const generalReason = computed(() => {
    if (c5RiskExplanation.value)
        return "";
    return props.row.executionReason || props.row.riskReason || props.row.exitReason || props.row.lastError || "";
});
const displayedManualExecutionDisabledReason = computed(() => {
    const disabledReason = props.manualExecutionDisabledReason;
    if (!c5RiskExplanation.value)
        return disabledReason;
    if (!disabledReason
        || disabledReason === props.row.executionReason
        || disabledReason === props.row.riskReason
        || /purchase\/sell ratio/i.test(disabledReason))
        return c5RiskExplanation.value.tooltip;
    return disabledReason;
});
function stateLabel(row) {
    if (isSelection.value) {
        const labels = {
            pending_first_scan: "等待首次观察",
            observed: "已观察",
            price_unavailable: "暂时无价格",
            scan_failed: "读取失败",
            paused: "已暂停",
            removed: "已移出",
        };
        return labels[row.status || ""] || "仅研究";
    }
    if (row.active === false)
        return "已退出观察池";
    if (props.listingsCooling
        && ["listings_cooldown", "listings_probe_ready", "executable", "eligible"].includes(row.executionStatus || ""))
        return "达到门槛 · listings 冷却";
    const labels = {
        listings_cooldown: "达到执行门槛",
        listings_probe_ready: "达到执行门槛",
        executable: "达到执行门槛",
        eligible: "达到执行门槛",
        observe_only: "仅观察，不执行",
        blocked: "风控阻断",
        manual_review: "异常 ROI，需人工",
        below_min_roi: "低于自动门槛，可人工执行",
        below_min_item_value: "低于最低商品价值 · 仅维护已有长期求购",
        asset_unavailable: "暂无可执行资产",
        exited: "已退出观察池",
    };
    const status = row.executionStatusCode || row.executionStatus || "observe_only";
    return labels[status] || row.executionStatus || "仅观察，不执行";
}
function stateClass(row) {
    if (isSelection.value) {
        if (["scan_failed", "price_unavailable"].includes(row.status || ""))
            return "blocked";
        if (["paused", "removed"].includes(row.status || ""))
            return "exited";
        if (row.status === "pending_first_scan")
            return "observe";
        return "ready";
    }
    if (row.active === false)
        return "exited";
    if (props.listingsCooling
        && ["listings_cooldown", "listings_probe_ready", "executable", "eligible"].includes(row.executionStatus || ""))
        return "cooldown";
    if (["listings_cooldown", "listings_probe_ready", "executable", "eligible"].includes(row.executionStatus || ""))
        return "ready";
    if (["blocked", "manual_review"].includes(row.executionStatus || ""))
        return "blocked";
    return "observe";
}
function referenceHint(row) {
    const labels = {
        missing_buy_book: "暂无有效最高求购参考",
        currency_invalid: "求购参考币种不可用",
        crossed_possible_stale: "求购参考盘口交叉，可能滞后",
        c5_price_unavailable: "C5 价格暂不可用",
        snapshot_expired: "求购参考快照已过期",
    };
    return labels[row.buyOrderReferenceStatus || ""] || null;
}
function tradeStateLabel(status) {
    const labels = {
        completed: "已完成",
        c5_listed: "已买入，C5 在售",
        listing_c5: "已买入，正在 C5 上架",
        steam_bought: "已买入，准备 C5 上架",
        buying: "正在买入",
        locked: "执行中，已锁定 A",
        audited: "已转化为流水，执行中",
        candidate: "已转化为流水，执行中",
        manual_required: "执行中断，需人工处理",
        failed: "执行失败",
        cancelled: "已取消",
    };
    return labels[status || ""] || status || "执行状态待同步";
}
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_withDefaultsArg = (function (t) { return t; })({
    pool: "inventory",
    listingsCooling: false,
    manualExecutionDisabledReason: "",
});
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
/** @type {__VLS_StyleScopedClasses['card-head']} */ ;
/** @type {__VLS_StyleScopedClasses['card-head']} */ ;
/** @type {__VLS_StyleScopedClasses['card-head']} */ ;
/** @type {__VLS_StyleScopedClasses['card-head']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-state']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-state']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-state']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-state']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-state']} */ ;
/** @type {__VLS_StyleScopedClasses['card-cooldown']} */ ;
/** @type {__VLS_StyleScopedClasses['price-line']} */ ;
/** @type {__VLS_StyleScopedClasses['price-line']} */ ;
/** @type {__VLS_StyleScopedClasses['price-line']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['price-line']} */ ;
/** @type {__VLS_StyleScopedClasses['listing-probe']} */ ;
/** @type {__VLS_StyleScopedClasses['listing-probe']} */ ;
/** @type {__VLS_StyleScopedClasses['listing-probe']} */ ;
/** @type {__VLS_StyleScopedClasses['roi-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['roi-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['roi-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['roi-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['buy-reference']} */ ;
/** @type {__VLS_StyleScopedClasses['long-buy-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['long-buy-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['long-buy-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['long-buy-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['long-buy-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['long-buy-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['long-buy-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['long-buy-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['long-buy-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['long-buy-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['long-buy-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['long-buy-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['own-price-exclusion']} */ ;
/** @type {__VLS_StyleScopedClasses['long-buy-decision']} */ ;
/** @type {__VLS_StyleScopedClasses['long-buy-warning']} */ ;
/** @type {__VLS_StyleScopedClasses['long-buy-observe-note']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['c5-risk-reason']} */ ;
/** @type {__VLS_StyleScopedClasses['c5-risk-reason']} */ ;
/** @type {__VLS_StyleScopedClasses['c5-risk-reason']} */ ;
/** @type {__VLS_StyleScopedClasses['linked-trade']} */ ;
/** @type {__VLS_StyleScopedClasses['linked-trade']} */ ;
/** @type {__VLS_StyleScopedClasses['linked-trade']} */ ;
/** @type {__VLS_StyleScopedClasses['linked-trade']} */ ;
/** @type {__VLS_StyleScopedClasses['linked-trade']} */ ;
/** @type {__VLS_StyleScopedClasses['linked-trade']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-execute-action']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-execute-action']} */ ;
/** @type {__VLS_StyleScopedClasses['depth-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['depth-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['depth-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['depth-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['depth-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['depth-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['depth-empty']} */ ;
/** @type {__VLS_StyleScopedClasses['price-line']} */ ;
/** @type {__VLS_StyleScopedClasses['price-line']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['depth-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['roi-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['long-buy-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['long-buy-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['long-buy-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-state']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-execute-action']} */ ;
// CSS variable injection 
// CSS variable injection end 
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: (['watch-card', { 'selection-card': __VLS_ctx.isSelection }]) },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "card-head" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.row.name || __VLS_ctx.row.marketHashName);
if (__VLS_ctx.row.name && __VLS_ctx.row.name !== __VLS_ctx.row.marketHashName) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (__VLS_ctx.row.marketHashName);
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: (['watch-state', __VLS_ctx.stateClass(__VLS_ctx.row)]) },
});
(__VLS_ctx.stateLabel(__VLS_ctx.row));
if (__VLS_ctx.isSelection) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "research-note" },
    });
}
else if (__VLS_ctx.listingsCooling && ['listings_cooldown', 'listings_probe_ready', 'executable', 'eligible'].includes(__VLS_ctx.row.executionStatus || '')) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "card-cooldown" },
    });
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "price-line" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
(__VLS_ctx.money(__VLS_ctx.orderbook(__VLS_ctx.row).sellerFloorPrice ?? __VLS_ctx.row.steamBuyPrice));
__VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({
    ...{ class: "steam-highest-bid" },
});
(__VLS_ctx.money(__VLS_ctx.orderbook(__VLS_ctx.row).buyerMaxPrice));
if (__VLS_ctx.orderbook(__VLS_ctx.row).buyerMaxCount != null) {
    (__VLS_ctx.orderbook(__VLS_ctx.row).buyerMaxCount);
}
if (__VLS_ctx.orderbook(__VLS_ctx.row).crossed) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "crossed-badge" },
    });
}
if (__VLS_ctx.orderbook(__VLS_ctx.row).crossed) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: (['listing-probe', __VLS_ctx.listingProbeClass(__VLS_ctx.row)]) },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.listingProbeText(__VLS_ctx.row));
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
(__VLS_ctx.money(__VLS_ctx.row.c5ListingPrice));
__VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
(__VLS_ctx.money(__VLS_ctx.row.c5ExpectedNetPrice));
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "roi-summary" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.pct(__VLS_ctx.row.expectedRoi));
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
(__VLS_ctx.money(__VLS_ctx.row.expectedProfit));
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "buy-reference" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.pct(__VLS_ctx.row.buyOrderReferenceRoi));
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
(__VLS_ctx.money(__VLS_ctx.row.buyOrderReferenceProfit));
if (__VLS_ctx.referenceHint(__VLS_ctx.row)) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "reference-hint" },
    });
    (__VLS_ctx.referenceHint(__VLS_ctx.row));
}
if (__VLS_ctx.longBuyRetentionReason) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "long-buy-retained" },
    });
    (__VLS_ctx.longBuyRetentionReason);
}
if (__VLS_ctx.longBuyOrder && !__VLS_ctx.isSelection) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: (['long-buy-panel', `is-${__VLS_ctx.longBuyStateClass(__VLS_ctx.longBuyOrder.state)}`]) },
        'aria-label': "程序管理的 Steam 长期求购",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.longBuyStateLabel(__VLS_ctx.longBuyOrder.state));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.money(__VLS_ctx.longBuyOrder.bidPrice));
    (__VLS_ctx.longBuyOrder.remainingQuantity ?? 0);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "long-buy-grid" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.money(__VLS_ctx.longBuyOrder.bidPrice));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (__VLS_ctx.pct(__VLS_ctx.longBuyOrder.worstCaseRoi));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.money(__VLS_ctx.row.competitorBuyPrice ?? __VLS_ctx.longBuyProposal?.competitorBuyPrice));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (__VLS_ctx.pct(__VLS_ctx.row.competitorBuyRoi ?? __VLS_ctx.longBuyProposal?.competitorBuyRoi));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.longBuyOrder.filledQuantity ?? 0);
    (__VLS_ctx.longBuyOrder.quantity ?? "—");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (__VLS_ctx.longBuyOrder.remainingQuantity ?? "—");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.money(__VLS_ctx.longBuyOrder.standardSafePrice));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (__VLS_ctx.money(__VLS_ctx.longBuyOrder.aggressiveSafePrice));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "long-buy-meta" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.longBuyOrder.accountId || "—");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.compactOrderId(__VLS_ctx.longBuyOrder.buyOrderId));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.time(__VLS_ctx.longBuyOrder.createdAt));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.time(__VLS_ctx.longBuyOrder.lastCheckedAt));
    if (__VLS_ctx.excludedOwnBuyPrices.length) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
            ...{ class: "own-price-exclusion" },
        });
        (__VLS_ctx.excludedOwnBuyPrices.map(__VLS_ctx.money).join("、"));
    }
    if (__VLS_ctx.longBuyOrder.reason) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
            ...{ class: "long-buy-warning" },
        });
        (__VLS_ctx.longBuyOrder.reason);
    }
}
else if (__VLS_ctx.longBuyProposal && !__VLS_ctx.isSelection) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "long-buy-panel is-proposal" },
        'aria-label': "长期求购观察模式提案",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.longBuyProposal.eligible ? "本轮拟建方案" : "本轮不创建");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.money(__VLS_ctx.longBuyProposal.targetPrice));
    (__VLS_ctx.longBuyProposal.quantity ?? "—");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "long-buy-grid" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.money(__VLS_ctx.longBuyProposal.targetPrice));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (__VLS_ctx.pct(__VLS_ctx.longBuyProposal.worstCaseRoi));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.money(__VLS_ctx.longBuyProposal.competitorBuyPrice));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (__VLS_ctx.pct(__VLS_ctx.longBuyProposal.competitorBuyRoi));
    (__VLS_ctx.money(__VLS_ctx.longBuyProposal.competitorBuyProfit));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.money(__VLS_ctx.longBuyProposal.standardSafePrice));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (__VLS_ctx.pct(__VLS_ctx.longBuyProposal.standardRoi));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.money(__VLS_ctx.longBuyProposal.aggressiveSafePrice));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (__VLS_ctx.pct(__VLS_ctx.longBuyProposal.aggressiveRoi));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "long-buy-decision" },
    });
    (__VLS_ctx.longBuyDecisionLabel(__VLS_ctx.longBuyProposal.decision));
    if (__VLS_ctx.excludedOwnBuyPrices.length) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
            ...{ class: "own-price-exclusion" },
        });
        (__VLS_ctx.excludedOwnBuyPrices.map(__VLS_ctx.money).join("、"));
    }
    if (__VLS_ctx.longBuyProposal.blockedReason) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
            ...{ class: "long-buy-warning" },
        });
        (__VLS_ctx.longBuyProposal.blockedReason);
    }
    else if (!__VLS_ctx.longBuyProposal.executionAllowed) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
            ...{ class: "long-buy-observe-note" },
        });
    }
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "watch-metrics" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
(__VLS_ctx.pct(__VLS_ctx.row.roiBasis ?? __VLS_ctx.row.balanceDiscount));
if (!__VLS_ctx.isSelection) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.row.tradableCount ?? "—");
    (__VLS_ctx.row.inventoryCount ?? "—");
}
else {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.stateLabel(__VLS_ctx.row));
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
(__VLS_ctx.time(__VLS_ctx.row.lastObservedAt));
if (__VLS_ctx.c5RiskExplanation) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "c5-risk-reason" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.c5RiskExplanation.formula);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (__VLS_ctx.c5RiskExplanation.detail);
}
else if (__VLS_ctx.generalReason) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "reason" },
    });
    (__VLS_ctx.generalReason);
}
if (__VLS_ctx.row.latestTrade && !__VLS_ctx.isSelection) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "linked-trade" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.tradeStateLabel(__VLS_ctx.row.latestTrade.status));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (__VLS_ctx.row.latestTrade.tradeNo || "流水号同步中");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
    (__VLS_ctx.money(__VLS_ctx.row.latestTrade.steamBuyPrice));
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "card-actions" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.depthOpen = !__VLS_ctx.depthOpen;
        } },
    ...{ class: "link-action" },
    type: "button",
});
(__VLS_ctx.depthOpen ? "收起买卖前 5 档" : "查看买卖前 5 档");
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.emit('open-history', __VLS_ctx.row);
        } },
    ...{ class: "link-action" },
    type: "button",
});
if (__VLS_ctx.isSelection) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.isSelection))
                    return;
                __VLS_ctx.emit('remove-selection', __VLS_ctx.row);
            } },
        ...{ class: "link-action remove-action" },
        type: "button",
    });
}
else {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!!(__VLS_ctx.isSelection))
                    return;
                __VLS_ctx.emit('manual-execute', __VLS_ctx.row);
            } },
        ...{ class: "manual-execute-action" },
        type: "button",
        disabled: (Boolean(__VLS_ctx.displayedManualExecutionDisabledReason)),
        title: (__VLS_ctx.displayedManualExecutionDisabledReason || '选择数量并再次确认后执行'),
    });
}
if (__VLS_ctx.depthOpen) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "depth-panel" },
        'aria-label': "最新 Steam 买卖前五档",
    });
    if (!__VLS_ctx.hasSavedDepth) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
            ...{ class: "depth-empty" },
        });
    }
    else {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        for (const [level, levelIndex] of __VLS_getVForSourceType((__VLS_ctx.orderbook(__VLS_ctx.row).sellLevels || []))) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
                key: (`sell-${levelIndex}`),
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
            (levelIndex + 1);
            __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
            (__VLS_ctx.money(level.price));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
            (level.count ?? "—");
        }
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        for (const [level, levelIndex] of __VLS_getVForSourceType((__VLS_ctx.orderbook(__VLS_ctx.row).buyLevels || []))) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
                key: (`buy-${levelIndex}`),
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
            (levelIndex + 1);
            __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
            (__VLS_ctx.money(level.price));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
            (level.count ?? "—");
        }
    }
}
/** @type {__VLS_StyleScopedClasses['card-head']} */ ;
/** @type {__VLS_StyleScopedClasses['research-note']} */ ;
/** @type {__VLS_StyleScopedClasses['card-cooldown']} */ ;
/** @type {__VLS_StyleScopedClasses['price-line']} */ ;
/** @type {__VLS_StyleScopedClasses['steam-highest-bid']} */ ;
/** @type {__VLS_StyleScopedClasses['crossed-badge']} */ ;
/** @type {__VLS_StyleScopedClasses['roi-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['buy-reference']} */ ;
/** @type {__VLS_StyleScopedClasses['reference-hint']} */ ;
/** @type {__VLS_StyleScopedClasses['long-buy-retained']} */ ;
/** @type {__VLS_StyleScopedClasses['long-buy-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['long-buy-meta']} */ ;
/** @type {__VLS_StyleScopedClasses['own-price-exclusion']} */ ;
/** @type {__VLS_StyleScopedClasses['long-buy-warning']} */ ;
/** @type {__VLS_StyleScopedClasses['long-buy-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['is-proposal']} */ ;
/** @type {__VLS_StyleScopedClasses['long-buy-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['long-buy-decision']} */ ;
/** @type {__VLS_StyleScopedClasses['own-price-exclusion']} */ ;
/** @type {__VLS_StyleScopedClasses['long-buy-warning']} */ ;
/** @type {__VLS_StyleScopedClasses['long-buy-observe-note']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['c5-risk-reason']} */ ;
/** @type {__VLS_StyleScopedClasses['reason']} */ ;
/** @type {__VLS_StyleScopedClasses['linked-trade']} */ ;
/** @type {__VLS_StyleScopedClasses['card-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['link-action']} */ ;
/** @type {__VLS_StyleScopedClasses['link-action']} */ ;
/** @type {__VLS_StyleScopedClasses['link-action']} */ ;
/** @type {__VLS_StyleScopedClasses['remove-action']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-execute-action']} */ ;
/** @type {__VLS_StyleScopedClasses['depth-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['depth-empty']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            emit: emit,
            depthOpen: depthOpen,
            isSelection: isSelection,
            hasSavedDepth: hasSavedDepth,
            longBuyOrder: longBuyOrder,
            longBuyProposal: longBuyProposal,
            excludedOwnBuyPrices: excludedOwnBuyPrices,
            longBuyRetentionReason: longBuyRetentionReason,
            money: money,
            pct: pct,
            time: time,
            orderbook: orderbook,
            compactOrderId: compactOrderId,
            longBuyStateLabel: longBuyStateLabel,
            longBuyStateClass: longBuyStateClass,
            longBuyDecisionLabel: longBuyDecisionLabel,
            listingProbeText: listingProbeText,
            listingProbeClass: listingProbeClass,
            c5RiskExplanation: c5RiskExplanation,
            generalReason: generalReason,
            displayedManualExecutionDisabledReason: displayedManualExecutionDisabledReason,
            stateLabel: stateLabel,
            stateClass: stateClass,
            referenceHint: referenceHint,
            tradeStateLabel: tradeStateLabel,
        };
    },
    __typeEmits: {},
    __typeProps: {},
    props: {},
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
    __typeEmits: {},
    __typeProps: {},
    props: {},
});
; /* PartiallyEnd: #4569/main.vue */
