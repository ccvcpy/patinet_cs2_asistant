<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import FolioIcon from "./FolioIcon.vue";

type LinkedTrade = {
  tradeId: number; tradeNo?: string | null; status?: string | null; stepKey?: string | null;
  stepIndex?: number | null; progress?: number | null; steamBoughtAt?: string | null;
  completedAt?: string | null; steamBuyPrice?: number | null; c5ListingPrice?: number | null;
  c5SoldNetPrice?: number | null; expectedProfit?: number | null; realizedProfit?: number | null;
  expectedRoi?: number | null; realizedRoi?: number | null; error?: string | null;
  createdAt?: string | null; manuallyEdited?: boolean;
};
type WatchItem = {
  marketHashName: string; name?: string | null; active?: boolean;
  steamBuyPrice?: number | null; c5ListingPrice?: number | null; c5ExpectedNetPrice?: number | null;
  balanceDiscount?: number | null; expectedProfit?: number | null; expectedRoi?: number | null;
  minRoi?: number | null; manualReviewRoi?: number | null; inventoryCount?: number | null;
  tradableCount?: number | null; riskStatus?: string | null; riskReason?: string | null;
  executionStatus?: string | null; executionReason?: string | null; firstSeenAt?: string | null;
  lastObservedAt?: string | null; exitedAt?: string | null; exitReason?: string | null;
  latestTrade?: LinkedTrade | null;
};
type HistoryItem = WatchItem & {
  eventType?: string | null; observedAt?: string | null; scanId?: string | null;
  relatedTrade?: LinkedTrade | null;
};
type ListingsCircuit = {
  status?: "closed" | "open" | "half_open"; isBlocking?: boolean;
  nextProbeAt?: string | null; cooldownUntil?: string | null; consecutive429Count?: number;
};
type Paged<T> = { items: T[]; total: number; page: number; pageSize: number; listingsCircuit?: ListingsCircuit };

const rows = ref<WatchItem[]>([]);
const total = ref(0);
const page = ref(1);
const pageSize = 12;
const keywordDraft = ref("");
const keyword = ref("");
const status = ref("active");
const sort = ref("roi_desc");
const loading = ref(false);
const error = ref("");
const listingsCircuit = ref<ListingsCircuit>({ status: "closed", isBlocking: false });
const selected = ref<WatchItem | null>(null);
const history = ref<HistoryItem[]>([]);
const historyPage = ref(1);
const historyTotal = ref(0);
const historyLoading = ref(false);
const historyError = ref("");
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)));
const historyPages = computed(() => Math.max(1, Math.ceil(historyTotal.value / 20)));

function money(value?: number | null): string {
  return typeof value === "number" && Number.isFinite(value) ? `¥${value.toFixed(2)}` : "-";
}
function pct(value?: number | null): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "-";
  return `${(Math.abs(value) <= 1 ? value * 100 : value).toFixed(2)}%`;
}
function time(value?: string | null): string {
  if (!value) return "-";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("zh-CN", { hour12: false });
}
async function responseError(response: Response): Promise<string> {
  try {
    const payload = await response.json() as { error?: string; detail?: string };
    return payload.error || payload.detail || response.statusText;
  } catch { return response.statusText; }
}
function stateLabel(row: WatchItem): string {
  if (row.executionStatus === "listings_probe_ready") return "达到门槛 · 等待恢复探测";
  if (
    row.executionStatus === "listings_cooldown"
    || (listingsCircuit.value.isBlocking && ["executable", "eligible"].includes(row.executionStatus || ""))
  ) return "达到门槛 · listings冷却中";
  const labels: Record<string, string> = {
    executable: "达到执行门槛", eligible: "达到执行门槛", observe_only: "仅观察，不执行",
    blocked: "风控阻断", manual_review: "异常 ROI，需人工", exited: "已退出观察池",
  };
  return labels[row.executionStatus || "observe_only"] || row.executionStatus || "仅观察，不执行";
}
function stateClass(row: WatchItem): string {
  if (row.active === false) return "exited";
  if (["listings_cooldown", "listings_probe_ready"].includes(row.executionStatus || "")) return "cooldown";
  if (listingsCircuit.value.isBlocking && ["executable", "eligible"].includes(row.executionStatus || "")) return "cooldown";
  if (["executable", "eligible"].includes(row.executionStatus || "")) return "ready";
  if (["blocked", "manual_review"].includes(row.executionStatus || "")) return "blocked";
  return "observe";
}
function tradeStateLabel(trade: LinkedTrade): string {
  const status = trade.status || "";
  if (status === "completed") return "已完成";
  if (status === "c5_listed") return "已买入，C5 在售";
  if (status === "listing_c5") return "已买入，正在 C5 上架";
  if (status === "steam_bought") return "已买入，准备 C5 上架";
  if (status === "buying") return "正在买入";
  if (status === "locked") return "执行中，已锁定 A";
  if (status === "audited" || status === "candidate") return "已转化为流水，执行中";
  if (status === "manual_required") return "执行中断，需人工处理";
  if (status === "failed") return "执行失败";
  if (status === "cancelled") return "已取消";
  return status || "执行状态待同步";
}
function tradeStateClass(trade: LinkedTrade): string {
  if (trade.status === "completed") return "done";
  if (["manual_required", "failed"].includes(trade.status || "")) return "attention";
  if (trade.status === "cancelled") return "stopped";
  return "running";
}

async function load(): Promise<void> {
  loading.value = true; error.value = "";
  const activeFilter = status.value === "all" ? "all" : status.value === "exited" ? "false" : "true";
  const query = new URLSearchParams({ page: String(page.value), pageSize: String(pageSize), active: activeFilter, sort: sort.value });
  if (keyword.value) query.set("keyword", keyword.value);
  try {
    const response = await fetch(`/api/profit-trade/roi-watch?${query}`, { cache: "no-store" });
    if (!response.ok) throw new Error(await responseError(response));
    const payload = await response.json() as Paged<WatchItem>;
    rows.value = Array.isArray(payload.items) ? payload.items : [];
    total.value = Number(payload.total) || 0;
    listingsCircuit.value = payload.listingsCircuit || { status: "closed", isBlocking: false };
  } catch (reason) {
    rows.value = []; total.value = 0;
    error.value = `观察池读取失败：${reason instanceof Error ? reason.message : String(reason)}`;
  } finally { loading.value = false; }
}
function search(): void { keyword.value = keywordDraft.value.trim(); page.value = 1; void load(); }
function turn(direction: -1 | 1): void {
  const next = page.value + direction;
  if (next >= 1 && next <= totalPages.value) { page.value = next; void load(); }
}
async function openHistory(row: WatchItem): Promise<void> { selected.value = row; historyPage.value = 1; await loadHistory(); }
async function loadHistory(): Promise<void> {
  if (!selected.value) return;
  historyLoading.value = true; historyError.value = "";
  const query = new URLSearchParams({ marketHashName: selected.value.marketHashName, page: String(historyPage.value), pageSize: "20" });
  try {
    const response = await fetch(`/api/profit-trade/roi-watch/history?${query}`, { cache: "no-store" });
    if (!response.ok) throw new Error(await responseError(response));
    const payload = await response.json() as Paged<HistoryItem>;
    history.value = Array.isArray(payload.items) ? payload.items : [];
    historyTotal.value = Number(payload.total) || 0;
  } catch (reason) {
    history.value = []; historyTotal.value = 0;
    historyError.value = `历史读取失败：${reason instanceof Error ? reason.message : String(reason)}`;
  } finally { historyLoading.value = false; }
}
function turnHistory(direction: -1 | 1): void {
  const next = historyPage.value + direction;
  if (next >= 1 && next <= historyPages.value) { historyPage.value = next; void loadHistory(); }
}
watch([status, sort], () => { page.value = 1; void load(); });
onMounted(() => { void load(); window.addEventListener("profit-trade:refresh-observability", load); });
onUnmounted(() => window.removeEventListener("profit-trade:refresh-observability", load));
</script>

<template>
  <section class="roi-watch panel" aria-labelledby="roi-watch-title">
    <div class="watch-heading">
      <div><p class="eyebrow">S1 · 机会观察</p><h2 id="roi-watch-title">ROI &gt; 0 观察池</h2><p>全量评估后的正 ROI 品类；低于阈值或风控阻断时只观察，绝不自动执行。</p></div>
      <div class="watch-count"><strong>{{ total }}</strong><span>当前品类</span></div>
    </div>
    <form class="watch-toolbar" @submit.prevent="search">
      <label><span>品类搜索</span><input v-model="keywordDraft" type="search" placeholder="中文名或 marketHashName"></label>
      <label><span>观察状态</span><select v-model="status"><option value="active">当前观察</option><option value="all">全部历史状态</option><option value="exited">已退出</option></select></label>
      <label><span>排序</span><select v-model="sort"><option value="roi_desc">ROI 从高到低</option><option value="updated_desc">最近观察</option><option value="price_desc">C5 挂价从高到低</option></select></label>
      <button class="secondary-button" type="submit">查询</button>
      <button class="secondary-button refresh" type="button" @click="load"><FolioIcon name="refresh" :size="15" />刷新</button>
    </form>
    <p v-if="error" class="watch-error">{{ error }}。不会使用静态或伪造数据替代。</p>
    <p v-if="listingsCircuit.isBlocking" class="watch-circuit-note">
      <strong>Steam listings 冷却中：</strong>
      正 ROI 机会继续更新，但不会创建买 B 流水或锁定 A；下次探测
      {{ time(listingsCircuit.nextProbeAt || listingsCircuit.cooldownUntil) }}。
    </p>
    <div v-if="loading" class="watch-empty">正在读取观察池…</div>
    <div v-else-if="!error && rows.length === 0" class="watch-empty">当前筛选条件下没有 ROI &gt; 0 的观察记录。</div>
    <div v-else class="watch-grid">
      <article v-for="row in rows" :key="row.marketHashName" class="watch-card">
        <div class="card-head"><div><strong>{{ row.name || row.marketHashName }}</strong><small v-if="row.name && row.name !== row.marketHashName">{{ row.marketHashName }}</small></div><span :class="['watch-state', stateClass(row)]">{{ stateLabel(row) }}</span></div>
        <p
          v-if="row.executionStatus === 'listings_cooldown' || row.executionStatus === 'listings_probe_ready' || (listingsCircuit.isBlocking && ['executable','eligible'].includes(row.executionStatus || ''))"
          class="card-cooldown"
        >
          当前行情仍符合执行门槛，但 Steam listings 查询处于{{ listingsCircuit.status === "half_open" ? "恢复探测" : "冷却" }}状态；不会沿用旧价格购买。
        </p>
        <div class="price-line"><dl><dt>Steam 买入</dt><dd>{{ money(row.steamBuyPrice) }}</dd></dl><i>→</i><dl><dt>C5 竞争挂价</dt><dd>{{ money(row.c5ListingPrice) }}</dd></dl><i>→</i><dl><dt>C5 预计到手</dt><dd>{{ money(row.c5ExpectedNetPrice) }}</dd></dl></div>
        <div class="watch-metrics"><dl><dt>当前 ROI</dt><dd class="positive">{{ pct(row.expectedRoi) }}</dd></dl><dl><dt>执行阈值</dt><dd>{{ pct(row.minRoi) }}</dd></dl><dl><dt>余额折扣</dt><dd>{{ pct(row.balanceDiscount) }}</dd></dl><dl><dt>预计收益</dt><dd>{{ money(row.expectedProfit) }}</dd></dl><dl><dt>可交易 / 库存</dt><dd>{{ row.tradableCount ?? "-" }} / {{ row.inventoryCount ?? "-" }}</dd></dl><dl><dt>最后观察</dt><dd>{{ time(row.lastObservedAt) }}</dd></dl></div>
        <p v-if="row.executionReason || row.riskReason || row.exitReason" class="reason">{{ row.executionReason || row.riskReason || row.exitReason }}</p>
        <div v-if="row.latestTrade" :class="['linked-trade', tradeStateClass(row.latestTrade)]">
          <span><small>最近关联流水</small><strong>{{ tradeStateLabel(row.latestTrade) }}</strong></span>
          <span><small>{{ row.latestTrade.tradeNo }}</small><b>Steam {{ money(row.latestTrade.steamBuyPrice) }}</b></span>
        </div>
        <button class="history-link" type="button" @click="openHistory(row)">查看价格与 ROI 历史</button>
      </article>
    </div>
    <footer class="pagination"><span>第 {{ page }} / {{ totalPages }} 页，共 {{ total }} 个品类</span><div><button class="mini-action" type="button" :disabled="page <= 1" @click="turn(-1)">上一页</button><button class="mini-action" type="button" :disabled="page >= totalPages" @click="turn(1)">下一页</button></div></footer>

    <div v-if="selected" class="history-backdrop" @click.self="selected = null">
      <aside class="history-drawer">
        <header><div><p class="eyebrow">观察历史</p><h3>{{ selected.name || selected.marketHashName }}</h3><small>{{ selected.marketHashName }}</small></div><button type="button" aria-label="关闭" @click="selected = null">×</button></header>
        <p v-if="historyError" class="watch-error">{{ historyError }}</p><div v-if="historyLoading" class="watch-empty">正在读取历史…</div>
        <div v-else class="history-list"><article v-for="(event,index) in history" :key="`${event.scanId || event.observedAt}-${index}`"><div><strong>{{ pct(event.expectedRoi) }}</strong><span>{{ event.eventType || (event.active === false ? "退出" : "观察") }}</span></div><dl><div><dt>Steam</dt><dd>{{ money(event.steamBuyPrice) }}</dd></div><div><dt>C5 挂价</dt><dd>{{ money(event.c5ListingPrice) }}</dd></div><div><dt>预计到手</dt><dd>{{ money(event.c5ExpectedNetPrice) }}</dd></div><div><dt>观察时间</dt><dd>{{ time(event.observedAt || event.lastObservedAt) }}</dd></div></dl><p v-if="event.executionReason || event.riskReason || event.exitReason">{{ event.executionReason || event.riskReason || event.exitReason }}</p><section v-if="event.relatedTrade" :class="['history-trade-link', tradeStateClass(event.relatedTrade)]"><header><span>该快照已转化为真实流水</span><strong>{{ tradeStateLabel(event.relatedTrade) }}</strong></header><small>{{ event.relatedTrade.tradeNo }} · trade {{ event.relatedTrade.tradeId }}</small><dl><div><dt>Steam 实际买入</dt><dd>{{ money(event.relatedTrade.steamBuyPrice) }}</dd></div><div><dt>买入时间</dt><dd>{{ time(event.relatedTrade.steamBoughtAt) }}</dd></div><div><dt>C5 实际到手</dt><dd>{{ money(event.relatedTrade.c5SoldNetPrice) }}</dd></div><div><dt>完成时间</dt><dd>{{ time(event.relatedTrade.completedAt) }}</dd></div><div><dt>实际收益</dt><dd>{{ money(event.relatedTrade.realizedProfit) }}</dd></div><div><dt>实际 ROI</dt><dd>{{ pct(event.relatedTrade.realizedRoi) }}</dd></div></dl><p v-if="event.relatedTrade.error">{{ event.relatedTrade.error }}</p></section></article></div>
        <footer class="pagination"><span>第 {{ historyPage }} / {{ historyPages }} 页，共 {{ historyTotal }} 条</span><div><button class="mini-action" type="button" :disabled="historyPage <= 1" @click="turnHistory(-1)">上一页</button><button class="mini-action" type="button" :disabled="historyPage >= historyPages" @click="turnHistory(1)">下一页</button></div></footer>
      </aside>
    </div>
  </section>
</template>

<style scoped>
.roi-watch{padding:18px;border-color:#dfe5df;box-shadow:0 8px 24px rgba(27,62,44,.045)}.watch-heading{display:flex;justify-content:space-between;gap:20px}.watch-heading h2{margin:0;color:#17201c;font-size:19px}.watch-heading p:not(.eyebrow){margin:6px 0 0;color:#6f7872;font-size:12px}.watch-count{display:grid;min-width:88px;padding:10px 14px;border-radius:9px;text-align:right;background:#e8f2ec}.watch-count strong{color:#236a4c;font-size:22px}.watch-count span{color:#68736d;font-size:11px}
.watch-toolbar{display:grid;grid-template-columns:minmax(260px,1fr) 150px 180px auto auto;gap:9px;align-items:end;margin:16px 0 13px;padding:12px;border:1px solid #e5e9e4;border-radius:9px;background:#f8faf7}.watch-toolbar label{display:grid;gap:4px}.watch-toolbar label span{color:#68736d;font-size:11px;font-weight:650}.watch-toolbar input,.watch-toolbar select{min-height:35px;border:1px solid #d6ddd6;border-radius:6px;padding:6px 9px;color:#17201c;background:#fff}.watch-toolbar .refresh{display:inline-flex;align-items:center;justify-content:center;gap:5px}.watch-error{margin:10px 0;padding:9px 11px;border:1px solid #e7b8b2;border-radius:7px;color:#8d3d34;background:#fff7f5;font-size:12px}.watch-empty{display:grid;place-items:center;min-height:90px;color:#77817b;border:1px dashed #d8ded8;border-radius:8px}
.watch-circuit-note{margin:0 0 12px;padding:9px 11px;border:1px solid #dfc77e;border-radius:7px;color:#6b5b2c;background:#fff9e9;font-size:11px}.watch-circuit-note strong{color:#564718}
.watch-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.watch-card{display:grid;gap:11px;padding:13px;border:1px solid #e0e5df;border-radius:9px;background:#fff}.card-head{display:flex;justify-content:space-between;gap:12px}.card-head>div{display:grid;min-width:0}.card-head strong,.card-head small{overflow-wrap:anywhere}.card-head small{color:#7a837e}.watch-state{height:max-content;padding:4px 7px;border-radius:999px;font-size:10px;font-weight:700;white-space:nowrap}.watch-state.ready{color:#1d6748;background:#e6f4eb}.watch-state.observe{color:#6d5a25;background:#faf3d9}.watch-state.blocked{color:#913b31;background:#fbe9e6}.watch-state.exited{color:#68716c;background:#edf0ed}.price-line{display:grid;grid-template-columns:1fr auto 1fr auto 1fr;gap:7px;align-items:center;padding:9px;border-radius:7px;background:#f7f9f6}.price-line i{color:#98a19b;font-style:normal}.price-line dl,.watch-metrics dl{margin:0}.price-line dt,.watch-metrics dt{color:#77817b;font-size:10px}.price-line dd{margin:2px 0 0;font-size:13px;font-weight:700}.watch-metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:9px}.watch-metrics dd{margin:2px 0 0;font-size:12px;font-weight:650}.watch-metrics .positive{color:#1f704d}.reason{margin:0;padding:7px 9px;border-left:3px solid #d4b05b;color:#665731;background:#fbf7e9;font-size:11px}.history-link{justify-self:start;border:0;padding:0;color:#236a4c;background:transparent;font-size:11px;font-weight:700}.pagination{display:flex;justify-content:space-between;align-items:center;margin-top:13px;color:#6f7872;font-size:11px}.pagination>div{display:flex;gap:6px}
.watch-state.cooldown{color:#6d5720;background:#f8edc8}.card-cooldown{margin:0;padding:7px 9px;border-left:3px solid #cba545;color:#665731;background:#fff8e7;font-size:10px}
.linked-trade{border:1px solid #c9dfd1;border-radius:9px;padding:9px 10px;display:flex;align-items:center;justify-content:space-between;gap:12px;background:#f0f7f3}.linked-trade>span{display:grid;gap:2px}.linked-trade>span:last-child{text-align:right}.linked-trade small{color:#718078;font-size:9px}.linked-trade strong,.linked-trade b{color:#236a4c;font-size:11px}.linked-trade b{font-weight:700}.linked-trade.attention{border-color:#e8c5c0;background:#fff2ef}.linked-trade.attention strong,.linked-trade.attention b{color:#9a4037}.linked-trade.stopped{border-color:#dfe3df;background:#f3f5f3}.linked-trade.stopped strong,.linked-trade.stopped b{color:#68716c}
.history-backdrop{position:fixed;inset:0;z-index:40;display:flex;justify-content:flex-end;background:rgba(19,31,25,.25)}.history-drawer{width:min(620px,48vw);height:100%;overflow:auto;padding:22px;border-left:1px solid #dce3dc;background:#f7f9f6;box-shadow:-15px 0 35px rgba(21,47,34,.13)}.history-drawer>header{display:flex;justify-content:space-between;gap:20px;padding-bottom:14px;border-bottom:1px solid #dfe4df}.history-drawer h3{margin:0}.history-drawer header small{color:#768079}.history-drawer header button{width:32px;height:32px;border:1px solid #d9dfd9;border-radius:7px;background:#fff;font-size:19px}.history-list{display:grid;gap:8px;margin:14px 0}.history-list article{padding:12px;border:1px solid #dfe5df;border-radius:8px;background:#fff}.history-list article>div{display:flex;justify-content:space-between}.history-list article>div strong{color:#236a4c;font-size:17px}.history-list article>div span,.history-list dt{color:#79827d;font-size:11px}.history-list dl{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}.history-list dl>div{display:flex;justify-content:space-between}.history-list dd{margin:0;font-size:11px;font-weight:650}.history-list p{color:#745148;font-size:11px}
.history-trade-link{margin-top:10px;border:1px solid #c9dfd1;border-radius:9px;padding:10px;background:#f0f7f3}.history-trade-link>header{display:flex;align-items:center;justify-content:space-between;gap:10px}.history-trade-link>header span{color:#5e7769;font-size:10px;font-weight:700}.history-trade-link>header strong{color:#236a4c;font-size:12px}.history-trade-link>small{display:block;margin:3px 0 8px;color:#728078;font-size:9px}.history-trade-link dl{margin:0}.history-trade-link.done{border-color:#bcd9c6;background:#eaf4ee}.history-trade-link.attention{border-color:#e8c5c0;background:#fff2ef}.history-trade-link.attention>header strong{color:#9a4037}.history-trade-link.stopped{border-color:#dfe3df;background:#f3f5f3}.history-trade-link.stopped>header strong{color:#68716c}
</style>
