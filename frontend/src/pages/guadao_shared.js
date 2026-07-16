export function formatLocal(value) {
    if (!value)
        return "—";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}
export function formatCountdown(value) {
    if (!value)
        return "—";
    const seconds = Math.max(0, Math.ceil((new Date(value).getTime() - Date.now()) / 1000));
    if (!Number.isFinite(seconds))
        return "—";
    if (seconds < 60)
        return `${seconds} 秒后`;
    const minutes = Math.ceil(seconds / 60);
    return minutes < 60 ? `${minutes} 分钟后` : `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分后`;
}
export async function responseError(response) {
    try {
        const body = await response.json();
        return body.error || body.detail || body.message || response.statusText || `HTTP ${response.status}`;
    }
    catch {
        return response.statusText || `HTTP ${response.status}`;
    }
}
export function unwrapPayload(payload, key) {
    if (payload && typeof payload === "object") {
        const record = payload;
        if (key && record[key] !== undefined)
            return record[key];
        if (record.data !== undefined)
            return record.data;
    }
    return payload;
}
