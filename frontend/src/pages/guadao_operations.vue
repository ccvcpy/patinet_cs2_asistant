<script setup lang="ts">
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, ref, watch } from "vue";
import { RouterLink, useRoute } from "vue-router";
import FolioIcon from "../components/FolioIcon.vue";
import { formatCountdown, formatLocal, responseError, unwrapPayload } from "./guadao_shared";

type Operation = {
  id: string | number;
  operationId?: string;
  marketHashName?: string;
  displayName?: string | null;
  accountName?: string | null;
  status?: string;
  stage?: string;
  stepIndex?: number;
  listingRatioAtOpen?: number | null;
  maxRebuyRatioAtOpen?: number | null;
  guadaoMaxListingRatioAtOpen?: number | null;
  ratioRuleSource?: string | null;
  ratioRuleId?: string | number | null;
  ratioRuleVersion?: number | null;
  assetId?: string | null;
  listingId?: string | null;
  c5OrderId?: string | null;
  steamListPrice?: number | null;
  steamNetAmount?: number | null;
  c5RebuyPrice?: number | null;
  steamId?: string | null;
  steamSoldAt?: string | null;
  c5OrderSubmittedAt?: string | null;
  c5DeliveryDeadlineAt?: string | null;
  createdAt?: string | null;
  updatedAt?: string | null;
  nextAttemptAt?: string | null;
  nextTaskLabel?: string | null;
  nextTaskReason?: string | null;
  timeline?: Array<{ at?: string; label?: string; detail?: string; status?: string }>;
};
type ResponsePayload = { operations?: Operation[]; total?: number; page?: number; pageSize?: number; summary?: Record<string, number>; accounts?: Array<{id?:string;name?:string;steamId?:string}>; runtime?: {enabled?:boolean;status?:string;runtimeStatus?:string} };

const steps = ["锁定资产", "Steam 上架", "挂单确认", "Steam 在售", "创建补仓", "C5 发货", "闭环"];
const operations = ref<Operation[]>([]);
const total = ref(0);
const summary = ref<Record<string, number>>({});
const accountOptions = ref<string[]>([]);
const selectedId = ref<string | number | null>(null);
const loading = ref(false);
const error = ref("");
const keyword = ref("");
const account = ref("");
const status = ref("");
const startAt = ref("");
const endAt = ref("");
const page = ref(1);
const pageSize = ref(10);
const route = useRoute();
const runtime = ref<ResponsePayload["runtime"]>({});
keyword.value = String(route.query.q || "");
let timer: ReturnType<typeof setInterval> | null = null;

const selected = computed(() => operations.value.find(row => row.id === selectedId.value) || operations.value[0] || null);
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)));
const accounts = computed(() => [...new Set([...accountOptions.value,...operations.value.map(row => row.accountName).filter(Boolean) as string[]])]);
const metricCards = computed(() => [
  ["全部", summary.value.total ?? total.value], ["待确认", summary.value.pendingConfirmation ?? 0], ["Steam 在售", summary.value.steamListed ?? 0], ["已卖出待补仓", summary.value.pendingRebuy ?? 0], ["C5 发货确认", summary.value.deliveryPending ?? 0], ["已闭环", summary.value.completed ?? 0],
]);

function pct(value?: number | null): string { return value == null ? "—" : `${(value * (value <= 1 ? 100 : 1)).toFixed(2)}%`; }
function money(value?: number | null): string { return value == null ? "—" : `¥ ${Number(value).toFixed(2)}`; }
function stageTone(row: Operation): string { if (row.status === "completed") return "success"; if (row.status?.includes("failed") || row.status === "manual_required") return "danger"; return "warning"; }
function relatedLogsTo(row: Operation): { path: string; query: Record<string,string> } { return { path: "/guadao/logs", query: { operationId: String(row.operationId || row.id), marketHashName: row.marketHashName || "", account: row.accountName || "" } }; }
const runtimeText = computed(() => { const value=String(runtime.value?.runtimeStatus||runtime.value?.status||""); if(value==="closing_only")return"存量闭环中";if(value==="preparing")return"启动准备中";return runtime.value?.enabled?"运行中":"已关闭"; });

async function refresh(): Promise<void> {
  loading.value = true;
  try {
    const params = new URLSearchParams({ page: String(page.value), pageSize: String(pageSize.value) });
    if (keyword.value.trim()) params.set("q", keyword.value.trim());
    if (account.value) params.set("account", account.value);
    if (status.value) params.set("status", status.value);
    if (startAt.value) params.set("startAt", new Date(startAt.value).toISOString());
    if (endAt.value) params.set("endAt", new Date(endAt.value).toISOString());
    const response = await fetch(`/api/guadao/operations?${params}`, { cache: "no-store" });
    if (!response.ok) throw new Error(await responseError(response));
    const data = unwrapPayload<ResponsePayload>(await response.json());
    operations.value = data.operations || [];
    total.value = Number(data.total ?? operations.value.length);
    summary.value = data.summary || {};
    runtime.value = data.runtime || {};
    accountOptions.value = (data.accounts || []).map(row => row.name || row.id || "").filter(Boolean);
    if (selectedId.value == null || !operations.value.some(row => row.id === selectedId.value)) selectedId.value = operations.value[0]?.id ?? null;
    error.value = "";
  } catch (reason) { error.value = reason instanceof Error ? reason.message : String(reason); }
  finally { loading.value = false; }
}
function query(): void { page.value = 1; void refresh(); }
function startPolling(): void { if (timer === null) timer = setInterval(() => void refresh(), 15_000); }
function stopPolling(): void { if (timer !== null) clearInterval(timer); timer = null; }
watch(()=>route.query.q,value=>{const next=String(value||"");if(next===keyword.value)return;keyword.value=next;query()});
onMounted(() => { keyword.value=String(route.query.q||"");void refresh(); startPolling(); }); onActivated(startPolling); onDeactivated(stopPolling); onUnmounted(stopPolling);
</script>

<template>
  <main class="page operations-page">
    <header class="page-title-row"><div><p class="eyebrow">Guadao Operations</p><h1>挂刀流水状态</h1><p>每笔流水独立推进，下一步由 nextAttemptAt 决定</p></div><RouterLink class="runtime-link" to="/guadao/overview"><span></span>挂刀执行器 · {{ runtimeText }} · 前往总览控制</RouterLink></header>
    <p v-if="error" class="api-error">流水 API 请求失败：{{ error }}</p>
    <section class="operation-metrics"><article v-for="metric in metricCards" :key="String(metric[0])"><span>{{ metric[0] }}</span><strong>{{ metric[1] }}</strong></article></section>
    <section class="panel filters"><label><span>关键词</span><input v-model="keyword" placeholder="物品 / 账号 / listingId / assetId" @keyup.enter="query" /></label><label><span>Steam 账号</span><select v-model="account"><option value="">全部</option><option v-for="name in accounts" :key="name" :value="name">{{ name }}</option></select></label><label><span>流水状态</span><select v-model="status"><option value="">全部阶段</option><option value="listed">Steam 在售</option><option value="sold">已卖出待补仓</option><option value="delivery_pending">C5 发货确认</option><option value="completed">已闭环</option><option value="manual_required">人工处理</option></select></label><label><span>创建时间（北京时间）</span><div class="date-range"><input v-model="startAt" type="datetime-local" /><i>至</i><input v-model="endAt" type="datetime-local" /></div></label><button class="primary-button" type="button" :disabled="loading" @click="query">{{ loading ? "查询中…" : "查询流水" }}</button></section>

    <section class="operation-workbench">
      <div class="panel operation-list">
        <article v-for="row in operations" :key="row.id" :class="['operation-row', { selected: selected?.id === row.id }]" @click="selectedId = row.id">
          <div class="operation-summary"><div><strong>{{ row.displayName || row.marketHashName || "未命名饰品" }}</strong><span>{{ row.accountName || "账号未记录" }}</span></div><span :class="['status-pill', stageTone(row)]">{{ row.stage || row.status || "状态未知" }}</span></div>
          <div class="operation-values"><div><span>当前挂刀比例</span><strong>{{ pct(row.listingRatioAtOpen) }}</strong></div><div><span>冻结上限</span><strong>{{ pct(row.maxRebuyRatioAtOpen) }}</strong></div><div><span>规则</span><strong>{{ row.ratioRuleSource || "全局" }}</strong></div><div><span>下一任务</span><strong>{{ row.nextTaskLabel || formatCountdown(row.nextAttemptAt) }}</strong></div><time>{{ formatLocal(row.updatedAt) }}</time></div>
          <div class="stepper" :aria-label="`${row.displayName || row.marketHashName} 执行进度`"><div v-for="(label,index) in steps" :key="label" :class="{ done: (row.stepIndex || 0) > index, active: (row.stepIndex || 0) === index }"><i>{{ (row.stepIndex || 0) > index ? "✓" : index + 1 }}</i><span>{{ label }}</span></div></div>
        </article>
        <div v-if="!operations.length" class="empty-state">{{ loading ? "正在读取流水…" : "当前筛选没有后端流水记录。" }}</div>
        <footer v-if="total" class="pagination"><span>共 {{ total }} 条 · 第 {{ page }} / {{ pageCount }} 页</span><select v-model.number="pageSize" @change="page=1;refresh()"><option :value="10">10 条/页</option><option :value="20">20 条/页</option><option :value="50">50 条/页</option></select><button :disabled="page<=1" @click="page--;refresh()">上一页</button><button :disabled="page>=pageCount" @click="page++;refresh()">下一页</button></footer>
      </div>

      <aside class="panel operation-detail">
        <template v-if="selected"><div class="detail-head"><div><strong>{{ selected.displayName || selected.marketHashName }}</strong><span class="mono">运行 ID：{{ selected.operationId || selected.id }}</span></div><span :class="['status-pill', stageTone(selected)]">{{ selected.stage || selected.status }}</span></div>
          <dl class="detail-grid"><div><dt>assetId</dt><dd>{{ selected.assetId || "—" }}</dd></div><div><dt>Steam 账号</dt><dd>{{ selected.accountName || "—" }}</dd></div><div><dt>SteamID</dt><dd>{{ selected.steamId || "—" }}</dd></div><div><dt>listingId</dt><dd>{{ selected.listingId || "—" }}</dd></div><div><dt>C5 orderId</dt><dd>{{ selected.c5OrderId || "—" }}</dd></div><div><dt>规则来源</dt><dd>{{ selected.ratioRuleSource || "全局" }}{{ selected.ratioRuleId ? ` · ${selected.ratioRuleId}` : "" }}{{ selected.ratioRuleVersion ? ` · v${selected.ratioRuleVersion}` : "" }}</dd></div><div><dt>实际挂刀比例</dt><dd>{{ pct(selected.listingRatioAtOpen) }}</dd></div><div><dt>最大补仓比例</dt><dd>{{ pct(selected.maxRebuyRatioAtOpen) }}</dd></div><div><dt>本单候选上限</dt><dd>{{ pct(selected.guadaoMaxListingRatioAtOpen) }}</dd></div><div><dt>Steam 挂价</dt><dd>{{ money(selected.steamListPrice) }}</dd></div><div><dt>Steam 税后到手</dt><dd>{{ money(selected.steamNetAmount) }}</dd></div><div><dt>C5 补仓价</dt><dd>{{ money(selected.c5RebuyPrice) }}</dd></div><div><dt>Steam 官方卖出时间</dt><dd>{{ formatLocal(selected.steamSoldAt) }}</dd></div><div><dt>C5 下单时间</dt><dd>{{ formatLocal(selected.c5OrderSubmittedAt) }}</dd></div><div><dt>C5 发货硬期限</dt><dd>{{ formatLocal(selected.c5DeliveryDeadlineAt) }}</dd></div></dl>
          <section class="timeline"><h2>状态时间线</h2><div v-if="selected.timeline?.length"><article v-for="(event,index) in selected.timeline" :key="`${event.at}-${index}`" :class="event.status"><i></i><div><strong>{{ event.label || "状态更新" }}</strong><span>{{ event.detail }}</span></div><time>{{ formatLocal(event.at) }}</time></article></div><p v-else>后端暂未返回该流水的状态时间线。</p></section>
          <RouterLink class="related-log-link" :to="relatedLogsTo(selected)"><FolioIcon name="clock" :size="13" />查看关联实时日志</RouterLink><div class="next-task"><span>下一个任务</span><strong>{{ selected.nextTaskLabel || "尚未安排" }} · {{ formatCountdown(selected.nextAttemptAt) }}</strong><p v-if="selected.nextTaskReason">原因：{{ selected.nextTaskReason }}</p></div>
        </template><div v-else class="empty-state">选择一笔流水查看冻结口径、远端标识和时间线。</div>
      </aside>
    </section>
  </main>
</template>

<style scoped>
.operations-page{width:min(1360px,calc(100vw - 44px));gap:14px}.page-title-row{display:flex;justify-content:space-between;align-items:center;padding:6px 8px}.page-title-row h1{margin:0;font-size:32px;letter-spacing:-.045em}.page-title-row p:last-child{margin:6px 0 0;color:var(--folio-muted);font-size:12px}.runtime-link{display:flex;align-items:center;gap:7px;border:1px solid var(--folio-line);border-radius:11px;padding:10px 13px;color:var(--folio-green);background:#fff;font-size:11px;font-weight:700}.runtime-link span{width:7px;height:7px;border-radius:50%;background:currentColor}.api-error{margin:0;border-radius:10px;padding:9px 12px;color:var(--folio-red);background:var(--folio-red-soft);font-size:12px}.operation-metrics{display:grid;grid-template-columns:repeat(6,1fr);gap:10px}.operation-metrics article{min-height:82px;display:grid;align-content:space-between;border:1px solid var(--folio-line);border-radius:14px;padding:13px 15px;background:#fff;box-shadow:var(--folio-shadow)}.operation-metrics span{color:var(--folio-muted);font-size:10px}.operation-metrics strong{font-size:22px}.filters{display:grid;grid-template-columns:1.6fr .8fr .9fr 120px;gap:12px;align-items:end;padding:14px}.filters label{display:grid;gap:5px}.filters label>span{color:var(--folio-muted);font-size:10px;font-weight:700}.filters input,.filters select{width:100%;min-height:40px;border:1px solid #dfe4df;border-radius:10px;padding:8px 11px;color:var(--folio-ink);background:#fff}.operation-workbench{display:grid;grid-template-columns:minmax(0,1.8fr) minmax(360px,.8fr);gap:12px;align-items:start}.operation-list{padding:0;overflow:hidden}.operation-row{padding:16px 18px;border-bottom:1px solid var(--folio-line);cursor:pointer;transition:background .16s}.operation-row:hover{background:#fafcfa}.operation-row.selected{background:#f2f8f4;box-shadow:inset 3px 0 0 var(--folio-green)}.operation-summary,.operation-values,.detail-head{display:flex;align-items:center}.operation-summary{justify-content:space-between}.operation-summary>div{display:grid;gap:3px}.operation-summary strong{font-size:13px}.operation-summary span,.operation-values span{color:var(--folio-muted);font-size:9px}.status-pill{display:inline-flex;border-radius:999px;padding:5px 9px;font-size:9px;font-weight:800}.status-pill.success{color:var(--folio-green);background:var(--folio-green-soft)}.status-pill.warning{color:var(--folio-amber);background:var(--folio-amber-soft)}.status-pill.danger{color:var(--folio-red);background:var(--folio-red-soft)}.operation-values{display:grid;grid-template-columns:repeat(4,minmax(90px,1fr)) 120px;gap:9px;margin-top:12px}.operation-values div{display:grid;gap:3px}.operation-values strong{font-size:10px}.operation-values time{align-self:center;color:var(--folio-muted);font-size:9px;text-align:right}.stepper{display:grid;grid-template-columns:repeat(7,1fr);margin-top:14px}.stepper div{position:relative;display:grid;place-items:center;gap:4px;color:#9aa29d}.stepper div::before{content:"";position:absolute;top:8px;right:50%;left:-50%;height:2px;background:#e1e5e1}.stepper div:first-child::before{display:none}.stepper i{position:relative;z-index:1;display:grid;place-items:center;width:18px;height:18px;border:1px solid #d6ddd8;border-radius:50%;background:#fff;font-size:8px;font-style:normal}.stepper span{font-size:7px}.stepper .done,.stepper .active{color:var(--folio-green)}.stepper .done::before,.stepper .active::before{background:var(--folio-green)}.stepper .done i,.stepper .active i{border-color:var(--folio-green)}.stepper .done i{color:#fff;background:var(--folio-green)}.pagination{display:flex;justify-content:flex-end;align-items:center;gap:8px;padding:12px 16px;color:var(--folio-muted);font-size:10px}.pagination span{margin-right:auto}.pagination select,.pagination button{min-height:30px;border:1px solid var(--folio-line);border-radius:8px;padding:5px 9px;background:#fff}.operation-detail{position:sticky;top:122px;min-height:560px}.detail-head{justify-content:space-between;gap:12px;border-bottom:1px solid var(--folio-line);padding-bottom:12px}.detail-head>div{display:grid;gap:4px}.detail-head strong{font-size:15px}.detail-head span{color:var(--folio-muted);font-size:9px}.detail-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 14px;margin:13px 0}.detail-grid div{border-bottom:1px solid #edf0ed;padding:8px 0}.detail-grid dt{color:var(--folio-muted);font-size:9px}.detail-grid dd{margin:3px 0 0;overflow-wrap:anywhere;font-size:10px;font-weight:650}.timeline{margin-top:15px}.timeline h2{font-size:14px}.timeline>div{display:grid}.timeline article{position:relative;display:grid;grid-template-columns:12px minmax(0,1fr) auto;gap:7px;padding:7px 0}.timeline article:not(:last-child)::before{content:"";position:absolute;top:18px;bottom:-4px;left:5px;width:1px;background:#d9dfda}.timeline i{z-index:1;width:11px;height:11px;margin-top:2px;border:2px solid var(--folio-green);border-radius:50%;background:#fff}.timeline article>div{display:grid;gap:2px}.timeline strong{font-size:10px}.timeline span,.timeline time,.timeline>p{color:var(--folio-muted);font-size:9px}.next-task{margin-top:14px;border-radius:11px;padding:12px;color:#765319;background:var(--folio-amber-soft)}.next-task span{font-size:9px}.next-task strong{display:block;margin-top:3px;font-size:11px}.next-task p{margin:4px 0 0;font-size:9px}.empty-state{margin:14px;border:1px dashed #d8ded9;border-radius:11px;padding:26px;color:var(--folio-muted);text-align:center;background:var(--folio-surface-soft);font-size:11px}
.runtime-link{text-decoration:none}.filters{grid-template-columns:1.25fr .65fr .72fr 1.35fr 112px}.date-range{display:grid;grid-template-columns:1fr auto 1fr;gap:5px;align-items:center}.date-range i{color:var(--folio-muted);font-size:9px;font-style:normal}.related-log-link{display:inline-flex;align-items:center;gap:6px;margin-top:10px;color:var(--folio-green);font-size:10px;font-weight:700;text-decoration:none}
</style>
