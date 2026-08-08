export function formatRatio(value) {
    if (value === null || value === undefined || Number.isNaN(value))
        return "-";
    return value.toFixed(4);
}
export function formatInteger(value) {
    if (value === null || value === undefined || Number.isNaN(value))
        return "-";
    return Math.round(value).toLocaleString("zh-CN");
}
export function formatClock(value) {
    if (!value)
        return "--:--:--";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime()))
        return "--:--:--";
    return parsed.toLocaleTimeString("zh-CN", {
        hour12: false,
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
    });
}
export function formatDuration(totalSeconds) {
    const seconds = Math.max(0, Math.floor(totalSeconds));
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    if (days > 0)
        return `${days}天 ${hours}小时`;
    if (hours > 0)
        return `${hours}小时 ${minutes}分`;
    return `${minutes}分钟`;
}
export function recommendedRatio(item) {
    return item.effectiveRecommendedMaxListingRatio ?? item.recommendedMaxListingRatio;
}
export function speedLabel(item) {
    const volume = item.steamVolume24h ?? 0;
    if (volume >= 500)
        return "快";
    if (volume >= 100)
        return "中";
    if (volume > 0)
        return "慢";
    return item.liquidityLabel || "-";
}
export function stabilityLabel(item) {
    const deviation = Number(item.stddevRatio || 0);
    if (deviation <= 0.012)
        return "高";
    if (deviation <= 0.035)
        return "中";
    return "低";
}
export function stabilityTone(item) {
    const label = stabilityLabel(item);
    if (label === "高")
        return "high";
    if (label === "中")
        return "medium";
    return "low";
}
