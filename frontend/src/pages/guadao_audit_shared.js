export const GUADAO_AUDIT_STORAGE_KEY = "cs2-guadao-audit-last-request-id";
export const DEFAULT_AUDIT_START = "2026-07-19T15:20";
export const DEFAULT_OPENING_WALLET = "2502.92";
export const DEFAULT_OPENING_REAL_VALUE = "1755.474";
export const AUDIT_STAGES = [
    { key: "local", index: 0, label: "本地流水", hint: "冻结测试窗口与程序记录" },
    { key: "steam", index: 1, label: "Steam 官方", hint: "枚举五账号市场历史与钱包" },
    { key: "c5", index: 2, label: "C5 官方", hint: "读取订单列表与订单详情" },
    { key: "matching", index: 3, label: "证据匹配", hint: "逐笔追踪卖出与唯一补仓去向" },
    { key: "summary", index: 4, label: "汇总核验", hint: "计算数量守恒、余额与综合折扣" },
];
export const AUDIT_TABLES = [
    {
        dataset: "steamSales",
        title: "程序卖出 vs Steam 官方卖出",
        description: "以 Steam 官方成交时间和税后入账为准，逐笔暴露缺失、重复与金额差异。",
        emptyText: "当前任务尚未返回 Steam 卖出对账记录。",
        columns: [
            { key: "account", label: "账号", aliases: ["accountName", "account", "accountId", "steamAccountId"], format: "text", tone: "strong" },
            { key: "item", label: "物品", aliases: ["marketHashName", "market_hash_name", "itemName"], format: "text", tone: "strong" },
            { key: "listingId", label: "listingId", aliases: ["listingId", "listing_id"], format: "text" },
            { key: "purchaseId", label: "purchaseId", aliases: ["purchaseId", "purchase_id"], format: "text" },
            { key: "assetId", label: "assetId", aliases: ["assetId", "asset_id"], format: "text" },
            { key: "soldAt", label: "官方成交时间", aliases: ["officialSoldAt", "steamSoldAt", "timeSold", "soldAt"], format: "datetime" },
            { key: "programGross", label: "程序售价", aliases: ["programGross", "programGrossFen", "localGrossFen", "steamListPriceFen"], format: "money" },
            { key: "officialGross", label: "官方买家支付", aliases: ["officialGross", "officialGrossFen", "buyerTotalMinor", "paidTotalMinor"], format: "money" },
            { key: "programNet", label: "程序税后", aliases: ["programNet", "programNetFen", "localNetFen"], format: "money" },
            { key: "officialNet", label: "官方税后", aliases: ["officialNet", "officialNetFen", "receivedAmountMinor", "steamNetFen"], format: "money" },
            { key: "difference", label: "差额", aliases: ["netDifference", "difference", "differenceFen", "netDifferenceFen"], format: "money", tone: "difference" },
            { key: "reason", label: "核验说明", aliases: ["reason", "evidenceMessage"], format: "text" },
            { key: "evidence", label: "证据", aliases: ["verdict", "evidenceState", "matchState", "state"], format: "evidence" },
        ],
    },
    {
        dataset: "rebuyChains",
        title: "补仓链 vs C5 官方订单 / 手动完结",
        description: "每笔官方卖出只归入一个当前去向；失败父订单不重复计入成功金额。",
        emptyText: "当前任务尚未返回补仓链对账记录。",
        columns: [
            { key: "source", label: "卖出流水", aliases: ["sourceSellOperationId", "sellOperationId", "sourceOperationId"], format: "text" },
            { key: "item", label: "物品", aliases: ["marketHashName", "market_hash_name", "itemName"], format: "text", tone: "strong" },
            { key: "disposition", label: "当前唯一去向", aliases: ["disposition", "destination", "rebuyState", "statusLabel"], format: "text", tone: "strong" },
            { key: "order", label: "C5 订单", aliases: ["c5OrderIds", "assetOrderId", "c5OrderId", "orderId"], format: "text" },
            { key: "remoteStatus", label: "远端状态", aliases: ["c5StatusName", "statusName", "remoteStatus"], format: "text" },
            { key: "steamNet", label: "Steam 税后", aliases: ["steamNet", "steamNetFen"], format: "money" },
            { key: "officialPrice", label: "远端实际价", aliases: ["officialPrice", "officialPriceFen", "remotePriceFen", "c5PriceFen"], format: "money" },
            { key: "effectivePrice", label: "当前生效价", aliases: ["effectiveAmount", "effectiveAmountCents", "effectivePriceFen", "frozenPriceFen", "manualPriceFen"], format: "money" },
            { key: "priceSource", label: "金额来源", aliases: ["priceSource", "amountSource", "evidencePriceSource"], format: "text" },
            { key: "reason", label: "核验说明", aliases: ["reason", "evidenceMessage"], format: "text" },
            { key: "evidence", label: "证据", aliases: ["verdict", "evidenceState", "matchState", "state"], format: "evidence" },
        ],
    },
    {
        dataset: "itemConservation",
        title: "按物品验证数量守恒",
        description: "卖出数量必须等于成功、手动、发货中、待处理及异常去向之和。",
        emptyText: "当前任务尚未返回物品数量守恒记录。",
        columns: [
            { key: "item", label: "物品", aliases: ["marketHashName", "market_hash_name", "itemName"], format: "text", tone: "strong" },
            { key: "sales", label: "Steam 卖出", aliases: ["steamSalesCount", "soldCount", "steamSold"], format: "integer" },
            { key: "c5", label: "C5 成功", aliases: ["c5SuccessCount", "completedCount", "c5Success"], format: "integer" },
            { key: "manual", label: "手动完结", aliases: ["manualComplete", "manualCompletedCount", "manualCount", "manualCompleted"], format: "integer" },
            { key: "delivery", label: "发货确认中", aliases: ["c5DeliveryPending", "deliveryPendingCount", "deliveryPending"], format: "integer" },
            { key: "pending", label: "待补仓", aliases: ["pendingRebuy", "rebuyPendingCount", "pendingRebuyCount", "pending"], format: "integer" },
            { key: "submission", label: "提交待确认", aliases: ["c5SubmissionUnconfirmed", "submissionUnconfirmedCount"], format: "integer" },
            { key: "exception", label: "不确定 / 异常", aliases: ["exception", "uncertainCount", "exceptionCount", "unknownCount"], format: "integer" },
            { key: "difference", label: "数量差", aliases: ["quantityDifference", "difference", "quantityDiff"], format: "integer", tone: "difference" },
            { key: "restored", label: "物理底仓恢复", aliases: ["physicallyRestored"], format: "text" },
            { key: "evidence", label: "证据", aliases: ["verdict", "evidenceState", "state"], format: "evidence" },
        ],
    },
    {
        dataset: "wallet",
        title: "Steam 钱包变化与综合折扣",
        description: "官方卖出入账减官方购买支出，并与五账号实际余额和真实价值复核。",
        emptyText: "当前任务尚未返回钱包对账记录。",
        columns: [
            { key: "account", label: "账号 / 总计", aliases: ["accountName", "account", "label", "accountId"], format: "text", tone: "strong" },
            { key: "accounts", label: "钱包账号", aliases: ["walletAccounts"], format: "text" },
            { key: "opening", label: "期初余额", aliases: ["initialBalance", "openingBalanceFen", "openingWalletFen"], format: "money" },
            { key: "sales", label: "官方卖出", aliases: ["officialSaleNet", "officialSalesNetFen", "steamSalesNetFen"], format: "money" },
            { key: "purchases", label: "官方购买", aliases: ["officialPurchaseSpend", "officialPurchasesFen", "steamPurchaseSpendFen"], format: "money" },
            { key: "inferred", label: "推算期末", aliases: ["predictedEndingBalance", "inferredEndBalanceFen", "calculatedEndBalanceFen"], format: "money" },
            { key: "actual", label: "实际期末", aliases: ["actualEndingBalance", "actualEndBalanceFen", "officialEndBalanceFen"], format: "money" },
            { key: "difference", label: "钱包差额", aliases: ["balanceDifference", "differenceFen", "walletDifferenceFen"], format: "money", tone: "difference" },
            { key: "realValue", label: "预计真实价值", aliases: ["predictedEndingRealValue", "estimatedRealValue", "endingRealValue", "realValue"], format: "money", digits: 4 },
            { key: "discount", label: "综合折扣", aliases: ["endingBalanceDiscount", "discountRatio", "endingDiscountRatio", "balanceDiscount"], format: "percent" },
            { key: "reason", label: "核验说明", aliases: ["reason", "evidenceMessage"], format: "text" },
            { key: "evidence", label: "证据", aliases: ["verdict", "evidenceState", "state"], format: "evidence" },
        ],
    },
];
const STAGE_ALIASES = {
    local: "local",
    database: "local",
    local_records: "local",
    localrecords: "local",
    local_collecting: "local",
    steam: "steam",
    steam_history: "steam",
    steamhistory: "steam",
    steam_collecting: "steam",
    c5: "c5",
    c5_orders: "c5",
    c5orders: "c5",
    c5_collecting: "c5",
    match: "matching",
    matching: "matching",
    evidence_matching: "matching",
    evidencematching: "matching",
    aggregate: "summary",
    summary: "summary",
    reconciliation: "summary",
    finished: "summary",
};
const DATASET_SECTIONS = {
    steamSales: "steam_sales",
    rebuyChains: "rebuys",
    itemConservation: "item_conservation",
    wallet: "wallet_discount",
};
function isRecord(value) {
    return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
function recordFrom(value) {
    return isRecord(value) ? value : {};
}
function nestedRecord(root, keys) {
    for (const key of keys) {
        const candidate = root[key];
        if (isRecord(candidate))
            return candidate;
    }
    return {};
}
export function firstValue(root, aliases) {
    for (const alias of aliases) {
        if (root[alias] !== undefined && root[alias] !== null)
            return root[alias];
    }
    return undefined;
}
export function finiteNumber(value) {
    if (typeof value === "number")
        return Number.isFinite(value) ? value : null;
    if (typeof value !== "string" || !value.trim())
        return null;
    const parsed = Number(value.replace(/,/g, "").trim());
    return Number.isFinite(parsed) ? parsed : null;
}
function integer(value, fallback = 0) {
    const parsed = finiteNumber(value);
    return parsed === null ? fallback : Math.max(0, Math.trunc(parsed));
}
function booleanOrNull(value) {
    if (typeof value === "boolean")
        return value;
    if (value === 1 || value === "1" || value === "true")
        return true;
    if (value === 0 || value === "0" || value === "false")
        return false;
    return null;
}
function stringValue(value) {
    return value === undefined || value === null ? "" : String(value).trim();
}
function safePercent(value, done, total) {
    const explicit = finiteNumber(value);
    if (explicit !== null) {
        const normalized = explicit <= 1 && explicit >= 0 ? explicit * 100 : explicit;
        return Math.min(100, Math.max(0, normalized));
    }
    if (total > 0)
        return Math.min(100, Math.max(0, (done / total) * 100));
    return 0;
}
export function toBeijingDateTimeInput(value = new Date()) {
    const parts = new Intl.DateTimeFormat("en-CA", {
        timeZone: "Asia/Shanghai",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hourCycle: "h23",
    }).formatToParts(value);
    const pick = (type) => parts.find((part) => part.type === type)?.value || "00";
    return `${pick("year")}-${pick("month")}-${pick("day")}T${pick("hour")}:${pick("minute")}`;
}
export function toBeijingApiTimestamp(value) {
    const trimmed = value.trim();
    if (!trimmed)
        return "";
    if (/Z$|[+-]\d{2}:\d{2}$/.test(trimmed))
        return trimmed;
    if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(trimmed))
        return `${trimmed}:00+08:00`;
    if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/.test(trimmed))
        return `${trimmed}+08:00`;
    return trimmed;
}
export function normalizeDecimalInput(value, fallback) {
    const raw = stringValue(value);
    if (!raw)
        return fallback;
    const parsed = finiteNumber(raw);
    return parsed === null ? fallback : raw.replace(/,/g, "");
}
export function defaultAuditForm(now = new Date()) {
    return {
        dateFrom: DEFAULT_AUDIT_START,
        dateTo: toBeijingDateTimeInput(now),
        openingWallet: DEFAULT_OPENING_WALLET,
        openingRealValue: DEFAULT_OPENING_REAL_VALUE,
        accountIds: [],
    };
}
export function normalizeAuditPreset(payload, now = new Date()) {
    const outer = recordFrom(payload);
    const data = nestedRecord(outer, ["preset", "defaultPreset", "data"]);
    const presetList = Array.isArray(outer.presets) ? outer.presets : [];
    const firstPreset = presetList.find(isRecord) || {};
    const source = Object.keys(data).length ? data : Object.keys(firstPreset).length ? firstPreset : outer;
    const defaults = defaultAuditForm(now);
    const rawAccounts = Array.isArray(source.accounts)
        ? source.accounts
        : Array.isArray(source.steamAccounts)
            ? source.steamAccounts
            : Array.isArray(source.accountIds)
                ? source.accountIds
                : Array.isArray(source.steamAccountIds)
                    ? source.steamAccountIds
                    : [];
    const accounts = rawAccounts.flatMap((entry) => {
        if (typeof entry === "string" && entry.trim()) {
            return [{ id: entry.trim(), label: entry.trim(), steamId: null }];
        }
        if (!isRecord(entry))
            return [];
        const id = stringValue(firstValue(entry, ["id", "accountId", "steamAccountId", "steamId"]));
        if (!id)
            return [];
        return [{
                id,
                label: stringValue(firstValue(entry, ["label", "account", "accountName", "name"])) || id,
                steamId: stringValue(firstValue(entry, ["steamId", "steamId64"])) || null,
            }];
    });
    const dateFrom = stringValue(firstValue(source, ["dateFrom", "startAt", "startTime"])) || defaults.dateFrom;
    const dateTo = stringValue(firstValue(source, ["dateTo", "endAt", "endTime"])) || defaults.dateTo;
    return {
        dateFrom: dateFrom.slice(0, 16),
        dateTo: dateTo.slice(0, 16),
        openingWallet: normalizeDecimalInput(firstValue(source, ["openingWallet", "openingWalletCny", "openingSteamBalance", "initialBalance"]), defaults.openingWallet),
        openingRealValue: normalizeDecimalInput(firstValue(source, ["openingRealValue", "openingRealValueCny", "openingCashValue", "initialRealValue"]), defaults.openingRealValue),
        accounts,
    };
}
export function validateAuditForm(form) {
    const errors = [];
    const start = Date.parse(toBeijingApiTimestamp(form.dateFrom));
    const end = Date.parse(toBeijingApiTimestamp(form.dateTo));
    if (!form.dateFrom || Number.isNaN(start))
        errors.push("开始时间无效");
    if (!form.dateTo || Number.isNaN(end))
        errors.push("结束时间无效");
    if (!Number.isNaN(start) && !Number.isNaN(end) && end <= start)
        errors.push("结束时间必须晚于开始时间");
    const wallet = finiteNumber(form.openingWallet);
    const realValue = finiteNumber(form.openingRealValue);
    if (wallet === null || wallet < 0)
        errors.push("期初 Steam 账面余额无效");
    if (realValue === null || realValue < 0)
        errors.push("期初余额真实价值无效");
    return errors;
}
export function buildAuditRunRequest(form) {
    const dateFrom = toBeijingApiTimestamp(form.dateFrom);
    const dateTo = toBeijingApiTimestamp(form.dateTo);
    const accountIds = form.accountIds.length ? [...form.accountIds] : "all";
    return {
        dateFrom,
        dateTo,
        startAt: dateFrom,
        endAt: dateTo,
        steamAccountIds: accountIds,
        accountIds,
        openingWallet: form.openingWallet.trim(),
        openingRealValue: form.openingRealValue.trim(),
        initialBalance: form.openingWallet.trim(),
        initialRealValue: form.openingRealValue.trim(),
        expectedAccountCount: form.accountIds.length || 5,
        mode: "strict_official",
        readOnly: true,
    };
}
export function normalizeAuditStatus(value) {
    const status = stringValue(value).toLowerCase().replace(/[\s-]+/g, "_");
    if (["running", "processing", "retrying", "collecting"].includes(status))
        return "running";
    if (["completed", "complete", "done", "success", "passed", "inconclusive", "partial", "completed_with_gaps", "completedwithgaps"].includes(status))
        return "completed";
    if (["failed", "error"].includes(status))
        return "failed";
    if (["cancelled", "canceled"].includes(status))
        return "cancelled";
    return "queued";
}
export function normalizeAuditVerdict(value) {
    const verdict = stringValue(value).toLowerCase().replace(/[\s-]+/g, "_");
    if (["passed", "pass", "success", "verified"].includes(verdict))
        return "passed";
    if (["failed", "fail", "mismatch"].includes(verdict))
        return "failed";
    if (["inconclusive", "unknown", "partial", "completed_with_gaps", "unverified"].includes(verdict))
        return "inconclusive";
    return null;
}
export function normalizeAuditStage(value) {
    const key = stringValue(value).toLowerCase().replace(/[\s-]+/g, "_");
    return STAGE_ALIASES[key] || null;
}
export function isActiveAuditStatus(status) {
    return status === "queued" || status === "running";
}
function normalizeAuditError(value) {
    if (!value)
        return null;
    if (typeof value === "string") {
        return { source: "audit", code: "audit_error", message: value, retryable: false, coverageComplete: null };
    }
    const error = recordFrom(value);
    const message = stringValue(firstValue(error, ["message", "error", "detail"]));
    if (!message)
        return null;
    return {
        source: stringValue(firstValue(error, ["source", "provider", "stage"])) || "audit",
        code: stringValue(firstValue(error, ["code", "errorCode"])) || "audit_error",
        message,
        retryable: Boolean(firstValue(error, ["retryable", "canRetry"])),
        coverageComplete: booleanOrNull(firstValue(error, ["coverageComplete", "rangeCoverageComplete"])),
    };
}
function normalizeEvidenceGap(value) {
    if (typeof value === "string" && value.trim()) {
        return {
            source: "audit",
            code: "evidence_gap",
            message: value.trim(),
            state: "unknown",
            coverageComplete: null,
            observedAt: null,
            references: [],
        };
    }
    if (!isRecord(value))
        return null;
    const referencesValue = firstValue(value, ["references", "refs", "evidenceRefs"]);
    const references = Array.isArray(referencesValue)
        ? referencesValue.map(stringValue).filter(Boolean)
        : stringValue(referencesValue) ? [stringValue(referencesValue)] : [];
    return {
        source: stringValue(firstValue(value, ["source", "provider", "dataset"])) || "audit",
        code: stringValue(firstValue(value, ["code", "reasonCode"])) || "evidence_gap",
        message: stringValue(firstValue(value, ["message", "reason", "detail"])) || "官方证据不完整",
        state: stringValue(firstValue(value, ["state", "evidenceState", "status"])) || "unknown",
        coverageComplete: booleanOrNull(firstValue(value, ["coverageComplete", "rangeCoverageComplete"])),
        observedAt: stringValue(firstValue(value, ["observedAt", "checkedAt", "updatedAt"])) || null,
        references,
    };
}
function normalizeEvidenceGaps(value) {
    if (!Array.isArray(value))
        return [];
    return value.map(normalizeEvidenceGap).filter((gap) => gap !== null);
}
export function extractEvidenceGaps(row) {
    const direct = firstValue(row, ["evidenceGaps", "gaps", "evidenceErrors", "missingEvidence"]);
    const gaps = normalizeEvidenceGaps(direct);
    if (gaps.length)
        return gaps;
    const state = stringValue(firstValue(row, ["verdict", "evidenceState", "matchState", "state"]));
    if (!["unknown", "unverified", "inconclusive", "partial", "missing", "conflict", "mismatch"].includes(state.toLowerCase())) {
        return [];
    }
    const message = stringValue(firstValue(row, ["evidenceMessage", "reason", "error", "message"])) || "官方证据不完整";
    const fallback = normalizeEvidenceGap({
        source: firstValue(row, ["source", "dataset", "provider"]),
        code: firstValue(row, ["evidenceCode", "code"]),
        message,
        state,
        coverageComplete: firstValue(row, ["coverageComplete", "rangeCoverageComplete"]),
        observedAt: firstValue(row, ["observedAt", "updatedAt"]),
    });
    return fallback ? [fallback] : [];
}
export function normalizeAuditRun(payload, fallbackRequestId = "") {
    const outer = recordFrom(payload);
    const candidate = nestedRecord(outer, ["run", "job", "data"]);
    const run = Object.keys(candidate).length ? candidate : outer;
    const progressRecord = nestedRecord(run, ["progress"]);
    const done = integer(firstValue(progressRecord, ["done", "completed", "processed", "current"]));
    const total = integer(firstValue(progressRecord, ["total", "expected", "target"]));
    const rawStatus = firstValue(run, ["status", "state", "jobStatus"]);
    let status = normalizeAuditStatus(rawStatus);
    const rawVerdict = firstValue(run, ["verdict", "result", "conclusion", "evidenceState"]);
    const normalizedStatusText = stringValue(rawStatus).toLowerCase();
    let verdict = normalizeAuditVerdict(rawVerdict)
        || (["inconclusive", "partial", "completed_with_gaps", "completedwithgaps"].includes(normalizedStatusText) ? "inconclusive" : null)
        || (normalizedStatusText === "passed" ? "passed" : null);
    const summary = nestedRecord(run, ["summary", "kpis", "totals"]);
    const coverage = nestedRecord(run, ["coverage", "evidenceCoverage"]);
    const normalizedError = normalizeAuditError(firstValue(run, ["error", "lastError"]));
    if (normalizedStatusText === "failed" && !normalizedError) {
        status = "completed";
        verdict = "failed";
    }
    const directGaps = firstValue(run, ["evidenceGaps", "gaps", "missingEvidence"])
        ?? firstValue(summary, ["evidenceGaps", "gaps", "missingEvidence"]);
    const evidenceGaps = normalizeEvidenceGaps(directGaps);
    const evidenceComplete = firstValue(summary, ["evidenceComplete", "coverageComplete"]);
    if (status === "completed" && verdict === null && (evidenceComplete === false || evidenceGaps.length > 0)) {
        verdict = "inconclusive";
    }
    const stage = normalizeAuditStage(firstValue(run, ["stage", "currentStage"]) || firstValue(progressRecord, ["stage", "currentStage"]));
    const stagePercent = stage === "local" ? 15 : stage === "steam" ? 35 : stage === "c5" ? 55 : stage === "matching" ? 75 : stage === "summary" ? 90 : 0;
    const percent = status === "completed" ? 100 : Math.max(stagePercent, safePercent(firstValue(progressRecord, ["percent", "percentage", "ratio"]), done, total));
    return {
        requestId: stringValue(firstValue(run, ["requestId", "request_id", "jobId", "id"])) || fallbackRequestId,
        status,
        verdict,
        stage,
        progress: {
            done,
            total,
            percent,
            message: stringValue(firstValue(progressRecord, ["message", "detail", "label"]) || firstValue(run, ["message", "statusMessage"])),
        },
        summary,
        coverage,
        evidenceGaps,
        error: normalizedError,
        createdAt: stringValue(firstValue(run, ["createdAt", "queuedAt", "startedAt"])) || null,
        updatedAt: stringValue(firstValue(run, ["updatedAt", "completedAt", "finishedAt"])) || null,
        raw: run,
    };
}
export function normalizeAuditRows(payload, dataset, requestedPage, requestedPageSize) {
    const outer = recordFrom(payload);
    const candidate = nestedRecord(outer, ["data", "result"]);
    const source = Object.keys(candidate).length ? candidate : outer;
    const rawRows = firstValue(source, ["rows", "items", "records"]);
    const rows = Array.isArray(rawRows) ? rawRows.filter(isRecord) : [];
    const page = Math.max(1, integer(firstValue(source, ["page", "currentPage"]), requestedPage));
    const pageSize = Math.max(1, integer(firstValue(source, ["pageSize", "limit"]), requestedPageSize));
    const totalValue = finiteNumber(firstValue(source, ["total", "totalCount", "count"]));
    const total = totalValue === null ? rows.length : Math.max(0, Math.trunc(totalValue));
    const explicitHasMore = firstValue(source, ["hasMore", "hasNext"]);
    const hasMore = typeof explicitHasMore === "boolean" ? explicitHasMore : page * pageSize < total;
    return { dataset, rows, page, pageSize, total, hasMore };
}
export function summaryNumber(summary, aliases) {
    return finiteNumber(firstValue(summary, aliases));
}
export function stageState(stage, run) {
    if (!run)
        return "pending";
    const targetIndex = AUDIT_STAGES.find((entry) => entry.key === stage)?.index ?? 0;
    const currentIndex = AUDIT_STAGES.find((entry) => entry.key === run.stage)?.index ?? -1;
    if (run.status === "completed")
        return "completed";
    if (run.status === "failed")
        return targetIndex === Math.max(0, currentIndex) ? "failed" : targetIndex < currentIndex ? "completed" : "pending";
    if (run.status === "cancelled")
        return targetIndex === Math.max(0, currentIndex) ? "cancelled" : targetIndex < currentIndex ? "completed" : "pending";
    if (currentIndex < 0)
        return targetIndex === 0 && run.status === "running" ? "current" : "pending";
    if (targetIndex < currentIndex)
        return "completed";
    if (targetIndex === currentIndex)
        return "current";
    return "pending";
}
export function formatMoney(value, digits = 2) {
    const parsed = finiteNumber(value);
    if (parsed === null)
        return "—";
    return new Intl.NumberFormat("zh-CN", {
        style: "currency",
        currency: "CNY",
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
    }).format(parsed);
}
export function formatMoneyFen(value) {
    const parsed = finiteNumber(value);
    return parsed === null ? "—" : formatMoney(parsed / 100, 2);
}
export function formatPercent(value) {
    const parsed = finiteNumber(value);
    if (parsed === null)
        return "—";
    const ratio = Math.abs(parsed) > 1 ? parsed / 100 : parsed;
    return new Intl.NumberFormat("zh-CN", { style: "percent", minimumFractionDigits: 2, maximumFractionDigits: 4 }).format(ratio);
}
export function formatDateTime(value) {
    const raw = stringValue(value);
    if (!raw)
        return "—";
    const date = new Date(raw);
    if (Number.isNaN(date.getTime()))
        return raw;
    return date.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
}
export function evidenceLabel(value) {
    const state = stringValue(value).toLowerCase().replace(/[\s-]+/g, "_");
    if (["verified", "matched", "passed", "success", "official"].includes(state))
        return "已验证";
    if (["mismatch", "failed", "conflict", "duplicate"].includes(state))
        return "存在差异";
    if (["manual", "manual_external"].includes(state))
        return "人工证据";
    if (["unknown", "unverified", "inconclusive", "partial", "missing"].includes(state))
        return "证据不完整";
    return state ? stringValue(value) : "待核验";
}
export function evidenceTone(value) {
    const label = evidenceLabel(value);
    if (label === "已验证")
        return "success";
    if (label === "存在差异")
        return "danger";
    if (label === "证据不完整" || label === "人工证据")
        return "warning";
    return "neutral";
}
export function auditCellValue(row, column) {
    return firstValue(row, column.aliases);
}
export function formatAuditCell(row, column) {
    const matchedAlias = column.aliases.find((alias) => row[alias] !== undefined && row[alias] !== null);
    const value = matchedAlias ? row[matchedAlias] : undefined;
    if (column.format === "moneyFen")
        return formatMoneyFen(value);
    if (column.format === "money") {
        if (matchedAlias && /(Fen|Cents|Minor)$/i.test(matchedAlias))
            return formatMoneyFen(value);
        return formatMoney(value, column.digits ?? 2);
    }
    if (column.format === "percent")
        return formatPercent(value);
    if (column.format === "datetime")
        return formatDateTime(value);
    if (column.format === "integer") {
        const parsed = finiteNumber(value);
        return parsed === null ? "—" : String(Math.trunc(parsed));
    }
    if (column.format === "evidence")
        return evidenceLabel(value);
    if (Array.isArray(value))
        return value.length ? `${value.length} 项` : "—";
    if (typeof value === "boolean")
        return value ? "是" : "否";
    return stringValue(value) || "—";
}
export function auditRowKey(row, dataset, index) {
    const value = firstValue(row, [
        "id",
        "rowId",
        "listingId",
        "purchaseId",
        "sourceSellOperationId",
        "marketHashName",
        "accountId",
    ]);
    return `${dataset}:${stringValue(value) || index}`;
}
export function buildAuditRowsUrl(requestId, dataset, page, pageSize) {
    const query = new URLSearchParams({ section: DATASET_SECTIONS[dataset], page: String(page), pageSize: String(pageSize) });
    return `/api/guadao-audit/runs/${encodeURIComponent(requestId)}/rows?${query.toString()}`;
}
export function buildAuditExportUrl(requestId, format) {
    const query = new URLSearchParams({ format });
    return `/api/guadao-audit/runs/${encodeURIComponent(requestId)}/export?${query.toString()}`;
}
export function verdictCopy(verdict) {
    if (verdict === "passed") {
        return { label: "PASSED", title: "严格对账通过", description: "官方覆盖完整，卖出、补仓去向、数量和钱包均已一致。" };
    }
    if (verdict === "failed") {
        return { label: "FAILED", title: "严格对账发现差异", description: "官方覆盖已经足够，但存在可证明的缺失、重复、数量或金额差异。" };
    }
    if (verdict === "inconclusive") {
        return { label: "INCONCLUSIVE", title: "证据不足，无法判定", description: "至少一个官方数据源覆盖不完整或匹配冲突；未知项没有按零处理。" };
    }
    return { label: "WAITING", title: "等待严格对账结论", description: "任务完成前不会提前显示通过或差异为零。" };
}
