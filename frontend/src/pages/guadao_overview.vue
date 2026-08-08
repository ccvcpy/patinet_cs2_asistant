<script setup lang="ts">
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, ref, watch } from "vue";
import { RouterLink } from "vue-router";
import FolioIcon from "../components/FolioIcon.vue";
import { formatCountdown, formatLocal, responseError, unwrapPayload, type CookieGate, type GuadaoIssue, type RuntimeState, type ScheduledTask } from "./guadao_shared";

type TaskRun = {
  id?: string;
  completedAt?: string;
  taskKey?: string;
  taskType?: string;
  label?: string;
  accountName?: string | null;
  operationId?: number | null;
  marketHashName?: string | null;
  ok?: boolean;
  summary?: string;
  error?: string | null;
};

type ScanItem = {
  name?: string;
  marketHashName?: string;
  listingRatioPct?: number;
  maxListingRatioPct?: number;
  steamListPrice?: number;
  steamNetAmount?: number;
  c5RebuyPrice?: number;
  c5TradableCount?: number;
  localExecutableCount?: number;
  accountInventory?: Array<{ accountName?: string; count?: number }>;
  eligible?: boolean;
  decision?: string;
  reason?: string | null;
  stage?: string | null;
  requestSent?: boolean | null;
};

type ScanRound = {
  generatedAt?: string;
  inventorySource?: string;
  poolTypeCount?: number;
  evaluatedCount?: number;
  missingPriceCount?: number;
  notEvaluatedCount?: number;
  queueDeferredCount?: number;
  outcomeCounts?: Record<string, number>;
  candidateCount?: number;
  executableCount?: number;
  listedCount?: number;
  blockedByOpenCycle?: boolean;
  globalMaxListingRatioPct?: number;
  decisionCounts?: Record<string, number>;
  items?: ScanItem[];
  itemsTruncated?: boolean;
};

type Dashboard = {
  generatedAt?: string;
  runtime?: RuntimeState;
  cookieGate?: CookieGate;
  steamAuthHealth?: CookieGate;
  summary?: { activeListings?: number; pendingListingConfirmations?: number; pendingRebuys?: number; c5EvidencePending?: number; deliveryPending?: number; issueCount?: number; steamHeatPct?: number | null };
  settingsSummary?: { guadaoMaxListingRatio?: number; autoListEnabled?: boolean; autoRebuyEnabled?: boolean };
  dueTasks?: ScheduledTask[];
  taskQueue?: ScheduledTask[];
  recentTaskRuns?: TaskRun[];
  scanRounds?: ScanRound[];
  steamScheduler?: {
    status?: string;
    queueLength?: number;
    activeRequest?: string | null;
    requestsPerMinute?: number | null;
    cooldownUntil?: string | null;
    priorities?: Array<{ priority?: string; label?: string; queued?: number }>;
    circuits?: Array<{
      circuitKey?: string;
      scope?: string;
      accountId?: string | null;
      route?: string | null;
      state?: string;
      consecutive429?: number;
      last429At?: string | null;
      cooldownUntil?: string | null;
      nextProbeAt?: string | null;
    }>;
  };
  specialRules?: Array<{ id?: string | number; marketHashName?: string; displayName?: string | null; maxRatioPct?: number; currentRatioPct?: number | null; currentRatioObservedAt?: string | null; enabled?: boolean }>;
  issues?: GuadaoIssue[];
};

const dashboard = ref<Dashboard | null>(null);
const loading = ref(false);
const actionBusy = ref(false);
const error = ref("");
const notice = ref("");
const confirmAction = ref<"enable" | "disable" | "retry-auth" | "refresh-auth" | null>(null);
const cookieExpanded = ref(false);
const expandedScanAt = ref<string | null>(null);
let timer: ReturnType<typeof setInterval> | null = null;

const runtime = computed(() => dashboard.value?.runtime || {});
const cookieGate = computed(() => dashboard.value?.steamAuthHealth || dashboard.value?.cookieGate || {});
const cookieAccounts = computed(() => cookieGate.value.accounts || []);
const enabled = computed(() => Boolean(runtime.value.enabled));
const gateReady = computed(() => (cookieGate.value.totalCount || 0) > 0 && cookieGate.value.validCount === cookieGate.value.totalCount);
const failedCookieAccounts = computed(() => cookieAccounts.value.filter(account => !account.valid && account.status !== "refreshing"));
const cookieHealthy = computed(() => gateReady.value && failedCookieAccounts.value.length === 0 && cookieGate.value.status !== "degraded");
const cookieProgress = computed(() => {
  const total = Number(cookieGate.value.totalCount || 0);
  return total > 0 ? Math.min(100, Math.max(0, Number(cookieGate.value.validCount || 0) / total * 100)) : 0;
});
const runtimeStatus = computed(() => String(runtime.value.runtimeStatus || runtime.value.status || ""));
const runtimeLabel = computed(() => {
  if (runtime.value.migrationHold) return "迁移保护中";
  if (runtimeStatus.value === "closing_only") return "存量闭环中";
  if (cookieGate.value.status === "degraded") return "降级运行中";
  if (runtime.value.preparing || runtimeStatus.value === "preparing" || (enabled.value && !gateReady.value)) return "启动准备中";
  return enabled.value ? "运行中" : "已关闭";
});
const runtimeMessage = computed(() => {
  if (runtime.value.migrationHold) return "迁移保护期间只读审计，不发送 Steam/C5 真实写操作。";
  if (runtimeStatus.value === "closing_only") return "新扫描与新上架已停止；已有挂单、卖出确认、补仓与发货确认继续安全闭环。";
  if (cookieGate.value.status === "degraded") return "仅暂停认证异常账号的新动作；其他有效账号与已有安全闭环继续运行。";
  if (enabled.value && !gateReady.value) return "新扫描与新上架暂未启动；已有流水继续安全闭环。";
  return runtime.value.lastRunSummary || (enabled.value ? "新扫描与新上架已开放。" : "执行器已关闭，未发现存量闭环任务。");
});
const quietWindow = computed(() => (dashboard.value?.steamScheduler?.circuits || []).find(
  row => row.scope === "quiet" && row.state === "open",
));
const activeCircuits = computed(() => (dashboard.value?.steamScheduler?.circuits || []).filter(
  row => row.scope !== "quiet" && (row.state === "open" || row.state === "half_open"),
));
const routeCooldownUntil = computed(() => activeCircuits.value.map(row => row.nextProbeAt || row.cooldownUntil).filter(Boolean).sort()[0] || null);
watch(cookieHealthy, healthy => {
  if (!healthy) cookieExpanded.value = true;
});

function scanDecisionLabel(value?: string): string {
  if (value === "executable_candidate") return "可执行候选";
  if (value === "ratio_above_limit") return "比例高于上限";
  if (value === "no_local_executable_asset") return "本地无可执行资产";
  if (value === "waiting_existing_cycle") return "等待旧流水闭环";
  if (value === "queue_deferred") return "Steam 队列顺延";
  if (value === "steam_price_missing") return "Steam 卖盘确实缺价";
  if (value === "c5_price_missing") return "C5 补仓价缺失";
  if (value === "request_failed") return "行情请求失败";
  if (value === "market_state_missing") return "行情状态缺失";
  if (value === "steam_price_not_orderbook") return "非官方 Steam 价格";
  if (value === "below_min_price") return "低于扫描下限";
  if (value === "calculation_failed") return "指标计算失败";
  return value || "未分类";
}

function accountInventoryLabel(item: ScanItem): string {
  const rows = item.accountInventory || [];
  if (!rows.length) return "无";
  return rows.map(row => `${row.accountName || "未命名"} ${row.count || 0}件`).join("、");
}

function scanNotEvaluatedCount(round: ScanRound): number {
  if (round.notEvaluatedCount != null) return Number(round.notEvaluatedCount);
  return Number(round.missingPriceCount || 0);
}
const confirmCopy = computed(() => {
  if (confirmAction.value === "enable") return { title: "开启挂刀执行器", text: "后端将先刷新并验证全部 Steam 账号 Cookie。只有达到全部有效后，才会开放新扫描与新挂刀。", button: "确认开启" };
  if (confirmAction.value === "disable") return { title: "关闭挂刀执行器", text: "关闭后停止新扫描、新上架和新的非必要动作；已有挂单同步、卖出确认、补仓证据复核、已购待收货确认和结算仍会继续，并可能产生真实 Steam/C5 写操作。", button: "确认关闭" };
  if (confirmAction.value === "retry-auth") return { title: "立即重试失败账号", text: "只把当前认证失败或网络状态未知的账号重新排到 Steam Cookie 恢复队列；不会刷新已经有效的账号，也不会绕过共享 Steam 请求调度。", button: "确认重试" };
  return { title: "刷新全部 Steam Cookie", text: "这会为全部本地 Steam 账号重新建立认证刷新批次，并通过共享请求调度依次执行。期间新扫描和新上架仍服从 Cookie 门禁。", button: "确认全部刷新" };
});

async function refresh(): Promise<void> {
  loading.value = true;
  try {
    const response = await fetch("/api/guadao/dashboard", { cache: "no-store" });
    if (!response.ok) throw new Error(await responseError(response));
    dashboard.value = unwrapPayload<Dashboard>(await response.json(), "dashboard");
    error.value = "";
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : String(reason);
  } finally {
    loading.value = false;
  }
}

async function post(path: string, body: Record<string, unknown> = {}): Promise<void> {
  actionBusy.value = true;
  notice.value = "";
  try {
    const response = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!response.ok) throw new Error(await responseError(response));
    notice.value = "操作已提交，正在读取最新状态。";
    confirmAction.value = null;
    await refresh();
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : String(reason);
  } finally {
    actionBusy.value = false;
  }
}

function submitConfirmed(): void {
  if (confirmAction.value === "enable") void post("/api/guadao/runtime/toggle", { enabled: true });
  else if (confirmAction.value === "disable") void post("/api/guadao/runtime/toggle", { enabled: false });
  else if (confirmAction.value === "retry-auth") void post("/api/guadao/auth/retry-failed");
  else if (confirmAction.value === "refresh-auth") void post("/api/guadao/cookies/refresh");
}
function startPolling(): void { if (timer === null) timer = setInterval(() => void refresh(), 10_000); }
function stopPolling(): void { if (timer !== null) clearInterval(timer); timer = null; }
onMounted(() => { void refresh(); startPolling(); });
onActivated(startPolling);
onDeactivated(stopPolling);
onUnmounted(stopPolling);
</script>

<template>
  <main class="page overview-page">
    <header class="overview-heading">
      <div><p class="eyebrow">Guadao Operations</p><h1>挂刀执行器运行总览</h1><p>按任务到期时间推进，不再依赖固定轮询间隔</p></div>
      <section class="runtime-card">
        <div class="runtime-card-top"><div><strong>挂刀执行器</strong><span :class="['runtime-state', enabled ? 'on' : 'off']">{{ runtimeLabel }}</span></div><button class="switch" :class="{ on: enabled }" type="button" :disabled="actionBusy" @click="confirmAction = enabled ? 'disable' : 'enable'"><span></span><b>{{ enabled ? "ON" : "OFF" }}</b></button></div>
        <p><FolioIcon :name="enabled && !gateReady ? 'refresh' : 'shield'" :size="14" />{{ runtimeMessage }}</p>
        <div v-if="enabled && !gateReady" class="cookie-progress" aria-label="Steam Cookie 准备进度"><i :style="{ width: `${cookieProgress}%` }"></i></div>
        <p v-if="failedCookieAccounts.length" class="retry-status">失败账号 {{ failedCookieAccounts.length }} 个 · {{ cookieGate.nextRetryAt ? `${formatCountdown(cookieGate.nextRetryAt)}自动重试` : "等待调度" }} · 最高第 {{ Math.max(...failedCookieAccounts.map(row => Number(row.failureCount || 0))) + 1 }} 次</p>
        <div v-if="failedCookieAccounts.length" class="runtime-actions"><button class="secondary-button" type="button" :disabled="actionBusy" @click="confirmAction = 'retry-auth'">立即重试失败账号</button></div>
        <small>Profit Trade 在自己的页面独立开关</small>
      </section>
    </header>

    <div v-if="runtime.migrationHold" class="migration-banner"><FolioIcon name="shield" :size="17" /><div><strong>首次迁移保护中</strong><span>当前只读审计，禁止 Steam/C5 真实写操作。解除迁移保护属于受控发布操作，不在日常运营页面开放。</span></div></div>
    <p v-if="error" class="feedback error">API 请求失败：{{ error }}</p><p v-else-if="notice" class="feedback success">{{ notice }}</p>

    <section class="metric-row">
      <article><span>活跃挂单</span><strong>{{ dashboard?.summary?.activeListings ?? "—" }}</strong></article>
      <article><span>挂单待确认</span><strong>{{ dashboard?.summary?.pendingListingConfirmations ?? "—" }}</strong></article>
      <article><span>待补仓</span><strong>{{ dashboard?.summary?.pendingRebuys ?? "—" }}</strong></article>
      <article><span>C5 补仓待查证据</span><strong>{{ dashboard?.summary?.c5EvidencePending ?? "—" }}</strong></article>
      <article><span>C5 已购买待收货</span><strong>{{ dashboard?.summary?.deliveryPending ?? "—" }}</strong></article>
      <article><span>异常与待处理</span><strong>{{ dashboard?.summary?.issueCount ?? "—" }}</strong></article>
      <article><span>Steam 请求热度</span><strong>{{ dashboard?.summary?.steamHeatPct == null ? "—" : `${dashboard.summary.steamHeatPct.toFixed(0)}%` }}</strong></article>
    </section>

    <section class="panel cookie-panel">
      <div class="section-title cookie-title"><div><h2>Steam 账号 Cookie 健康</h2><p>{{ cookieHealthy ? "全部账号有效，详情已折叠" : "存在准备中或异常账号，详情已展开" }} · 最近完成 {{ formatLocal(cookieGate.lastCompletedAt) }}</p></div><div class="cookie-summary"><strong>{{ cookieGate.validCount || 0 }} / {{ cookieGate.totalCount || 0 }} 有效</strong><button v-if="failedCookieAccounts.length" class="secondary-button" type="button" :disabled="actionBusy" @click="confirmAction = 'retry-auth'"><FolioIcon name="refresh" :size="14" />重试失败账号</button><button class="secondary-button" type="button" :disabled="actionBusy" @click="confirmAction = 'refresh-auth'"><FolioIcon name="refresh" :size="14" />刷新全部 Cookie</button><button class="fold-button" type="button" :aria-expanded="cookieExpanded" aria-controls="cookie-health-detail" @click="cookieExpanded = !cookieExpanded">{{ cookieExpanded ? "收起详情" : "展开详情" }}<FolioIcon :name="cookieExpanded ? 'chevron-up' : 'chevron-down'" :size="14" /></button></div></div>
      <div v-if="cookieExpanded" id="cookie-health-detail" class="cookie-detail">
        <div v-if="cookieAccounts.length" class="table-wrap"><table class="data-table cookie-table"><thead><tr><th>账号</th><th>SteamID</th><th>Cookie 状态</th><th>最近验证</th><th>最近刷新结果</th><th>失败次数</th><th>下次动作</th></tr></thead><tbody><tr v-for="account in cookieAccounts" :key="account.accountId || account.steamId"><td>{{ account.accountName || account.name || "未命名" }}</td><td class="mono">{{ account.steamId || "—" }}</td><td><span :class="['pill', account.valid ? 'success' : account.status === 'unknown' ? 'neutral' : 'warning']">{{ account.valid ? "有效" : account.status || "未知" }}</span></td><td>{{ formatLocal(account.lastCheckedAt) }}</td><td :class="{ 'danger-text': account.error }">{{ account.error || account.lastResult || "—" }}</td><td>{{ account.failureCount || 0 }}</td><td>{{ account.nextRetryAt ? formatCountdown(account.nextRetryAt) : "运行中监测" }}</td></tr></tbody></table></div>
        <div v-else class="empty-state">{{ loading ? "正在读取 Cookie 健康状态…" : "后端暂未返回账号 Cookie 状态。" }}</div>
        <div class="cookie-legend"><span><b>400 / 401</b> Cookie 失效，暂停该账号新动作并自动刷新</span><span><b>429 / 超时</b> Steam 限流或网络未知，不误判 Cookie 失效，也不触发 relogin</span></div>
      </div>
    </section>

    <section class="panel execution-panel">
      <div class="section-title"><div><h2>任务执行摘要</h2><p>保留控制台同款关键结果；这里只展示业务结果，不重复 HTTP 请求和逐行日志</p></div><span class="panel-count">最近 {{ dashboard?.recentTaskRuns?.length || 0 }} 次</span></div>
      <div v-if="dashboard?.recentTaskRuns?.length" class="execution-scroll"><div class="run-head"><span>完成时间</span><span>任务</span><span>账号 / 物品</span><span>关键结果</span><span>状态</span></div><div v-for="run in dashboard.recentTaskRuns" :key="run.id"><time>{{ formatLocal(run.completedAt) }}</time><strong>{{ run.label || run.taskType || "未命名任务" }}</strong><span>{{ [run.accountName, run.marketHashName, run.operationId ? `GD-${run.operationId}` : ""].filter(Boolean).join(" · ") || "全局" }}</span><p>{{ run.error || run.summary || "任务已完成" }}</p><b :class="run.ok ? 'success-text' : 'danger-text'">{{ run.ok ? "完成" : "失败" }}</b></div></div>
      <div v-else class="empty-state">后续每次统一调度任务完成后，都会在这里留下简洁的执行摘要。</div>
      <details class="upcoming-tasks"><summary>查看下一批等待任务（{{ dashboard?.taskQueue?.length || 0 }}）</summary><div v-if="dashboard?.taskQueue?.length" class="task-list"><div class="task-head"><span>执行时间</span><span>任务</span><span>物品</span><span>账号</span><span>原因 / 状态</span></div><div v-for="task in dashboard.taskQueue" :key="task.id"><time>{{ formatLocal(task.nextAttemptAt) }}</time><strong>{{ task.label || task.taskType || "未命名任务" }}</strong><span>{{ task.marketHashName || "—" }}</span><span>{{ task.accountName || "—" }}</span><b>{{ task.reason || task.lastError || formatCountdown(task.nextAttemptAt) || task.status }}</b></div></div><div v-else class="empty-state small">当前没有等待执行的挂刀任务。</div></details>
    </section>

    <section class="panel scan-panel">
      <div class="section-title"><div><h2>每轮完整扫描</h2><p>查看评估品类、挂刀候选、真实可执行库存、比例与未执行原因</p></div><span class="panel-count">最近 {{ dashboard?.scanRounds?.length || 0 }} 轮</span></div>
      <div v-if="dashboard?.scanRounds?.length" class="scan-rounds">
        <article v-for="round in dashboard.scanRounds" :key="round.generatedAt">
          <button class="scan-round-summary" type="button" @click="expandedScanAt = expandedScanAt === round.generatedAt ? null : (round.generatedAt || null)">
            <time>{{ formatLocal(round.generatedAt) }}</time>
            <span><b>{{ round.evaluatedCount || 0 }}</b><small>完成评估</small></span>
            <span><b>{{ round.candidateCount || 0 }}</b><small>符合比例</small></span>
            <span><b>{{ round.executableCount || 0 }}</b><small>本地可执行</small></span>
            <span><b>{{ scanNotEvaluatedCount(round) }}</b><small>未评估明细</small></span>
            <span><b>{{ round.listedCount || 0 }}</b><small>新上架</small></span>
            <strong :class="round.blockedByOpenCycle ? 'warning-text' : 'success-text'">{{ round.blockedByOpenCycle ? "等待旧流水" : "扫描完成" }}</strong>
            <FolioIcon :name="expandedScanAt === round.generatedAt ? 'chevron-up' : 'chevron-down'" :size="15" />
          </button>
          <div v-if="expandedScanAt === round.generatedAt" class="scan-detail">
            <div class="scan-meta">
              <span>库存源 {{ round.inventorySource || "未知" }}</span>
              <span>底仓 {{ round.poolTypeCount || 0 }} 类</span>
              <span>{{ round.outcomeCounts ? "真实缺价" : "历史缺价计数" }} {{ round.missingPriceCount || 0 }} 类</span>
              <span v-if="round.outcomeCounts">Steam 队列顺延 {{ round.queueDeferredCount || 0 }} 类</span>
              <span>全局上限 {{ round.globalMaxListingRatioPct?.toFixed(2) || "—" }}%</span>
              <span v-for="(count, reason) in round.decisionCounts" :key="reason">{{ scanDecisionLabel(reason) }} {{ count }}</span>
            </div>
            <div class="scan-items">
              <div class="scan-item-head"><span>物品</span><span>Steam挂价 / 税后</span><span>C5补仓价</span><span>挂刀比例 / 上限</span><span>库存</span><span>结论与原因</span></div>
              <div v-for="item in round.items" :key="item.marketHashName">
                <span><strong>{{ item.name || item.marketHashName }}</strong><small>{{ item.marketHashName }}</small></span>
                <span>¥{{ item.steamListPrice?.toFixed(2) || "—" }} / ¥{{ item.steamNetAmount?.toFixed(2) || "—" }}</span>
                <span>¥{{ item.c5RebuyPrice?.toFixed(2) || "—" }}</span>
                <span><b>{{ item.listingRatioPct?.toFixed(2) || "—" }}%</b> / {{ item.maxListingRatioPct?.toFixed(2) || "—" }}%</span>
                <span v-if="item.decision === 'executable_candidate' || item.decision === 'ratio_above_limit' || item.decision === 'no_local_executable_asset' || item.decision === 'waiting_existing_cycle'">C5 {{ item.c5TradableCount || 0 }}件 · 本地 {{ item.localExecutableCount || 0 }}件<small>{{ accountInventoryLabel(item) }}</small></span>
                <span v-else class="muted-text">{{ item.requestSent === false ? "未发送 Steam HTTP" : "行情阶段" }}<small>{{ item.stage || "—" }}</small></span>
                <span class="scan-result"><strong :class="item.decision === 'executable_candidate' ? 'success-text' : (item.decision === 'request_failed' || item.decision === 'market_state_missing' ? 'danger-text' : 'muted-text')">{{ scanDecisionLabel(item.decision) }}</strong><small v-if="item.reason">{{ item.reason }}</small></span>
              </div>
            </div>
            <p v-if="round.itemsTruncated" class="scan-truncated">本轮品类较多，仅保留 80 个明细；顶部统计仍覆盖完整扫描。</p>
          </div>
        </article>
      </div>
      <div v-else class="empty-state">下一轮挂刀完整扫描结束后，会在这里保存结构化扫描结果。</div>
    </section>

    <section class="overview-two-column">
      <article class="panel mini-panel special-rules-panel"><div class="section-title compact"><h2>特殊箱子比例规则</h2><RouterLink to="/guadao/settings">查看策略设置</RouterLink></div><p class="global-ratio">全局最大挂刀比例 <strong>{{ dashboard?.settingsSummary?.guadaoMaxListingRatio == null ? "—" : `${(dashboard.settingsSummary.guadaoMaxListingRatio * 100).toFixed(2)}%` }}</strong></p><div v-if="dashboard?.specialRules?.length" class="mini-list"><div v-for="rule in dashboard.specialRules.slice(0, 4)" :key="rule.id"><span>{{ rule.displayName || rule.marketHashName }}</span><small>{{ rule.currentRatioPct == null ? "最近观测 —" : `最近观测 ${rule.currentRatioPct.toFixed(2)}% · ${formatLocal(rule.currentRatioObservedAt)}` }} · {{ rule.enabled === false ? "已停用" : "专用规则" }}</small><strong>{{ rule.maxRatioPct?.toFixed(2) }}%</strong></div></div><div v-else class="empty-state small">尚未配置特殊箱子比例规则；所有箱子使用全局上限。</div></article>
      <article class="panel mini-panel"><div class="section-title compact"><h2>异常与待处理</h2><RouterLink to="/guadao/issues">查看全部</RouterLink></div><div v-if="dashboard?.issues?.length" class="mini-list"><div v-for="issue in dashboard.issues.slice(0, 3)" :key="issue.id || issue.issueId"><span>{{ issue.title || issue.issueType || issue.reason || issue.status || "待处理问题" }}</span><strong>{{ issue.severity || issue.status || "待处理" }}</strong></div></div><div v-else class="empty-state small">当前没有需要人工处理的问题。</div></article>
    </section>

    <article class="panel scheduler-panel"><div class="section-title compact"><div><h2>共享 Steam 请求调度</h2><p>已下移到运营信息之后；安静窗口给 Profit listings 让路，429 熔断负责限流冷却</p></div><div class="scheduler-heading-actions"><span :class="['pill', dashboard?.steamScheduler?.status === 'healthy' ? 'success' : 'neutral']">{{ dashboard?.steamScheduler?.status || "状态未知" }}</span><RouterLink to="/guadao/logs?includeSteamScheduler=true">查看请求日志</RouterLink></div></div><div class="scheduler-layout"><dl class="scheduler-summary"><div><dt>排队请求</dt><dd>{{ dashboard?.steamScheduler?.queueLength ?? "—" }}</dd></div><div><dt>当前请求</dt><dd>{{ dashboard?.steamScheduler?.activeRequest || "无" }}</dd></div><div><dt>请求/分钟</dt><dd>{{ dashboard?.steamScheduler?.requestsPerMinute ?? "—" }}</dd></div><div><dt>429 冷却结束</dt><dd>{{ formatLocal(routeCooldownUntil) }}</dd></div></dl><section><div class="quiet-window-state"><div><strong>Profit listings 安静窗口</strong><span>暂停 P2 / P3，P0 安全终态仍可执行</span></div><b :class="quietWindow ? 'active' : ''">{{ quietWindow ? `进行中 · ${formatCountdown(quietWindow.cooldownUntil)}` : "当前未启用" }}</b></div><div v-if="dashboard?.steamScheduler?.priorities?.length" class="priority-list"><div v-for="row in dashboard.steamScheduler.priorities" :key="row.priority"><b>{{ row.priority }}</b><span>{{ row.label }}</span><strong>{{ row.queued || 0 }}</strong></div></div></section><section class="circuit-section"><div class="circuit-heading"><strong>429 熔断明细</strong><span>{{ activeCircuits.length ? `${activeCircuits.length} 条冷却中` : "当前无 429 熔断" }}</span></div><div v-if="activeCircuits.length" class="circuit-list"><article v-for="row in activeCircuits" :key="row.circuitKey || `${row.accountId}-${row.route}`"><header><strong>{{ row.accountId || "全账号" }}</strong><span :class="['pill', row.state === 'half_open' ? 'warning' : 'neutral']">{{ row.state === "half_open" ? "恢复探测" : "冷却中" }}</span></header><p class="mono">{{ row.route || "全局 Steam 请求" }}</p><dl><div><dt>连续 429</dt><dd>{{ row.consecutive429 || 0 }} 次</dd></div><div><dt>最后 429</dt><dd>{{ formatLocal(row.last429At) }}</dd></div><div><dt>剩余冷却</dt><dd>{{ formatCountdown(row.cooldownUntil || row.nextProbeAt) }}</dd></div><div><dt>下次探测</dt><dd>{{ formatLocal(row.nextProbeAt) }}</dd></div></dl></article></div></section></div></article>

    <div v-if="confirmAction" class="modal-backdrop" @click.self="confirmAction = null"><section class="confirm-dialog" role="dialog" aria-modal="true"><span class="dialog-icon"><FolioIcon :name="confirmAction === 'disable' ? 'warning' : 'shield'" :size="22" /></span><h2>{{ confirmCopy.title }}</h2><p>{{ confirmCopy.text }}</p><div><button class="secondary-button" type="button" :disabled="actionBusy" @click="confirmAction = null">取消</button><button :class="confirmAction === 'disable' ? 'danger-button' : 'primary-button'" type="button" :disabled="actionBusy" @click="submitConfirmed">{{ actionBusy ? "提交中…" : confirmCopy.button }}</button></div></section></div>
  </main>
</template>

<style scoped>
.overview-page{width:min(1320px,calc(100vw - 44px));gap:14px}.overview-heading{display:grid;grid-template-columns:minmax(0,1fr) 500px;gap:28px;align-items:center;padding:8px 0 2px}.overview-heading h1{margin:0;font-size:32px;letter-spacing:-.045em}.overview-heading>div>p:last-child{margin:7px 0 0;color:var(--folio-muted);font-size:13px}.runtime-card{border:1px solid var(--folio-line);border-radius:17px;padding:15px 17px;background:#fff;box-shadow:var(--folio-shadow)}.runtime-card-top,.runtime-card-top>div,.runtime-actions,.cookie-summary{display:flex;align-items:center}.runtime-card-top{justify-content:space-between}.runtime-card-top>div{gap:10px}.runtime-state{color:var(--folio-muted);font-size:11px}.runtime-state.on{color:var(--folio-green)}.runtime-card p{display:flex;align-items:center;gap:6px;margin:10px 0;color:var(--folio-muted);font-size:11px}.runtime-actions{justify-content:flex-end;gap:8px}.runtime-actions button{min-height:33px;padding:6px 10px}.switch{position:relative;width:60px;height:30px;border:0;border-radius:999px;padding:0;color:#fff;background:#aab2ad}.switch span{position:absolute;top:4px;left:4px;width:22px;height:22px;border-radius:50%;background:#fff;transition:transform .18s}.switch b{position:absolute;top:7px;right:9px;font-size:9px}.switch.on{background:var(--folio-green)}.switch.on span{transform:translateX(30px)}.switch.on b{right:auto;left:9px}.migration-banner{display:flex;align-items:center;gap:12px;border:1px solid #d9cb96;border-radius:13px;padding:11px 14px;color:#735b16;background:#fff9e8}.migration-banner>div{display:grid;gap:2px;flex:1}.migration-banner span{font-size:11px}.migration-banner button{border:1px solid #c4a84c;border-radius:9px;padding:8px 12px;color:#654f10;background:#fff;font-size:11px;font-weight:750}.feedback{margin:0;border-radius:10px;padding:9px 12px;font-size:12px}.feedback.error{color:var(--folio-red);background:var(--folio-red-soft)}.feedback.success{color:var(--folio-green);background:var(--folio-green-soft)}.metric-row{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:11px}.metric-row article{min-height:88px;display:grid;align-content:space-between;border:1px solid var(--folio-line);border-radius:15px;padding:14px 16px;background:#fff;box-shadow:var(--folio-shadow)}.metric-row span{color:var(--folio-muted);font-size:11px}.metric-row strong{font-size:24px}.section-title{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:13px}.section-title h2{margin:0;font-size:16px}.section-title p{margin:3px 0 0;color:var(--folio-muted);font-size:10px}.section-title.compact{margin-bottom:10px}.section-title a{color:var(--folio-green);font-size:10px;font-weight:700;text-decoration:none}.cookie-summary{gap:13px;color:var(--folio-green)}.cookie-summary button{display:inline-flex;align-items:center;gap:6px;min-height:34px;padding:6px 10px}.cookie-table{min-width:1000px}.cookie-table td{font-size:11px}.pill{display:inline-flex;border-radius:999px;padding:4px 8px;font-size:9px;font-weight:800}.pill.success{color:var(--folio-green);background:var(--folio-green-soft)}.pill.warning{color:var(--folio-amber);background:var(--folio-amber-soft)}.pill.neutral{color:#66716b;background:#eef1ee}.danger-text{color:var(--folio-red)!important}.empty-state{border:1px dashed #d8ded9;border-radius:11px;padding:24px;color:var(--folio-muted);text-align:center;background:var(--folio-surface-soft);font-size:11px}.empty-state.small{padding:17px 10px}.two-column{display:grid;grid-template-columns:1.06fr .94fr;gap:12px}.task-list{display:grid}.task-list>div{display:grid;grid-template-columns:116px 150px minmax(0,1fr) 72px;gap:8px;align-items:center;border-top:1px solid #edf0ed;padding:8px 2px;font-size:10px}.task-list>div:first-child{border-top:0}.task-list time,.task-list span{color:var(--folio-muted)}.task-list b{color:var(--folio-amber);text-align:right}.scheduler-summary{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin:0}.scheduler-summary div{border:1px solid var(--folio-line);border-radius:10px;padding:9px;background:var(--folio-surface-soft)}.scheduler-summary dt{color:var(--folio-muted);font-size:9px}.scheduler-summary dd{margin:3px 0 0;font-size:11px;font-weight:700}.priority-list{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-top:9px}.priority-list div{display:grid;grid-template-columns:auto 1fr auto;gap:5px;padding:6px;border-radius:8px;background:#f3f6f3;font-size:9px}.priority-list b{color:var(--folio-green)}.circuit-section{margin-top:12px;border-top:1px solid var(--folio-line);padding-top:10px}.circuit-heading,.circuit-list article header{display:flex;align-items:center;justify-content:space-between;gap:8px}.circuit-heading strong{font-size:10px}.circuit-heading>span{color:var(--folio-muted);font-size:8px}.circuit-list{display:grid;gap:7px;margin-top:8px}.circuit-list article{border:1px solid #ead7a4;border-radius:10px;padding:9px;background:#fffaf0}.circuit-list article header strong{font-size:9px}.circuit-list article>p{margin:5px 0 7px;color:#5f6d65;font-size:8px;overflow-wrap:anywhere}.circuit-list dl{display:grid;grid-template-columns:repeat(2,1fr);gap:5px;margin:0}.circuit-list dl div{border-radius:7px;padding:6px;background:rgba(255,255,255,.75)}.circuit-list dt{color:var(--folio-muted);font-size:7px}.circuit-list dd{margin:2px 0 0;font-size:8px;font-weight:700}.three-column{display:grid;grid-template-columns:.85fr 1.05fr 1.3fr;gap:12px}.mini-panel{min-height:176px}.mini-list,.log-preview{display:grid}.mini-list>div,.log-preview>div{display:grid;align-items:center;border-top:1px solid #edf0ed;padding:8px 2px;font-size:10px}.mini-list>div:first-child,.log-preview>div:first-child{border-top:0}.mini-list>div{grid-template-columns:1fr auto;gap:8px}.mini-list strong{color:var(--folio-green)}.log-preview>div{grid-template-columns:108px 80px minmax(0,1fr);gap:7px}.log-preview time,.log-preview span{color:var(--folio-muted)}.log-preview strong{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600}.modal-backdrop{position:fixed;inset:0;z-index:50;display:grid;place-items:center;background:rgba(20,31,25,.34);backdrop-filter:blur(3px)}.confirm-dialog{width:440px;border:1px solid var(--folio-line);border-radius:18px;padding:24px;background:#fff;box-shadow:0 24px 70px rgba(20,59,46,.2)}.dialog-icon{display:grid;place-items:center;width:44px;height:44px;border-radius:12px;color:var(--folio-green);background:var(--folio-green-soft)}.confirm-dialog h2{margin:15px 0 8px;font-size:20px}.confirm-dialog p{margin:0;color:var(--folio-muted);font-size:13px;line-height:1.7}.confirm-dialog>div{display:flex;justify-content:flex-end;gap:9px;margin-top:20px}.danger-button{min-height:40px;border:1px solid var(--folio-red);border-radius:11px;padding:8px 15px;color:#fff;background:var(--folio-red);font-size:12px;font-weight:750}
.runtime-card small{display:block;margin-top:8px;color:var(--folio-muted);font-size:9px;text-align:right}.cookie-progress{height:6px;overflow:hidden;border-radius:99px;background:#edf2ee}.cookie-progress i{display:block;height:100%;border-radius:inherit;background:var(--folio-green);transition:width .2s}.runtime-card .retry-status{color:var(--folio-amber);font-size:9px}.cookie-legend{display:flex;gap:20px;margin-top:10px;border-radius:9px;padding:8px 10px;color:var(--folio-muted);background:var(--folio-surface-soft);font-size:9px}.cookie-legend b{color:var(--folio-ink)}.task-list>div{grid-template-columns:105px 112px minmax(100px,1fr) 90px minmax(110px,1fr)}.task-list .task-head{color:var(--folio-muted);font-size:8px;font-weight:750}.task-list>div b{text-align:left;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.quiet-window-state{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:9px;border:1px solid var(--folio-line);border-radius:9px;padding:8px 10px;background:var(--folio-surface-soft)}.quiet-window-state>div{display:grid;gap:2px}.quiet-window-state strong{font-size:9px}.quiet-window-state span{color:var(--folio-muted);font-size:8px}.quiet-window-state>b{color:var(--folio-muted);font-size:8px}.quiet-window-state>b.active{color:var(--folio-green)}.global-ratio{display:flex;justify-content:space-between;margin:0 0 4px;color:var(--folio-muted);font-size:9px}.global-ratio strong{color:var(--folio-ink)}.special-rules-panel .mini-list>div{grid-template-columns:minmax(0,1fr) auto auto}.special-rules-panel .mini-list small{color:var(--folio-muted);font-size:8px}.log-preview>div{grid-template-columns:100px 72px minmax(0,1fr) 38px}.log-preview .log-head{color:var(--folio-muted);font-size:8px;font-weight:700}.log-preview b{text-align:right}.log-preview b.error{color:var(--folio-red)}
.metric-row{grid-template-columns:repeat(auto-fit,minmax(145px,1fr))}.cookie-title{margin-bottom:0}.cookie-detail{margin-top:13px}.fold-button{display:inline-flex;align-items:center;gap:5px;min-height:34px;border:1px solid var(--folio-line);border-radius:10px;padding:6px 10px;color:var(--folio-muted);background:#fff;font-size:10px;font-weight:700;cursor:pointer}.panel-count{color:var(--folio-green);font-size:10px;font-weight:750}.execution-panel,.scan-panel{min-width:0}.execution-scroll{max-height:410px;overflow:auto;border:1px solid var(--folio-line);border-radius:11px}.execution-scroll>div{display:grid;grid-template-columns:145px 155px minmax(190px,.8fr) minmax(300px,1.6fr) 55px;gap:12px;align-items:center;border-top:1px solid #edf0ed;padding:10px 12px;font-size:10px}.execution-scroll>div:first-child{border-top:0}.execution-scroll .run-head{position:sticky;z-index:2;top:0;color:var(--folio-muted);background:#f7f9f7;font-size:8px;font-weight:750}.execution-scroll time,.execution-scroll span{color:var(--folio-muted)}.execution-scroll p{margin:0;line-height:1.5}.success-text{color:var(--folio-green)!important}.warning-text{color:var(--folio-amber)!important}.muted-text{color:var(--folio-muted)!important}.upcoming-tasks{margin-top:11px;border:1px solid var(--folio-line);border-radius:10px;padding:9px 11px}.upcoming-tasks summary{color:var(--folio-green);font-size:10px;font-weight:750;cursor:pointer}.upcoming-tasks .task-list{max-height:300px;overflow:auto;margin-top:8px}.scan-rounds{max-height:520px;overflow:auto;display:grid;gap:8px}.scan-rounds>article{border:1px solid var(--folio-line);border-radius:12px;overflow:hidden;background:#fff}.scan-round-summary{width:100%;display:grid;grid-template-columns:155px repeat(5,minmax(72px,.55fr)) minmax(110px,.7fr) 22px;gap:9px;align-items:center;border:0;padding:11px 13px;text-align:left;color:var(--folio-ink);background:#fff;cursor:pointer}.scan-round-summary:hover{background:#fbfdfb}.scan-round-summary time{color:var(--folio-muted);font-size:10px}.scan-round-summary span{display:grid;gap:2px}.scan-round-summary span b{font-size:14px}.scan-round-summary span small{color:var(--folio-muted);font-size:8px}.scan-round-summary>strong{font-size:10px}.scan-detail{border-top:1px solid var(--folio-line);padding:12px;background:#fafcfb}.scan-meta{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:10px}.scan-meta span{border-radius:999px;padding:5px 8px;color:var(--folio-muted);background:#edf3ef;font-size:8px}.scan-items{max-height:360px;overflow:auto;border:1px solid var(--folio-line);border-radius:10px;background:#fff}.scan-items>div{display:grid;grid-template-columns:minmax(220px,1.5fr) 145px 100px 130px minmax(210px,1.2fr) 110px;gap:9px;align-items:center;border-top:1px solid #edf0ed;padding:9px 10px;font-size:9px}.scan-items>div:first-child{border-top:0}.scan-items .scan-item-head{position:sticky;z-index:2;top:0;color:var(--folio-muted);background:#f4f7f4;font-size:8px;font-weight:750}.scan-items span{min-width:0}.scan-items span>strong,.scan-items span>small{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.scan-items span>small{margin-top:2px;color:var(--folio-muted);font-size:7px}.scan-truncated{margin:9px 0 0;color:var(--folio-muted);font-size:8px}.overview-two-column{display:grid;grid-template-columns:.9fr 1.1fr;gap:12px}.scheduler-panel{min-width:0}.scheduler-heading-actions{display:flex;align-items:center;gap:10px}.scheduler-layout{display:grid;grid-template-columns:.85fr 1.15fr 1fr;gap:13px;align-items:start}.scheduler-layout .circuit-section{margin-top:0;padding-top:0;border-top:0}.mini-list{display:grid}.mini-list>div{display:grid;align-items:center;border-top:1px solid #edf0ed;padding:8px 2px;font-size:10px}.mini-list>div:first-child{border-top:0}
.scan-items>div{grid-template-columns:minmax(210px,1.35fr) 135px 95px 125px minmax(175px,1fr) minmax(210px,1.25fr)}
.scan-items .scan-result small{overflow:visible;text-overflow:clip;white-space:normal;line-height:1.45}
</style>
