<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { RouterLink, useRoute } from "vue-router";
import FolioIcon from "../components/FolioIcon.vue";
import { formatProfitTradePercentagePoints } from "../components/profit_trade_roi_format";

type Interruption = {
  id: number; tradeNo: string; marketHashName: string; name?: string | null; status: string;
  stepKey: string; stepIndex: number; progressPct?: number; aAssetId?: string | null;
  aSteamId?: string | null; bAssetId?: string | null; steamBuyPrice?: number | null;
  c5ListingPrice?: number | null; expectedRoiPct?: number | null; error?: string | null;
  cancelSource?: string | null; cancelReason?: string | null; requiresManualAction?: boolean;
  purchaseRequestSent?: boolean | null; listingIdObtained?: boolean | null;
  steamOrderbookEvidence?: {
    crossedObserved?: boolean;
    snapshots?: OrderbookSnapshot[];
  };
  acknowledged?: boolean; acknowledgedAt?: string | null; acknowledgementReason?: string | null;
  createdAt: string; updatedAt: string; completedAt?: string | null; note?: Record<string, unknown> | null;
};
type OrderbookSnapshot = {
  stage?: string; observedAt?: string | null; sellerFloorPrice?: number | null;
  sellerFloorCount?: number | null; buyerMaxPrice?: number | null;
  buyerMaxCount?: number | null; crossed?: boolean | null;
};
type StateEvent = {
  id?: number; eventType?: string; statusFrom?: string | null; statusTo?: string | null;
  stepKeyFrom?: string | null; stepKeyTo?: string | null; stepIndexFrom?: number | null;
  stepIndexTo?: number | null; reason?: string | null; createdAt: string; isSnapshot?: boolean;
  isProjected?: boolean; logEventId?: string | null;
  context?: {
    stage?: string; cancelSource?: string; steamBuyMethod?: string;
    steamBuyOrderId?: string | null; steamListingId?: string | null;
    purchaseRequestSent?: boolean; steamOrderbook?: OrderbookSnapshot;
  };
};
type StepCount = { stepKey: string; stepIndex: number; count: number };
type ListingsCircuit = {
  status?: "closed" | "open"; isBlocking?: boolean;
  cooldownUntil?: string | null;
  triggerAccountName?: string | null; triggerAccountId?: string | null;
  consecutive429Count?: number; lastRecoveredAt?: string | null;
};
type InterruptionResponse = {
  items: Interruption[]; total: number; page: number; pageSize: number;
  summary?: { total?: number; stepCounts?: StepCount[] }; stepCounts?: StepCount[];
  listingsCircuit?: ListingsCircuit;
};
type TimelineResponse = { trade: Interruption; events: StateEvent[]; listingsCircuit?: ListingsCircuit };

const steps = [
  { key: "discovered", label: "发现机会", index: 0 }, { key: "audited", label: "审计", index: 1 },
  { key: "asset_locked", label: "锁定 A", index: 2 }, { key: "steam_bought", label: "买入 B", index: 3 },
  { key: "c5_listed", label: "C5 上架", index: 4 }, { key: "c5_sold", label: "C5 售出", index: 5 },
  { key: "settled", label: "收益结算", index: 6 },
] as const;

const route = useRoute();
const rows = ref<Interruption[]>([]);
const total = ref(0);
const page = ref(1);
const pageSize = 20;
const stepCounts = ref<StepCount[]>([]);
const loading = ref(false);
const error = ref("");
const routeTradeNo = typeof route.query.tradeNo === "string" ? route.query.tradeNo : "";
const keywordDraft = ref(routeTradeNo);
const keyword = ref(routeTradeNo);
const from = ref("");
const to = ref("");
const stepKey = ref("");
const status = ref("");
const acknowledged = ref("exclude");
const selected = ref<Interruption | null>(null);
const timeline = ref<StateEvent[]>([]);
const timelineLoading = ref(false);
const timelineError = ref("");
const acknowledgeReason = ref("");
const actionBusy = ref(false);
const actionMessage = ref("");
const listingsCircuit = ref<ListingsCircuit>({ status: "closed", isBlocking: false });
const listingsCooling = computed(() => listingsCircuit.value.status === "open");
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)));

function stepCount(index: number): number {
  return Number(stepCounts.value.find(item => Number(item.stepIndex) === index)?.count) || 0;
}
function statusLabel(value: string): string {
  return ({ cancelled: "已取消", failed: "失败", manual_required: "需人工处理" } as Record<string,string>)[value] || value;
}
function statusClass(value: string): string {
  return value === "manual_required" ? "manual" : value === "failed" ? "failed" : "cancelled";
}
function time(value?: string | null): string {
  if (!value) return "-"; const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("zh-CN", { hour12: false });
}
function apiTime(value: string): string {
  if (!value) return "";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toISOString();
}
function pct(value?: number | null): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "-";
  return formatProfitTradePercentagePoints(value);
}
function money(value?: number | null): string {
  return typeof value === "number" && Number.isFinite(value) ? `¥${value.toFixed(2)}` : "-";
}
function reason(trade: Interruption): string {
  return trade.cancelReason || trade.error || String(trade.note?.cancelReason || trade.note?.manualReviewReason || "未记录明确原因");
}
function note(trade: Interruption, key: string): string {
  const value = trade.note?.[key]; return value === undefined || value === null || value === "" ? "-" : String(value);
}
function evidenceBoolean(value: unknown): boolean | null {
  if (typeof value === "boolean") return value;
  if (typeof value === "number" && (value === 0 || value === 1)) return value === 1;
  if (typeof value === "string") {
    const normalized = value.trim().toLowerCase();
    if (["true", "1", "yes", "sent", "obtained"].includes(normalized)) return true;
    if (["false", "0", "no", "not_sent", "not_obtained"].includes(normalized)) return false;
  }
  return null;
}
function evidenceLabel(trade: Interruption, field: "purchaseRequestSent"|"listingIdObtained"): string {
  const topLevel = evidenceBoolean(trade[field]);
  const resolved = topLevel ?? evidenceBoolean(trade.note?.[field]);
  if (resolved === null) return "未记录";
  if (field === "purchaseRequestSent") return resolved ? "已发送" : "未发送";
  return resolved ? "已取得" : "未取得";
}
function crossedObserved(trade: Interruption): boolean {
  return trade.steamOrderbookEvidence?.crossedObserved === true;
}
function orderbookSnapshots(trade: Interruption): OrderbookSnapshot[] {
  return Array.isArray(trade.steamOrderbookEvidence?.snapshots)
    ? trade.steamOrderbookEvidence!.snapshots!
    : [];
}
function stageLabel(stage?: string): string {
  return ({
    scan: "扫描发现", pre_buy: "购买前",
    after_listings_400: "listings 400 后重查",
    after_listings_429: "listings 429 后重查",
    buy_retry: "求购重试前", account_change: "切换账号后",
  } as Record<string,string>)[String(stage || "")] || String(stage || "盘口快照");
}
function eventLabel(event: StateEvent): string {
  return ({
    created: "流水创建", transition: "状态推进",
    orderbook_snapshot: "Steam 盘口快照",
    steam_purchase_requested: "Steam 购买请求已发送",
    steam_purchase_request_returned: "Steam 购买请求已返回",
    steam_buy_order_cancelled: "Steam 求购已撤销",
    historical_snapshot: "历史状态快照",
  } as Record<string,string>)[String(event.eventType || "")]
    || event.eventType
    || (event.isSnapshot ? "历史快照" : "状态迁移");
}
function eventOrderbook(event: StateEvent): OrderbookSnapshot | null {
  return event.context?.steamOrderbook || null;
}
function isListings429Interruption(trade: Interruption): boolean {
  const source = String(trade.cancelSource || trade.note?.cancelSource || "").toLowerCase();
  const detail = `${reason(trade)} ${String(trade.note?.searchListingsError || "")}`.toLowerCase();
  return source.includes("search_listings_429") || source.includes("listings_circuit") || detail.includes("429");
}
async function responseError(response: Response): Promise<string> {
  try { const payload = await response.json() as { error?: string; detail?: string }; return payload.error || payload.detail || response.statusText; }
  catch { return response.statusText; }
}

async function load(): Promise<void> {
  loading.value = true; error.value = "";
  const query = new URLSearchParams({ page: String(page.value), pageSize: String(pageSize), acknowledged: acknowledged.value });
  if (keyword.value) query.set("keyword", keyword.value); if (from.value) query.set("from", apiTime(from.value));
  if (to.value) query.set("to", apiTime(to.value)); if (stepKey.value) query.set("stepKey", stepKey.value);
  if (status.value) query.set("status", status.value);
  try {
    const response = await fetch(`/api/profit-trade/interruptions?${query}`, { cache: "no-store" });
    if (!response.ok) throw new Error(await responseError(response));
    const payload = await response.json() as InterruptionResponse;
    rows.value = Array.isArray(payload.items) ? payload.items : []; total.value = Number(payload.total) || 0;
    stepCounts.value = payload.summary?.stepCounts || payload.stepCounts || [];
    listingsCircuit.value = payload.listingsCircuit || { status: "closed", isBlocking: false };
    const queryTradeNo = typeof route.query.tradeNo === "string" ? route.query.tradeNo : "";
    const nextSelected = rows.value.find(item => item.tradeNo === queryTradeNo)
      || rows.value.find(item => item.id === selected.value?.id) || rows.value[0] || null;
    if (nextSelected) await selectTrade(nextSelected); else { selected.value = null; timeline.value = []; }
  } catch (cause) {
    rows.value = []; total.value = 0; stepCounts.value = []; selected.value = null; timeline.value = [];
    error.value = `中断记录读取失败：${cause instanceof Error ? cause.message : String(cause)}`;
  } finally { loading.value = false; }
}
function search(): void { keyword.value = keywordDraft.value.trim(); page.value = 1; void load(); }
function reset(): void { keywordDraft.value = ""; keyword.value = ""; from.value = ""; to.value = ""; stepKey.value = ""; status.value = ""; acknowledged.value = "exclude"; page.value = 1; void load(); }
function turn(direction: -1|1): void { const next = page.value + direction; if (next >= 1 && next <= totalPages.value) { page.value = next; void load(); } }
async function selectTrade(trade: Interruption): Promise<void> {
  selected.value = trade; timelineLoading.value = true; timelineError.value = ""; actionMessage.value = "";
  try {
    const response = await fetch(`/api/profit-trade/interruptions/timeline?tradeId=${encodeURIComponent(trade.id)}`, { cache: "no-store" });
    if (!response.ok) throw new Error(await responseError(response));
    const payload = await response.json() as TimelineResponse;
    selected.value = payload.trade || trade; timeline.value = Array.isArray(payload.events) ? payload.events : [];
    listingsCircuit.value = payload.listingsCircuit || listingsCircuit.value;
  } catch (cause) { timeline.value = []; timelineError.value = `时间线读取失败：${cause instanceof Error ? cause.message : String(cause)}`; }
  finally { timelineLoading.value = false; }
}
async function setAcknowledged(action: "acknowledge"|"restore"): Promise<void> {
  if (!selected.value || actionBusy.value) return; actionBusy.value = true; actionMessage.value = "";
  try {
    const response = await fetch("/api/profit-trade/interruptions/acknowledge", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ tradeId: selected.value.id, action, reason: acknowledgeReason.value.trim() }) });
    if (!response.ok) throw new Error(await responseError(response));
    const successMessage = action === "acknowledge" ? "已知晓；审计记录仍保留。" : "已恢复到默认问题列表。";
    acknowledgeReason.value = ""; await load(); actionMessage.value = successMessage;
  } catch (cause) { actionMessage.value = `操作未完成：${cause instanceof Error ? cause.message : String(cause)}`; }
  finally { actionBusy.value = false; }
}
watch([from, to, stepKey, status, acknowledged], () => { page.value = 1; void load(); });
watch(() => route.query.tradeNo, value => {
  if (typeof value !== "string" || value === keyword.value) return;
  keywordDraft.value = value;
  keyword.value = value;
  page.value = 1;
  void load();
});
onMounted(() => void load());
</script>

<template>
  <main class="interruptions-page">
    <header class="page-title"><div><p class="eyebrow">S2 · 状态机审计</p><h1>未完成与中断追踪</h1><p>展示曾经开始推进、但未完成收益结算的取消、失败和人工处理流水；原进行中流水仍保留在总览。</p></div><button class="secondary-button refresh" type="button" @click="load"><FolioIcon name="refresh" :size="15" />刷新</button></header>

    <section class="step-summary" aria-label="各步骤中断数量">
      <button v-for="step in steps" :key="step.key" type="button" :class="{ active: stepKey === step.key }" @click="stepKey = stepKey === step.key ? '' : step.key"><span>{{ step.index + 1 }}</span><div><strong>{{ stepCount(step.index) }}</strong><small>{{ step.label }}未完成</small></div></button>
    </section>

    <form class="filter-bar" @submit.prevent="search">
      <label class="keyword"><span>交易号或品类</span><input v-model="keywordDraft" type="search" placeholder="tradeNo / 中文名 / marketHashName"></label>
      <label><span>开始时间</span><input v-model="from" type="datetime-local"></label><label><span>结束时间</span><input v-model="to" type="datetime-local"></label>
      <label><span>终态</span><select v-model="status"><option value="">全部</option><option value="cancelled">已取消</option><option value="failed">失败</option><option value="manual_required">需人工处理</option></select></label>
      <label><span>知晓状态</span><select v-model="acknowledged"><option value="exclude">默认：排除已知晓</option><option value="include">包含已知晓</option><option value="only">只看已知晓</option></select></label>
      <button class="primary-button" type="submit">查询</button><button class="secondary-button" type="button" @click="reset">重置</button>
    </form>
    <p v-if="error" class="page-error">{{ error }}。不会使用静态数据替代。</p>
    <p v-if="actionMessage" class="action-message global-message">{{ actionMessage }}</p>
    <section v-if="listingsCooling" class="interruption-circuit">
      <div><strong>Steam listings 冷却仍在生效</strong><span>中断流水永久保留；恢复的是查询路由，不会把旧流水改成成功。</span></div>
      <dl><div><dt>触发账号</dt><dd>{{ listingsCircuit.triggerAccountName || listingsCircuit.triggerAccountId || "-" }}</dd></div><div><dt>连续429</dt><dd>{{ listingsCircuit.consecutive429Count || 0 }}次</dd></div><div><dt>冷却结束</dt><dd>{{ time(listingsCircuit.cooldownUntil) }}</dd></div></dl>
    </section>

    <section class="master-detail">
      <aside class="trade-list panel">
        <header><div><h2>问题流水</h2><span>{{ total }} 笔</span></div><small>按最近中断时间排序</small></header>
        <div v-if="loading" class="empty">正在读取…</div><div v-else-if="rows.length === 0" class="empty">当前筛选条件下没有中断流水。</div>
        <button v-for="trade in rows" v-else :key="trade.id" type="button" :class="['trade-item',{ selected: selected?.id === trade.id }]" @click="selectTrade(trade)">
          <div class="trade-item-head"><span :class="['status',statusClass(trade.status)]">{{ statusLabel(trade.status) }}</span><time>{{ time(trade.completedAt || trade.updatedAt) }}</time></div>
          <strong>{{ trade.name || trade.marketHashName }}</strong><small>{{ trade.tradeNo }}</small>
          <div class="stop-line"><span>停在 {{ steps[trade.stepIndex]?.label || trade.stepKey }}</span><em v-if="trade.acknowledged">已知晓</em></div>
          <span v-if="crossedObserved(trade)" class="crossed-warning compact">曾出现交叉盘口 · 可能滞后</span>
          <p>{{ reason(trade) }}</p>
        </button>
        <footer class="list-pagination"><button type="button" :disabled="page <= 1" @click="turn(-1)">上一页</button><span>{{ page }} / {{ totalPages }}</span><button type="button" :disabled="page >= totalPages" @click="turn(1)">下一页</button></footer>
      </aside>

      <article class="detail panel">
        <div v-if="!selected" class="empty large">选择一笔流水查看完整停止现场。</div>
        <template v-else>
          <header class="detail-head"><div><div class="detail-badges"><span :class="['status',statusClass(selected.status)]">{{ statusLabel(selected.status) }}</span><span>停在步骤 {{ selected.stepIndex + 1 }}</span><span v-if="crossedObserved(selected)" class="crossed-warning">曾出现交叉盘口 · 可能滞后</span><span v-if="selected.acknowledged">已知晓</span></div><h2>{{ selected.name || selected.marketHashName }}</h2><p>{{ selected.marketHashName }}</p><small>{{ selected.tradeNo }}</small></div><RouterLink :to="{ path:'/profit-trade/logs', query:{ tradeNo:selected.tradeNo } }" class="log-link"><FolioIcon name="link" :size="15" />查看关联日志</RouterLink></header>

          <section class="process"><div v-for="step in steps" :key="step.key" :class="step.index < selected.stepIndex ? 'done' : step.index === selected.stepIndex ? 'stopped' : 'pending'"><span>{{ step.index + 1 }}</span><strong>{{ step.label }}</strong><small>{{ step.index < selected.stepIndex ? "已完成" : step.index === selected.stepIndex ? "停止位置" : "未开始" }}</small></div></section>

          <section class="stop-evidence"><div><p>停止原因</p><strong>{{ reason(selected) }}</strong></div><dl><div><dt>中断来源</dt><dd>{{ selected.cancelSource || note(selected,"cancelSource") }}</dd></div><div><dt>中断时间</dt><dd>{{ time(selected.completedAt || selected.updatedAt) }}</dd></div><div><dt>A assetId</dt><dd>{{ selected.aAssetId || "-" }}</dd></div><div><dt>A Steam账号</dt><dd>{{ selected.aSteamId || "-" }}</dd></div><div><dt>B assetId</dt><dd>{{ selected.bAssetId || "未获得" }}</dd></div><div><dt>Steam 买入价</dt><dd>{{ money(selected.steamBuyPrice) }}</dd></div><div><dt>预计 ROI</dt><dd>{{ pct(selected.expectedRoiPct) }}</dd></div><div><dt>listingId 获取</dt><dd>{{ evidenceLabel(selected,"listingIdObtained") }}</dd></div><div><dt>购买请求</dt><dd>{{ evidenceLabel(selected,"purchaseRequestSent") }}</dd></div></dl></section>
          <section v-if="orderbookSnapshots(selected).length" class="orderbook-evidence">
            <header><strong>Steam 盘口证据</strong><span>来自流水永久保存的同次 orderbook 快照</span></header>
            <div class="orderbook-grid">
              <article v-for="(snapshot,index) in orderbookSnapshots(selected)" :key="`${snapshot.stage}-${snapshot.observedAt}-${index}`" :class="{ crossed:snapshot.crossed }">
                <div><strong>{{ stageLabel(snapshot.stage) }}</strong><time>{{ time(snapshot.observedAt) }}</time></div>
                <p>Steam 买入 {{ money(snapshot.sellerFloorPrice) }} · 最高求购 {{ money(snapshot.buyerMaxPrice) }}</p>
                <span v-if="snapshot.crossed">盘口交叉 · 该公开价格可能已经滞后</span>
              </article>
            </div>
          </section>
          <section v-if="isListings429Interruption(selected)" class="listings-evidence">
            <strong>这笔流水没有取得 listingId，也没有发送新的 Steam 购买请求。</strong>
            <span v-if="listingsCooling">冷却将在 {{ time(listingsCircuit.cooldownUntil) }} 自动结束，不会额外发送恢复探测；旧中断流水不会复用，新机会会重新评估并可改走安全求购。</span>
            <span v-else>Steam listings 路由当前已恢复；这张卡片仍作为历史中断保留，后续机会会重新评估。</span>
          </section>

          <section class="timeline"><div class="section-heading"><h3>永久状态时间线</h3><span>{{ timeline.length }} 条事件</span></div><p v-if="timelineError" class="page-error">{{ timelineError }}</p><div v-if="timelineLoading" class="empty">正在读取时间线…</div><div v-else-if="timeline.length === 0" class="empty">历史流水只有当前快照，未伪造缺失的中间事件。</div><ol v-else><li v-for="(event,index) in timeline" :key="event.id || index" :class="{ snapshot:event.isSnapshot }"><span></span><div><header><strong>{{ eventLabel(event) }}</strong><time>{{ time(event.createdAt) }}</time></header><p v-if="eventOrderbook(event)">Steam 买入 {{ money(eventOrderbook(event)?.sellerFloorPrice) }} · 最高求购 {{ money(eventOrderbook(event)?.buyerMaxPrice) }} · {{ stageLabel(event.context?.stage) }}</p><p v-else>{{ event.statusFrom || "-" }} → {{ event.statusTo || "-" }} · {{ event.stepKeyFrom || "-" }} → {{ event.stepKeyTo || "-" }}</p><small>{{ event.reason || "未记录补充原因" }}<template v-if="event.isProjected"> · 来自流水持久化证据</template></small></div></li></ol></section>

          <section class="acknowledge"><div><h3>{{ selected.acknowledged ? "恢复问题记录" : "知晓并隐藏" }}</h3><p>只改变默认问题列表显示，不删除流水、时间线或日志；关联远端 Steam 订单终态不明确时，后端会拒绝操作。</p></div><input v-if="!selected.acknowledged" v-model="acknowledgeReason" type="text" placeholder="知晓原因（可选）"><button v-if="selected.acknowledged" class="secondary-button" type="button" :disabled="actionBusy" @click="setAcknowledged('restore')">恢复到默认列表</button><button v-else class="secondary-button danger" type="button" :disabled="actionBusy" @click="setAcknowledged('acknowledge')">知晓并隐藏</button></section>
        </template>
      </article>
    </section>
  </main>
</template>

<style scoped>
.interruptions-page{width:min(1280px,calc(100vw - 40px));margin:0 auto;padding:22px 0 38px;display:grid;gap:14px;color:#17201c}.page-title{display:flex;justify-content:space-between;align-items:flex-start}.page-title h1{margin:0;font-size:24px}.page-title p:not(.eyebrow){margin:6px 0;color:#6f7872;font-size:12px}.refresh,.log-link{display:inline-flex;align-items:center;gap:6px}
.step-summary{display:grid;grid-template-columns:repeat(7,1fr);gap:8px}.step-summary button{display:flex;align-items:center;gap:9px;padding:11px;border:1px solid #dfe5df;border-radius:8px;text-align:left;color:#27342d;background:#fff}.step-summary button>span{display:grid;place-items:center;width:25px;height:25px;border-radius:50%;color:#647068;background:#edf1ed;font-size:11px}.step-summary button div{display:grid}.step-summary strong{font-size:17px}.step-summary small{color:#77817b;font-size:10px}.step-summary button.active{border-color:#75a68d;background:#edf6f0}.step-summary button.active>span{color:#fff;background:#236a4c}
.filter-bar{display:grid;grid-template-columns:minmax(210px,1.5fr) repeat(4,minmax(125px,1fr)) auto auto;gap:8px;align-items:end;padding:12px;border:1px solid #dfe5df;border-radius:9px;background:#fff}.filter-bar label{display:grid;gap:4px;min-width:0}.filter-bar label span{color:#6f7872;font-size:10px;font-weight:650}.filter-bar input,.filter-bar select{min-width:0;min-height:35px;border:1px solid #d6ddd6;border-radius:6px;padding:6px 8px;background:#fff}.page-error{margin:0;padding:9px 11px;border:1px solid #e6b6af;border-radius:7px;color:#8c382f;background:#fff7f5;font-size:12px}
.interruption-circuit{display:flex;align-items:center;justify-content:space-between;gap:18px;padding:11px 13px;border:1px solid #dfc77e;border-radius:8px;color:#5f5127;background:#fff9e9}.interruption-circuit>div{display:grid;gap:2px}.interruption-circuit strong{font-size:12px}.interruption-circuit span{color:#786b43;font-size:10px}.interruption-circuit dl{display:flex;gap:16px;margin:0}.interruption-circuit dl>div{display:grid}.interruption-circuit dt{color:#8a7d53;font-size:9px}.interruption-circuit dd{margin:1px 0 0;font-size:10px;font-weight:700;white-space:nowrap}
.master-detail{display:grid;grid-template-columns:370px minmax(0,1fr);gap:12px;align-items:start}.trade-list,.detail{padding:0;border-color:#dfe5df;overflow:hidden}.trade-list>header{display:flex;justify-content:space-between;align-items:end;padding:14px;border-bottom:1px solid #e3e8e3}.trade-list>header>div{display:flex;align-items:baseline;gap:8px}.trade-list h2{margin:0;font-size:16px}.trade-list header span,.trade-list header small{color:#77817b;font-size:11px}.trade-item{display:grid;width:100%;gap:5px;padding:12px 14px;border:0;border-bottom:1px solid #e8ece8;text-align:left;color:#17201c;background:#fff}.trade-item:hover,.trade-item.selected{background:#f0f6f2}.trade-item.selected{box-shadow:inset 3px 0 #236a4c}.trade-item-head,.stop-line{display:flex;justify-content:space-between;gap:8px;align-items:center}.trade-item time,.trade-item small{color:#7a837e;font-size:10px}.trade-item>strong{overflow-wrap:anywhere}.trade-item>p{margin:0;color:#735148;font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.stop-line span{color:#4f5e56;font-size:11px;font-weight:650}.stop-line em{color:#68726c;font-size:9px;font-style:normal}.status{padding:3px 6px;border-radius:999px;font-size:9px;font-weight:750}.status.cancelled{color:#6b5b31;background:#f6f0dc}.status.failed{color:#923c33;background:#fbe9e6}.status.manual{color:#8a4b26;background:#faebdf}.list-pagination{display:flex;justify-content:space-between;align-items:center;padding:10px 14px}.list-pagination button{border:1px solid #d8ded8;border-radius:5px;padding:4px 8px;background:#fff}.list-pagination span{color:#78817c;font-size:10px}.empty{display:grid;place-items:center;min-height:90px;color:#7a837e;font-size:12px}.empty.large{min-height:520px}
.detail{padding:17px}.detail-head{display:flex;justify-content:space-between;gap:20px;padding-bottom:14px;border-bottom:1px solid #e2e7e2}.detail-badges{display:flex;gap:6px;align-items:center}.detail-badges>span:not(.status){padding:3px 6px;border-radius:999px;color:#66716b;background:#edf1ed;font-size:9px}.detail-head h2{margin:8px 0 2px;font-size:19px}.detail-head p,.detail-head small{margin:0;color:#77817b;font-size:11px}.log-link{height:max-content;padding:7px 10px;border:1px solid #b8cfc1;border-radius:7px;color:#205f45;text-decoration:none;background:#f2f8f4;font-size:11px;font-weight:700}
.crossed-warning{display:inline-flex;width:max-content;padding:3px 7px;border:1px solid #dfc77e;border-radius:999px;color:#695515!important;background:#fff4cd!important;font-size:9px;font-weight:750}.crossed-warning.compact{padding:2px 6px}
.process{display:grid;grid-template-columns:repeat(7,1fr);gap:4px;margin:17px 0}.process>div{position:relative;display:grid;justify-items:center;gap:3px;text-align:center}.process>div:not(:last-child)::after{content:"";position:absolute;left:64%;right:-36%;top:12px;height:2px;background:#dfe4df}.process>div.done::after{background:#7dac93}.process>div>span{z-index:1;display:grid;place-items:center;width:25px;height:25px;border:2px solid #d9dfda;border-radius:50%;color:#738079;background:#fff;font-size:10px}.process>div.done>span{border-color:#2f7956;color:#fff;background:#2f7956}.process>div.stopped>span{border-color:#b7564b;color:#b0443a;background:#fff0ed}.process strong{font-size:10px}.process small{color:#7b847f;font-size:9px}.stop-evidence{padding:13px;border:1px solid #ead8d2;border-radius:8px;background:#fff9f7}.stop-evidence>div>p{margin:0;color:#8a5b51;font-size:10px}.stop-evidence>div>strong{color:#7f372f;font-size:12px}.stop-evidence dl{display:grid;grid-template-columns:repeat(4,1fr);gap:9px;margin:12px 0 0}.stop-evidence dt{color:#7b847f;font-size:9px}.stop-evidence dd{margin:2px 0 0;font-size:10px;font-weight:650;overflow-wrap:anywhere}
.orderbook-evidence{display:grid;gap:9px;margin-top:8px;padding:11px 12px;border:1px solid #d9e3db;border-radius:8px;background:#f8fbf8}.orderbook-evidence>header{display:flex;justify-content:space-between;gap:12px}.orderbook-evidence>header strong{font-size:11px}.orderbook-evidence>header span{color:#77817b;font-size:9px}.orderbook-grid{display:grid;gap:7px}.orderbook-grid>article{display:grid;grid-template-columns:minmax(90px,.7fr) repeat(2,minmax(100px,1fr)) minmax(150px,1.5fr);gap:10px;align-items:center;padding-top:7px;border-top:1px solid #e1e8e2}.orderbook-grid strong{font-size:10px}.orderbook-grid span{color:#58655e;font-size:10px}.orderbook-grid em{color:#8a6a1e;font-size:9px;font-style:normal;font-weight:700}
.listings-evidence{display:grid;gap:4px;margin-top:8px;padding:10px 12px;border:1px solid #dfc77e;border-radius:8px;color:#604f20;background:#fff9e9}.listings-evidence strong{font-size:11px}.listings-evidence span{color:#786b43;font-size:10px}
.timeline{margin-top:16px}.section-heading{display:flex;justify-content:space-between}.section-heading h3,.acknowledge h3{margin:0;font-size:14px}.section-heading span{color:#7a837e;font-size:10px}.timeline ol{display:grid;gap:0;margin:12px 0;padding:0;list-style:none}.timeline li{display:grid;grid-template-columns:18px 1fr;gap:8px;min-height:62px}.timeline li>span{position:relative;width:9px;height:9px;margin-top:4px;border:2px solid #2d7654;border-radius:50%;background:#fff}.timeline li:not(:last-child)>span::after{content:"";position:absolute;left:2px;top:9px;width:1px;height:49px;background:#cfdbd2}.timeline li.snapshot>span{border-color:#a7afa9}.timeline li header{display:flex;justify-content:space-between}.timeline li header strong{font-size:11px}.timeline li time,.timeline li small{color:#7a837e;font-size:9px}.timeline li p{margin:3px 0;color:#4f5d55;font-size:10px}
.acknowledge{display:grid;grid-template-columns:1fr minmax(220px,330px) auto;gap:12px;align-items:center;margin-top:8px;padding:12px;border:1px solid #e1e6e1;border-radius:8px;background:#f7f9f6}.acknowledge p{margin:4px 0 0;color:#707a74;font-size:10px}.acknowledge input{min-height:34px;border:1px solid #d5dcd6;border-radius:6px;padding:6px 8px}.secondary-button.danger{color:#893a32;border-color:#d9aaa4;background:#fff7f5}.action-message{margin:8px 0 0;padding:8px;border-radius:6px;color:#4d5c54;background:#edf3ef;font-size:11px}.global-message{margin:0;border:1px solid #d5e4d9}
</style>
