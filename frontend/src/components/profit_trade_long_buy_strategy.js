export function requiresLongBuyConfigConfirmation(key) {
    return key === "longBuyAllowRealExecution";
}
/** The total switch owns both the persistent config and the runtime schedule. */
export function usesProfitTradeRuntimeToggle(key) {
    return key === "enabled";
}
/**
 * Keep the UI permission summary aligned with the backend long-buy write guard:
 * every one of the four flags must be true before the UI may call it "live".
 */
export function resolveProfitTradeLongBuyStrategyState(config) {
    const canObserve = config.enabled && config.longBuyEnabled;
    // Existing remote orders are still reconciled while Profit Trade is on,
    // even if new long-buy strategy work is disabled.
    const canReconcileExistingOrders = config.enabled;
    const canWriteSteam = canObserve
        && config.allowRealExecution
        && config.longBuyAllowRealExecution;
    const canExecuteC5Followup = canObserve && config.allowRealExecution;
    if (!config.enabled) {
        return {
            mode: "disabled",
            canObserve: false,
            canReconcileExistingOrders: false,
            canWriteSteam: false,
            canExecuteC5Followup: false,
            label: "已停止",
            detail: "Profit Trade 总功能已关闭，长期求购不会运行。",
        };
    }
    if (!config.longBuyEnabled) {
        return {
            mode: "disabled",
            canObserve: false,
            canReconcileExistingOrders,
            canWriteSteam: false,
            canExecuteC5Followup: false,
            label: "未启用",
            detail: "不产生新方案或 Steam 写入；已有长期求购仍安全核对官方成交。",
        };
    }
    if (canWriteSteam) {
        return {
            mode: "live",
            canObserve: true,
            canReconcileExistingOrders,
            canWriteSteam: true,
            canExecuteC5Followup,
            label: "真实写入已开放",
            detail: "满足风控时，可安全创建、撤销和重建 Steam 长期求购。",
        };
    }
    if (!config.allowRealExecution) {
        return {
            mode: "observe",
            canObserve: true,
            canReconcileExistingOrders,
            canWriteSteam: false,
            canExecuteC5Followup,
            label: "观察模式",
            detail: "普通真实执行未开放；会计算方案和核对成交，但不会写 Steam 求购。",
        };
    }
    return {
        mode: "observe",
        canObserve: true,
        canReconcileExistingOrders,
        canWriteSteam: false,
        canExecuteC5Followup,
        label: "观察模式",
        detail: "会计算方案和核对成交；不会创建、撤销或改价 Steam 求购。",
    };
}
