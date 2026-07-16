<script setup lang="ts">
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, ref } from "vue";
import { RouterLink } from "vue-router";
import FolioIcon from "../components/FolioIcon.vue";
import { formatLocal, responseError, unwrapPayload, type GuadaoIssue } from "./guadao_shared";

type IssuesPayload = { issues?: GuadaoIssue[]; total?: number; summary?: Record<string, number>; runtime?: {enabled?:boolean;status?:string;runtimeStatus?:string} };
type RawIssue = Record<string, unknown>;
const issues = ref<GuadaoIssue[]>([]);
const summary = ref<Record<string, number>>({});
const runtime = ref<IssuesPayload["runtime"]>({});
const selectedId = ref<string | number | null>(null);
const loading = ref(false);
const busy = ref(false);
const error = ref("");
const statusFilter = ref(""); const severityFilter = ref(""); const accountFilter = ref(""); const typeFilter = ref(""); const keyword = ref(""); const showAcknowledged = ref(false);
const ackOpen = ref(false); const ackReason = ref("");
const reviewOpen = ref(false); const reviewBusy = ref(false); const notice = ref("");
let timer: ReturnType<typeof setInterval> | null = null;

const filtered = computed(() => issues.value.filter(issue => {
  if (!showAcknowledged.value && issue.acknowledged) return false;
  if (statusFilter.value && issue.status !== statusFilter.value) return false;
  if (severityFilter.value && issue.severity !== severityFilter.value) return false;
  if (accountFilter.value && issue.accountName !== accountFilter.value) return false;
  if (typeFilter.value && issue.issueType !== typeFilter.value) return false;
  const haystack = `${issue.title || ""} ${issue.summary || ""} ${issue.marketHashName || ""} ${issue.accountName || ""}`.toLowerCase();
  return !keyword.value.trim() || haystack.includes(keyword.value.trim().toLowerCase());
}));
const selected = computed(() => filtered.value.find(issue => issue.id === selectedId.value) || filtered.value[0] || null);
const accounts = computed(() => [...new Set(issues.value.map(row => row.accountName).filter(Boolean))] as string[]);
const types = computed(() => [...new Set(issues.value.map(row => row.issueType).filter(Boolean))] as string[]);
const metrics = computed(() => [["全部问题", summary.value.total ?? issues.value.filter(i => !i.acknowledged).length],["待安全复核",summary.value.pendingReview ?? 0],["Steam 待处理",summary.value.steam ?? 0],["C5 待处理",summary.value.c5 ?? 0],["本地状态异常",summary.value.local ?? 0]]);
const runtimeText = computed(() => { const value=String(runtime.value?.runtimeStatus||runtime.value?.status||"");if(value==="closing_only")return"存量闭环中";if(value==="preparing")return"启动准备中";return runtime.value?.enabled?"运行中":"已关闭"; });
const canQueueSafeReview = computed(() => Boolean(selected.value?.canQueueSafeReview));

function severityText(value?: string): string { return value === "high" || value === "critical" ? "高" : value === "medium" ? "中" : value === "low" ? "低" : value || "未分级"; }
function normalizeIssue(raw: RawIssue): GuadaoIssue {
  const status = String(raw.status || "manual_required");
  const titleByStatus: Record<string, string> = { manual_required: "需要人工安全复核", listing_failed: "Steam 上架状态异常", failed: "挂刀流水执行失败" };
  const fallbackEvidence = [
    ["operationId", raw.operationId], ["assetId", raw.assetId], ["listingId", raw.listingId], ["SteamID", raw.steamId],
  ].filter((row) => row[1] != null && String(row[1]).trim()).map((row) => ({ label: String(row[0]), value: String(row[1]) }));
  const evidence = Array.isArray(raw.evidence) ? raw.evidence as GuadaoIssue["evidence"] : fallbackEvidence;
  const timeline = Array.isArray(raw.timeline)
    ? raw.timeline as GuadaoIssue["timeline"]
    : raw.createdAt
      ? [{ at: String(raw.createdAt), label: "问题进入待处理", detail: String(raw.reason || status) }]
      : [];
  return {
    id: String(raw.issueId || raw.id || raw.operationId || ""),
    issueType: String(raw.issueType || status),
    title: String(raw.title || titleByStatus[status] || status),
    severity: String(raw.severity || (status === "manual_required" ? "high" : "medium")),
    status,
    accountName: String(raw.accountName || raw.accountId || "") || null,
    marketHashName: String(raw.marketHashName || raw.nameCn || "") || null,
    summary: String(raw.summary || raw.reason || "") || null,
    detail: String(raw.detail || raw.reason || "") || null,
    firstSeenAt: String(raw.firstSeenAt || raw.createdAt || "") || null,
    lastSeenAt: String(raw.lastSeenAt || raw.createdAt || "") || null,
    repeatCount: Number(raw.repeatCount || 1),
    acknowledged: Boolean(raw.acknowledged),
    evidence,
    timeline,
    recommendation: typeof raw.recommendation === "string" ? raw.recommendation : null,
    accountId: String(raw.accountId || "") || null,
    operationId: (raw.operationId as string | number | null) ?? null,
    assetId: String(raw.assetId || "") || null,
    listingId: String(raw.listingId || "") || null,
    steamId: String(raw.steamId || "") || null,
    category: String(raw.category || "") || null,
    rawStatus: String(raw.rawStatus || "") || null,
    canQueueSafeReview: Boolean(raw.canQueueSafeReview),
    safeReviewBlockReason: String(raw.safeReviewBlockReason || "") || null,
  };
}
async function refresh(): Promise<void> { loading.value = true; try { const response=await fetch("/api/guadao/issues?acknowledged=all",{cache:"no-store"});if(!response.ok)throw new Error(await responseError(response));const data=unwrapPayload<IssuesPayload & {items?:RawIssue[]}>(await response.json());const rows=(data.items||data.issues||[]) as RawIssue[];issues.value=rows.map(normalizeIssue);summary.value=data.summary||{};runtime.value=data.runtime||{};if(selectedId.value==null||!issues.value.some(i=>i.id===selectedId.value))selectedId.value=issues.value[0]?.id??null;error.value="";}catch(reason){error.value=reason instanceof Error?reason.message:String(reason);}finally{loading.value=false;} }
async function acknowledge(value: boolean): Promise<void> { if(!selected.value)return; busy.value=true; try{const response=await fetch("/api/guadao/issues/ack",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({issueId:selected.value.id,acknowledged:value,reason:ackReason.value.trim()||null})});if(!response.ok)throw new Error(await responseError(response));ackOpen.value=false;ackReason.value="";await refresh();}catch(reason){error.value=reason instanceof Error?reason.message:String(reason);}finally{busy.value=false;} }
function relatedLogsTo(issue:GuadaoIssue):{path:string;query:Record<string,string>}{return{path:"/guadao/logs",query:{operationId:String(issue.operationId||issue.id),marketHashName:issue.marketHashName||"",account:issue.accountName||""}}}
async function confirmSafeReview():Promise<void>{if(!selected.value)return;reviewBusy.value=true;notice.value="";try{const response=await fetch("/api/guadao/issues/review",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({issueId:selected.value.id})});if(!response.ok)throw new Error(await responseError(response));const payload=await response.json() as {message?:string};reviewOpen.value=false;notice.value=payload.message||"安全复核已进入统一到期任务队列。";await refresh();}catch(reason){error.value=reason instanceof Error?reason.message:String(reason);}finally{reviewBusy.value=false;}}
function startPolling():void{if(timer===null)timer=setInterval(()=>void refresh(),15_000)}function stopPolling():void{if(timer!==null)clearInterval(timer);timer=null}
onMounted(()=>{void refresh();startPolling()});onActivated(startPolling);onDeactivated(stopPolling);onUnmounted(stopPolling);
</script>

<template>
  <main class="page issues-page">
    <header class="issues-heading"><div><p class="eyebrow">Guadao Operations</p><h1>异常与待处理</h1></div><RouterLink class="executor-state" to="/guadao/overview"><span></span>挂刀执行器 · {{runtimeText}} · 前往总览控制</RouterLink></header>
    <div class="policy-banner"><FolioIcon name="success" :size="15" /><strong>C5 超过 24 小时未发货会自动判定补仓失败并创建替换补仓，不进入本页。</strong></div>
    <p v-if="error" class="api-error">异常 API 请求失败：{{ error }}</p>
    <p v-else-if="notice" class="review-notice">{{notice}}</p>
    <section class="issue-metrics"><article v-for="metric in metrics" :key="String(metric[0])"><span>{{ metric[0] }}</span><strong>{{ metric[1] }}</strong></article></section>
    <section class="issue-filters"><label><span>状态</span><select v-model="statusFilter"><option value="">全部</option><option value="open">待处理</option><option value="monitoring">观察中</option></select></label><label><span>严重程度</span><select v-model="severityFilter"><option value="">全部</option><option value="critical">严重</option><option value="high">高</option><option value="medium">中</option><option value="low">低</option></select></label><label><span>账号</span><select v-model="accountFilter"><option value="">全部</option><option v-for="name in accounts" :key="name" :value="name">{{ name }}</option></select></label><label><span>问题类别</span><select v-model="typeFilter"><option value="">全部</option><option v-for="type in types" :key="type" :value="type">{{ type }}</option></select></label><label class="ack-filter"><input v-model="showAcknowledged" type="checkbox" />显示已知晓</label><label class="keyword"><input v-model="keyword" placeholder="订单号 / 物品名 / 账号 / 消息" /></label></section>
    <section class="issue-workbench">
      <div class="issue-list"><h2>异常列表（{{ filtered.length }}）</h2><article v-for="issue in filtered" :key="issue.id" :class="['issue-card',issue.severity,{selected:selected?.id===issue.id,acknowledged:issue.acknowledged}]" @click="selectedId=issue.id"><div class="issue-card-head"><span class="issue-icon"><FolioIcon :name="issue.severity==='critical'||issue.severity==='high'?'shield':'warning'" :size="18" /></span><strong>{{ issue.title || issue.issueType || "未命名问题" }}</strong><b>严重程度：{{ severityText(issue.severity) }}</b></div><dl><div><dt>物品</dt><dd>{{ issue.marketHashName || "—" }}</dd></div><div><dt>账号</dt><dd>{{ issue.accountName || "—" }}</dd></div></dl><p>{{ issue.summary || issue.detail || "后端未提供问题摘要。" }}</p><footer><span>首次 {{ formatLocal(issue.firstSeenAt) }}</span><span>最近 {{ formatLocal(issue.lastSeenAt) }}</span><span>重复 {{ issue.repeatCount || 1 }} 次</span></footer></article><div v-if="!filtered.length" class="empty-state">{{loading?"正在读取异常…":"当前筛选没有需要人工处理的问题。"}}</div></div>
      <aside class="panel issue-detail"><template v-if="selected"><header><div><span class="issue-icon"><FolioIcon name="shield" :size="17" /></span><strong>{{selected.title||selected.issueType}}</strong></div><b>严重程度：{{severityText(selected.severity)}}</b></header><section><h3>证据汇总</h3><ul v-if="selected.evidence?.length"><li v-for="(row,index) in selected.evidence" :key="index"><span>{{row.label||"证据"}}</span><strong>{{row.value||"—"}}</strong></li></ul><p v-else>{{selected.detail||selected.summary||"后端暂未提供证据详情。"}}</p></section><section><h3>时间线</h3><div v-if="selected.timeline?.length" class="issue-timeline"><article v-for="(event,index) in selected.timeline" :key="index"><i></i><div><strong>{{event.label||"状态更新"}}</strong><span>{{event.detail}}</span></div><time>{{formatLocal(event.at)}}</time></article></div><p v-else>后端暂未返回问题时间线。</p></section><section class="recommend"><h3>推荐操作</h3><p>{{selected.recommendation||"请结合远端 Steam/C5 终态证据完成安全复核，避免误推进或重复交易。"}}</p></section><div class="detail-actions"><button v-if="canQueueSafeReview" class="primary-button" type="button" @click="reviewOpen=true">立即安全复核</button><RouterLink class="secondary-button" :to="relatedLogsTo(selected)">查看关联日志</RouterLink><button class="secondary-button" type="button" @click="ackOpen=true">{{selected.acknowledged?"修改知晓记录":"知晓并隐藏"}}</button></div><p v-if="!canQueueSafeReview" class="ack-note">{{selected.safeReviewBlockReason||"该问题不能自动发起 Steam 复核，请按推荐操作核对远端终态证据。"}}</p><p v-if="selected.acknowledged" class="ack-note">该问题已知晓；打开“显示已知晓”后可恢复查看，原始问题和日志不会删除。</p></template><div v-else class="empty-state">选择左侧问题查看证据、时间线与安全操作。</div></aside>
    </section>
    <div v-if="ackOpen" class="modal-backdrop" @click.self="ackOpen=false"><section class="ack-dialog"><h2>{{selected?.acknowledged?"更新知晓状态":"知晓并隐藏问题"}}</h2><p>此操作只改变默认列表显示，不删除异常、流水或日志，也不会推进任何远端状态。</p><label><span>处理备注（可选）</span><textarea v-model="ackReason" rows="3" placeholder="记录判断依据，便于后续审计"></textarea></label><div><button class="secondary-button" @click="ackOpen=false">取消</button><button v-if="selected?.acknowledged" class="secondary-button" :disabled="busy" @click="acknowledge(false)">恢复到默认列表</button><button class="primary-button" :disabled="busy" @click="acknowledge(true)">{{busy?"保存中…":"确认知晓"}}</button></div></section></div>
    <div v-if="reviewOpen" class="modal-backdrop" @click.self="reviewOpen=false"><section class="ack-dialog"><h2>确认立即安全复核</h2><p>系统只会把关联 Steam 账号的状态同步任务排到统一调度队列，不会在当前页面直接请求 Steam。复核仍服从迁移保护、Cookie 门禁、Retry-After 和终态证据规则；若确认已经卖出，已有流水可能继续进入补仓闭环。</p><div><button class="secondary-button" @click="reviewOpen=false">返回检查</button><button class="primary-button" :disabled="reviewBusy" @click="confirmSafeReview">{{reviewBusy?"排队中…":"确认排队复核"}}</button></div></section></div>
  </main>
</template>

<style scoped>
.issues-page{width:min(1320px,calc(100vw - 44px));gap:14px}.issues-heading{display:flex;justify-content:space-between;align-items:center;padding:7px 8px}.issues-heading h1{margin:0;font-size:32px;letter-spacing:-.045em}.executor-state{display:flex;align-items:center;gap:7px;border:1px solid var(--folio-line);border-radius:10px;padding:9px 12px;color:var(--folio-green);background:#fff;font-size:11px;font-weight:700}.executor-state span{width:7px;height:7px;border-radius:50%;background:currentColor}.policy-banner{display:flex;align-items:center;gap:8px;border-radius:10px;padding:9px 14px;color:var(--folio-green);background:linear-gradient(90deg,var(--folio-green-soft),rgba(232,242,236,.45));font-size:11px}.api-error{margin:0;border-radius:10px;padding:9px 12px;color:var(--folio-red);background:var(--folio-red-soft);font-size:12px}.issue-metrics{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}.issue-metrics article{min-height:83px;display:grid;align-content:space-between;border:1px solid var(--folio-line);border-radius:14px;padding:13px 15px;background:#fff}.issue-metrics span{color:var(--folio-muted);font-size:10px}.issue-metrics strong{font-size:22px}.issue-filters{display:grid;grid-template-columns:repeat(4,minmax(120px,.7fr)) auto 1.5fr;gap:10px;align-items:end}.issue-filters label:not(.ack-filter){display:grid;gap:5px}.issue-filters label>span{color:var(--folio-muted);font-size:9px}.issue-filters select,.issue-filters input[type=text],.keyword input{min-height:38px;border:1px solid #dfe4df;border-radius:9px;padding:7px 10px;background:#fff}.ack-filter{min-height:38px;display:flex;align-items:center;gap:6px;color:var(--folio-muted);font-size:10px}.ack-filter input{accent-color:var(--folio-green)}.issue-workbench{display:grid;grid-template-columns:minmax(0,.95fr) minmax(0,1.05fr);gap:12px;align-items:start}.issue-list>h2{margin:0 0 10px;font-size:15px}.issue-card{margin-bottom:10px;border:1px solid #ead299;border-radius:14px;padding:16px;background:#fff;cursor:pointer}.issue-card.high,.issue-card.critical{border-color:#e5aca5}.issue-card.selected{box-shadow:0 0 0 2px rgba(35,106,76,.18)}.issue-card.acknowledged{opacity:.65}.issue-card-head{display:grid;grid-template-columns:auto 1fr auto;gap:9px;align-items:center}.issue-icon{display:grid;place-items:center;width:34px;height:34px;border-radius:10px;color:var(--folio-amber);background:var(--folio-amber-soft)}.high .issue-icon,.critical .issue-icon,.issue-detail .issue-icon{color:var(--folio-red);background:var(--folio-red-soft)}.issue-card-head strong{font-size:13px}.issue-card-head b,.issue-detail header>b{border-radius:999px;padding:4px 8px;color:var(--folio-red);background:var(--folio-red-soft);font-size:9px}.issue-card dl{display:grid;grid-template-columns:repeat(2,1fr);gap:9px;margin:13px 0}.issue-card dl div{display:grid;gap:2px}.issue-card dt{color:var(--folio-muted);font-size:9px}.issue-card dd{margin:0;font-size:10px;font-weight:650}.issue-card p{margin:0;color:#4c5751;font-size:10px;line-height:1.65}.issue-card footer{display:flex;gap:15px;margin-top:12px;border-top:1px solid var(--folio-line);padding-top:9px;color:var(--folio-muted);font-size:9px}.issue-detail{position:sticky;top:122px;min-height:580px}.issue-detail header,.issue-detail header>div{display:flex;align-items:center}.issue-detail header{justify-content:space-between;gap:12px;border-bottom:1px solid var(--folio-line);padding-bottom:12px}.issue-detail header>div{gap:8px}.issue-detail header strong{font-size:14px}.issue-detail section{margin-top:15px}.issue-detail h3{margin:0 0 8px;font-size:12px}.issue-detail section>p{margin:0;color:var(--folio-muted);font-size:10px;line-height:1.65}.issue-detail ul{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin:0;padding:0;list-style:none}.issue-detail li{display:grid;gap:3px;border:1px solid var(--folio-line);border-radius:9px;padding:8px;background:var(--folio-surface-soft)}.issue-detail li span{color:var(--folio-muted);font-size:8px}.issue-detail li strong{overflow-wrap:anywhere;font-size:9px}.issue-timeline article{position:relative;display:grid;grid-template-columns:12px 1fr auto;gap:7px;padding:6px 0}.issue-timeline i{width:10px;height:10px;border:2px solid var(--folio-green);border-radius:50%;background:#fff}.issue-timeline article>div{display:grid;gap:2px}.issue-timeline strong{font-size:9px}.issue-timeline span,.issue-timeline time{color:var(--folio-muted);font-size:8px}.recommend{border-radius:10px;padding:11px;color:#654d16;background:var(--folio-amber-soft)}.detail-actions{display:flex;gap:8px;margin-top:15px}.detail-actions a{display:inline-flex;align-items:center;text-decoration:none}.ack-note{margin-top:9px!important;color:var(--folio-muted)!important;font-size:9px!important}.empty-state{border:1px dashed #d8ded9;border-radius:11px;padding:30px;color:var(--folio-muted);text-align:center;background:var(--folio-surface-soft);font-size:11px}.modal-backdrop{position:fixed;inset:0;z-index:50;display:grid;place-items:center;background:rgba(20,31,25,.34);backdrop-filter:blur(3px)}.ack-dialog{width:480px;border:1px solid var(--folio-line);border-radius:18px;padding:24px;background:#fff;box-shadow:0 24px 70px rgba(20,59,46,.2)}.ack-dialog h2{margin:0;font-size:20px}.ack-dialog>p{color:var(--folio-muted);font-size:12px;line-height:1.65}.ack-dialog label{display:grid;gap:6px;color:var(--folio-muted);font-size:10px}.ack-dialog textarea{resize:vertical;border:1px solid #dfe4df;border-radius:11px;padding:10px;color:var(--folio-ink)}.ack-dialog>div{display:flex;justify-content:flex-end;gap:8px;margin-top:17px}
.executor-state{text-decoration:none}.review-notice{margin:0;border-radius:10px;padding:9px 12px;color:var(--folio-green);background:var(--folio-green-soft);font-size:11px}.detail-actions{flex-wrap:wrap}
</style>
