export type RuntimeState = {
  enabled?: boolean;
  status?: string;
  runtimeStatus?: string;
  preparing?: boolean;
  migrationHold?: boolean;
  migrationConfirmed?: boolean;
  allowRealExecution?: boolean;
  gateReason?: string | null;
  lastRunAt?: string | null;
  lastRunSummary?: string | null;
  nextScanAt?: string | null;
};

export type CookieAccount = {
  accountId?: string;
  accountName?: string;
  name?: string;
  steamId?: string;
  status?: string;
  valid?: boolean;
  lastCheckedAt?: string | null;
  lastRefreshAt?: string | null;
  lastResult?: string | null;
  error?: string | null;
  failureCount?: number;
  lastError?: string | null;
  lastValidatedAt?: string | null;
  batchId?: string | null;
  nextRetryAt?: string | null;
  currencyId?: number | null;
  currency?: string | null;
  currencyStatus?: "cny" | "non_cny" | "unknown" | string;
  currencyCheckedAt?: string | null;
  currencyError?: string | null;
};

export type CookieGate = {
  status?: string;
  validCount?: number;
  totalCount?: number;
  lastCompletedAt?: string | null;
  nextRetryAt?: string | null;
  accounts?: CookieAccount[];
};

export type ScheduledTask = {
  id?: string | number;
  taskType?: string;
  label?: string;
  marketHashName?: string | null;
  accountName?: string | null;
  reason?: string | null;
  priority?: string | number;
  status?: string;
  accountId?: string | null;
  operationId?: string | number | null;
  attemptCount?: number;
  lastError?: string | null;
  nextAttemptAt?: string | null;
};

export type GuadaoIssue = {
  id: string | number;
  issueId?: string | number;
  issueType?: string;
  title?: string;
  severity?: string;
  status?: string;
  accountName?: string | null;
  marketHashName?: string | null;
  summary?: string | null;
  detail?: string | null;
  firstSeenAt?: string | null;
  lastSeenAt?: string | null;
  repeatCount?: number;
  acknowledged?: boolean;
  evidence?: Array<{ label?: string; value?: string }>;
  timeline?: Array<{ at?: string; label?: string; detail?: string }>;
  recommendation?: string | null;
  reason?: string | null;
  nameCn?: string | null;
  accountId?: string | null;
  operationId?: string | number | null;
  assetId?: string | null;
  listingId?: string | null;
  steamId?: string | null;
  category?: string | null;
  rawStatus?: string | null;
  canQueueSafeReview?: boolean;
  safeReviewBlockReason?: string | null;
  createdAt?: string | null;
};

export type GuadaoLog = {
  id?: string | number;
  timestamp?: string;
  level?: string;
  service?: string;
  operation?: string;
  marketHashName?: string | null;
  accountName?: string | null;
  httpStatus?: number | null;
  durationMs?: number | null;
  message?: string;
  requestId?: string | null;
  operationId?: string | number | null;
  tradeNo?: string | null;
  caller?: string | null;
  endpoint?: string | null;
  retryAfter?: string | number | null;
  detail?: Record<string, unknown> | null;
};

export function formatLocal(value?: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

export function formatCountdown(value?: string | null): string {
  if (!value) return "—";
  const seconds = Math.max(0, Math.ceil((new Date(value).getTime() - Date.now()) / 1000));
  if (!Number.isFinite(seconds)) return "—";
  if (seconds < 60) return `${seconds} 秒后`;
  const minutes = Math.ceil(seconds / 60);
  return minutes < 60 ? `${minutes} 分钟后` : `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分后`;
}

export async function responseError(response: Response): Promise<string> {
  try {
    const body = await response.json() as { error?: string; detail?: string; message?: string };
    return body.error || body.detail || body.message || response.statusText || `HTTP ${response.status}`;
  } catch {
    return response.statusText || `HTTP ${response.status}`;
  }
}

export function unwrapPayload<T>(payload: unknown, key?: string): T {
  if (payload && typeof payload === "object") {
    const record = payload as Record<string, unknown>;
    if (key && record[key] !== undefined) return record[key] as T;
    if (record.data !== undefined) return record.data as T;
  }
  return payload as T;
}
