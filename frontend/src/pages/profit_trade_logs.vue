<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { RouterLink, useRoute } from "vue-router";
import FolioIcon from "../components/FolioIcon.vue";

type LogEvent = Record<string, unknown> & {
  event_id?: string; eventId?: string; timestamp_utc?: string; timestampUtc?: string;
  level?: string; source?: string; provider?: string; component?: string; operation?: string;
  message?: string; run_id?: string; runId?: string; trade_id?: number; tradeId?: number;
  trade_no?: string; tradeNo?: string; market_hash_name?: string; marketHashName?: string;
  asset_id?: string; assetId?: string; account_id?: string; accountId?: string;
  steam_id64?: string; steamId64?: string; request_id?: string; requestId?: string;
  client_instance_id?: string; clientInstanceId?: string; attempt?: number; method?: string;
  endpoint?: string; status_code?: number; statusCode?: number; elapsed_ms?: number; elapsedMs?: number;
  retry_after?: string | number | null; retryAfter?: string | number | null;
  state_from?: string; stateFrom?: string; state_to?: string; stateTo?: string;
  step_from?: string; stepFrom?: string; step_to?: string; stepTo?: string;
  exception_type?: string; exceptionType?: string; stack_trace?: string; stackTrace?: string;
  safe_context?: Record<string, unknown>; safeContext?: Record<string, unknown>;
};
type StorageStatus = {
  logDirectory?: string; retentionDays?: number; totalBytes?: number; fileCount?: number;
  compressedFileCount?: number; earliestTimestamp?: string | null; latestTimestamp?: string | null;
};
type LogsResponse = {
  items?: LogEvent[]; events?: LogEvent[]; nextCursor?: string | null; next_cursor?: string | null;
  hasMore?: boolean; has_more?: boolean; storage?: StorageStatus;
};

const route = useRoute();
const rows = ref<LogEvent[]>([]);
const nextCursor = ref<string | null>(null);
const hasMore = ref(false);
const storage = ref<StorageStatus>({ retentionDays: 90 });
const loading = ref(false);
const loadingMore = ref(false);
const error = ref("");
const connection = ref<"connecting"|"online"|"offline"|"paused">("connecting");
const paused = ref(false);
const queued = ref<LogEvent[]>([]);
const selected = ref<LogEvent | null>(null);
const detailLoading = ref(false);
const detailError = ref("");
const detailTab = ref("basic");
const from = ref(""); const to = ref(""); const level = ref(""); const provider = ref("");
const component = ref(""); const operation = ref(""); const steamId = ref("");
const tradeNo = ref(typeof route.query.tradeNo === "string" ? route.query.tradeNo : "");
const requestId = ref(""); const keywordDraft = ref(""); const keyword = ref("");
const pageSize = 100;
let stream: EventSource | null = null;

const levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"];
const connectionLabel = computed(() => ({ connecting: "正在连接", online: "SSE 已连接", offline: "SSE 已断开", paused: "实时显示已暂停" })[connection.value]);

function pick<T = unknown>(event: LogEvent, camel: string, snake: string): T | undefined {
  return (event[camel] ?? event[snake]) as T | undefined;
}
function id(event: LogEvent): string { return String(pick(event,"eventId","event_id") || "-"); }
function timestamp(event: LogEvent): string { return String(pick(event,"timestampUtc","timestamp_utc") || ""); }
function trade(event: LogEvent): string { return String(pick(event,"tradeNo","trade_no") || "-"); }
function request(event: LogEvent): string { return String(pick(event,"requestId","request_id") || "-"); }
function steam(event: LogEvent): string { return String(pick(event,"steamId64","steam_id64") || pick(event,"accountId","account_id") || "-"); }
function statusCode(event: LogEvent): number | undefined { return pick<number>(event,"statusCode","status_code"); }
function elapsed(event: LogEvent): number | undefined { return pick<number>(event,"elapsedMs","elapsed_ms"); }
function context(event: LogEvent): Record<string,unknown> { return pick<Record<string,unknown>>(event,"safeContext","safe_context") || {}; }
function frequency(event: LogEvent, key: "last_10_seconds"|"last_60_seconds"|"last_5_minutes"|"current_concurrent"): unknown {
  const value = context(event).request_frequency;
  return value && typeof value === "object" ? (value as Record<string,unknown>)[key] ?? "未记录" : "未记录";
}
function time(value?: string | null): string {
  if (!value) return "-"; const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return `${parsed.toLocaleString("zh-CN", { hour12: false })}.${String(parsed.getMilliseconds()).padStart(3, "0")}`;
}
function bytes(value?: number): string {
  const amount = Number(value) || 0; if (amount < 1024) return `${amount} B`;
  if (amount < 1024 ** 2) return `${(amount/1024).toFixed(1)} KB`;
  if (amount < 1024 ** 3) return `${(amount/1024**2).toFixed(1)} MB`;
  return `${(amount/1024**3).toFixed(2)} GB`;
}
function levelClass(value?: string): string { return String(value || "INFO").toLowerCase(); }
function serviceLabel(value?: string): string { return ({ steam: "Steam", c5: "C5", local: "本地" } as Record<string,string>)[String(value)] || String(value || "本地"); }
function apiTime(value: string): string {
  if (!value) return "";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toISOString();
}
function filters(includePaging = true): URLSearchParams {
  const query = new URLSearchParams();
  const values: Record<string,string> = { from:apiTime(from.value),to:apiTime(to.value),level:level.value,provider:provider.value,component:component.value,operation:operation.value,steamId:steamId.value,tradeNo:tradeNo.value,requestId:requestId.value,keyword:keyword.value };
  Object.entries(values).forEach(([key,value]) => { if (value.trim()) query.set(key,value.trim()); });
  if (includePaging) query.set("pageSize", String(pageSize));
  return query;
}
async function responseError(response: Response): Promise<string> {
  try { const payload = await response.json() as { error?: string; detail?: string }; return payload.error || payload.detail || response.statusText; }
  catch { return response.statusText; }
}
function normalize(payload: LogsResponse): LogEvent[] { return Array.isArray(payload.items) ? payload.items : Array.isArray(payload.events) ? payload.events : []; }

async function load(reset = true): Promise<void> {
  if (reset) { loading.value = true; error.value = ""; }
  else loadingMore.value = true;
  const query = filters(); if (!reset && nextCursor.value) query.set("cursor", nextCursor.value);
  try {
    const response = await fetch(`/api/profit-trade/logs?${query}`, { cache: "no-store" });
    if (!response.ok) throw new Error(await responseError(response));
    const payload = await response.json() as LogsResponse; const events = normalize(payload);
    rows.value = reset ? events : [...rows.value, ...events];
    nextCursor.value = payload.nextCursor ?? payload.next_cursor ?? null;
    hasMore.value = Boolean(payload.hasMore ?? payload.has_more ?? nextCursor.value);
    if (payload.storage) storage.value = { ...storage.value, ...payload.storage };
  } catch (cause) { if (reset) rows.value = []; error.value = `日志读取失败：${cause instanceof Error ? cause.message : String(cause)}`; }
  finally { loading.value = false; loadingMore.value = false; }
}
function apply(): void { keyword.value = keywordDraft.value.trim(); queued.value = []; void load(); connect(); }
function resetFilters(): void {
  from.value="";to.value="";level.value="";provider.value="";component.value="";operation.value="";steamId.value="";tradeNo.value="";requestId.value="";keywordDraft.value="";keyword.value="";queued.value=[];void load();connect();
}
function onLog(event: MessageEvent<string>): void {
  try {
    const parsed = JSON.parse(event.data) as LogEvent;
    if (String(parsed.source || "profit_trade") !== "profit_trade") return;
    if (paused.value) queued.value.push(parsed);
    else { rows.value = [parsed, ...rows.value.filter(item => id(item) !== id(parsed))].slice(0, 1000); void nextTick(() => document.querySelector(".logs-table-wrap")?.scrollTo({ top:0, behavior:"smooth" })); }
  } catch { /* A malformed event is ignored without breaking the stream. */ }
}
function connect(): void {
  stream?.close(); stream = null;
  if (paused.value) { connection.value = "paused"; return; }
  connection.value = "connecting";
  const query = filters(false); query.set("source","profit_trade");
  stream = new EventSource(`/api/profit-trade/logs/stream?${query}`);
  stream.addEventListener("open", () => { connection.value = paused.value ? "paused" : "online"; });
  stream.addEventListener("log", onLog as EventListener);
  stream.addEventListener("profit_trade_log", onLog as EventListener);
  stream.onmessage = onLog;
  stream.addEventListener("heartbeat", () => { connection.value = paused.value ? "paused" : "online"; });
  stream.onerror = () => { connection.value = paused.value ? "paused" : "offline"; };
}
function togglePause(): void {
  paused.value = !paused.value;
  if (paused.value) {
    connection.value = "paused";
    return;
  }
  rows.value = [...queued.value.reverse(), ...rows.value].slice(0,1000);
  queued.value=[];
  if (!stream || stream.readyState === EventSource.CLOSED) connect();
  else connection.value = stream.readyState === EventSource.OPEN ? "online" : "connecting";
}
function exportUrl(format: "jsonl"|"log"): string { const query = filters(false); query.set("format",format); return `/api/profit-trade/logs/export?${query}`; }
async function openDetail(event: LogEvent): Promise<void> {
  selected.value = event; detailTab.value = "basic"; detailLoading.value = true; detailError.value = "";
  const eventId = id(event);
  if (eventId === "-") { detailLoading.value = false; return; }
  try {
    const response = await fetch(`/api/profit-trade/logs/event?eventId=${encodeURIComponent(eventId)}`, { cache: "no-store" });
    if (!response.ok) throw new Error(await responseError(response));
    const payload = await response.json() as { event?: LogEvent | null };
    if (payload.event) selected.value = payload.event;
  } catch (cause) {
    detailError.value = `完整事件读取失败：${cause instanceof Error ? cause.message : String(cause)}`;
  } finally { detailLoading.value = false; }
}
watch(() => route.query.tradeNo, value => { if (typeof value === "string" && value !== tradeNo.value) { tradeNo.value=value; apply(); } });
onMounted(() => { void load(); connect(); });
onUnmounted(() => { stream?.close(); stream=null; });
</script>

<template>
  <main class="logs-page">
    <header class="logs-title"><div><p class="eyebrow">S3 · Profit Trade 可观测性</p><h1>实时执行日志</h1><p>仅记录并展示 <code>source=profit_trade</code> 的扫描、Steam、C5、本地状态机和异常活动；不读取挂刀执行器日志。</p></div><div class="stream-actions"><span :class="['connection',connection]"><i></i>{{ connectionLabel }}</span><button class="secondary-button" type="button" @click="togglePause"><FolioIcon :name="paused ? 'play':'pause'" :size="15" />{{ paused ? `继续实时显示${queued.length ? `（${queued.length}）`:''}` : "暂停实时显示" }}</button><button class="secondary-button" type="button" @click="connect"><FolioIcon name="refresh" :size="15" />重新连接</button></div></header>

    <section class="storage-strip"><div><span>日志来源</span><strong>Profit Trade</strong></div><div><span>保留策略</span><strong>{{ storage.retentionDays || 90 }} 天全量 · 按日压缩</strong></div><div><span>磁盘占用</span><strong>{{ bytes(storage.totalBytes) }}</strong></div><div><span>日志文件</span><strong>{{ storage.fileCount ?? "-" }} 个（压缩 {{ storage.compressedFileCount ?? "-" }}）</strong></div><div><span>时间范围</span><strong>{{ time(storage.earliestTimestamp) }} — {{ time(storage.latestTimestamp) }}</strong></div></section>

    <form class="log-filters" @submit.prevent="apply">
      <label><span>开始时间</span><input v-model="from" type="datetime-local"></label><label><span>结束时间</span><input v-model="to" type="datetime-local"></label>
      <label><span>级别</span><select v-model="level"><option value="">全部</option><option v-for="item in levels" :key="item">{{ item }}</option></select></label>
      <label><span>服务</span><select v-model="provider"><option value="">Steam / C5 / 本地</option><option value="steam">Steam</option><option value="c5">C5</option><option value="local">本地状态机</option></select></label>
      <label><span>组件</span><input v-model="component" type="text" placeholder="steam_market"></label><label><span>操作</span><input v-model="operation" type="text" placeholder="search_listings"></label>
      <label><span>Steam 账号</span><input v-model="steamId" type="text" placeholder="SteamId64"></label><label><span>交易号</span><input v-model="tradeNo" type="text" placeholder="PT-..."></label>
      <label><span>request_id</span><input v-model="requestId" type="text" placeholder="req_..."></label><label class="keyword"><span>关键词</span><input v-model="keywordDraft" type="search" placeholder="摘要、异常、饰品名"></label>
      <button class="primary-button" type="submit">应用筛选</button><button class="secondary-button" type="button" @click="resetFilters">重置</button>
    </form>

    <section class="export-bar"><p><FolioIcon name="shield" :size="15" />Cookie、sessionid、密码、API key、Steam Guard secret 与认证请求体不落盘；历史未保存字段显示“未记录”。</p><div><a :href="exportUrl('jsonl')" class="secondary-button">下载完整 JSONL</a><a :href="exportUrl('log')" class="secondary-button">下载可读 .log</a></div></section>
    <p v-if="error" class="log-error">{{ error }}。不会使用浏览器假日志或静态文件替代。</p>

    <section class="logs-panel">
      <div class="logs-table-wrap"><table><thead><tr><th>北京时间</th><th>级别</th><th>服务</th><th>组件 / 操作</th><th>交易号</th><th>Steam 账号</th><th>HTTP</th><th>耗时</th><th>摘要</th></tr></thead><tbody><tr v-if="loading"><td colspan="9" class="empty">正在读取日志…</td></tr><tr v-else-if="rows.length===0"><td colspan="9" class="empty">当前筛选条件下没有 Profit Trade 日志。</td></tr><tr v-for="event in rows" v-else :key="id(event)" :class="{ selected:id(selected || {})===id(event) }" @click="openDetail(event)"><td class="time">{{ time(timestamp(event)) }}</td><td><span :class="['level',levelClass(event.level)]">{{ event.level || "INFO" }}</span></td><td><span class="provider">{{ serviceLabel(event.provider) }}</span></td><td><strong>{{ event.component || "-" }}</strong><small>{{ event.operation || "-" }}</small></td><td>{{ trade(event) }}</td><td class="mono">{{ steam(event) }}</td><td><span :class="{ httpError:(statusCode(event)||0)>=400 }">{{ event.method || "" }} {{ statusCode(event) ?? "-" }}</span></td><td>{{ elapsed(event) === undefined ? "-" : `${elapsed(event)} ms` }}</td><td class="message">{{ event.message || "-" }}</td></tr></tbody></table></div>
      <footer><span>已加载 {{ rows.length }} 条；实时区域最多保留最近 1000 条，完整记录以 90 天日志文件为准。</span><button class="secondary-button" type="button" :disabled="!hasMore || loadingMore" @click="load(false)">{{ loadingMore ? "加载中" : hasMore ? "加载更早日志" : "没有更多" }}</button></footer>
    </section>

    <section v-if="selected" class="event-detail panel">
      <header><div><p class="eyebrow">事件详情</p><h2>{{ selected.component || "-" }} · {{ selected.operation || "-" }}</h2><small>{{ id(selected) }}</small></div><div><RouterLink v-if="trade(selected)!=='-'" :to="{ path:'/profit-trade/interruptions',query:{tradeNo:trade(selected)} }" class="detail-link"><FolioIcon name="link" :size="14" />查看关联流水</RouterLink><button type="button" @click="selected=null">×</button></div></header>
      <nav><button v-for="tab in [{key:'basic',label:'基本信息'},{key:'request',label:'请求与响应'},{key:'frequency',label:'请求频率'},{key:'links',label:'关联链路与异常'},{key:'raw',label:'完整脱敏 JSON'}]" :key="tab.key" type="button" :class="{active:detailTab===tab.key}" @click="detailTab=tab.key">{{ tab.label }}</button></nav>
      <p v-if="detailError" class="detail-error">{{ detailError }}；当前仍展示列表中已有的脱敏事件。</p>
      <div v-if="detailLoading" class="detail-loading">正在读取完整事件…</div>
      <div v-if="detailTab==='basic'" class="detail-grid"><dl><dt>时间（UTC）</dt><dd>{{ timestamp(selected) || "-" }}</dd></dl><dl><dt>时间（北京时间）</dt><dd>{{ time(timestamp(selected)) }}</dd></dl><dl><dt>来源</dt><dd>{{ selected.source || "profit_trade" }}</dd></dl><dl><dt>服务</dt><dd>{{ serviceLabel(selected.provider) }}</dd></dl><dl><dt>run_id</dt><dd>{{ pick(selected,'runId','run_id') || "-" }}</dd></dl><dl><dt>trade_no</dt><dd>{{ trade(selected) }}</dd></dl><dl><dt>饰品</dt><dd>{{ pick(selected,'marketHashName','market_hash_name') || "-" }}</dd></dl><dl><dt>asset_id</dt><dd>{{ pick(selected,'assetId','asset_id') || "-" }}</dd></dl><dl class="wide"><dt>消息</dt><dd>{{ selected.message || "-" }}</dd></dl></div>
      <div v-else-if="detailTab==='request'" class="detail-grid"><dl><dt>request_id</dt><dd>{{ request(selected) }}</dd></dl><dl><dt>client_instance_id</dt><dd>{{ pick(selected,'clientInstanceId','client_instance_id') || "-" }}</dd></dl><dl><dt>方法</dt><dd>{{ selected.method || "-" }}</dd></dl><dl><dt>脱敏 endpoint</dt><dd>{{ selected.endpoint || "-" }}</dd></dl><dl><dt>HTTP 状态</dt><dd>{{ statusCode(selected) ?? "未返回" }}</dd></dl><dl><dt>耗时</dt><dd>{{ elapsed(selected) === undefined ? "未记录" : `${elapsed(selected)} ms` }}</dd></dl><dl><dt>attempt</dt><dd>{{ selected.attempt ?? "-" }}</dd></dl><dl><dt>Retry-After</dt><dd>{{ pick(selected,'retryAfter','retry_after') ?? "未返回" }}</dd></dl><dl class="wide"><dt>安全上下文</dt><dd><pre>{{ JSON.stringify(context(selected),null,2) }}</pre></dd></dl></div>
      <div v-else-if="detailTab==='frequency'" class="detail-grid"><dl><dt>近 10 秒 Steam 请求</dt><dd>{{ frequency(selected,"last_10_seconds") }}</dd></dl><dl><dt>近 60 秒 Steam 请求</dt><dd>{{ frequency(selected,"last_60_seconds") }}</dd></dl><dl><dt>近 5 分钟 Steam 请求</dt><dd>{{ frequency(selected,"last_5_minutes") }}</dd></dl><dl><dt>当前并发请求</dt><dd>{{ frequency(selected,"current_concurrent") }}</dd></dl><dl><dt>距上次请求</dt><dd>{{ context(selected).ms_since_previous_request ?? context(selected).msSincePreviousRequest ?? "未记录" }}</dd></dl><dl><dt>Steam 账号</dt><dd>{{ steam(selected) }}</dd></dl><p class="wide-note">这些计数只覆盖 Profit Trade 自己发起的 Steam 请求，不能单独证明挂刀执行器是否影响了 429；挂刀日志上线后可按毫秒和账号另行对照。</p></div>
      <div v-else-if="detailTab==='links'" class="detail-grid"><dl><dt>状态迁移</dt><dd>{{ pick(selected,'stateFrom','state_from') || "-" }} → {{ pick(selected,'stateTo','state_to') || "-" }}</dd></dl><dl><dt>步骤迁移</dt><dd>{{ pick(selected,'stepFrom','step_from') || "-" }} → {{ pick(selected,'stepTo','step_to') || "-" }}</dd></dl><dl><dt>异常类型</dt><dd>{{ pick(selected,'exceptionType','exception_type') || "-" }}</dd></dl><dl><dt>是否取得 listingId</dt><dd>{{ context(selected).listing_id_obtained ?? context(selected).listingIdObtained ?? "未记录" }}</dd></dl><dl><dt>是否发送购买请求</dt><dd>{{ context(selected).purchase_request_sent ?? context(selected).purchaseRequestSent ?? "未记录" }}</dd></dl><dl><dt>关联 trade_id</dt><dd>{{ pick(selected,'tradeId','trade_id') || "-" }}</dd></dl><dl class="wide"><dt>脱敏堆栈</dt><dd><pre>{{ pick(selected,'stackTrace','stack_trace') || "未记录" }}</pre></dd></dl></div>
      <pre v-else class="raw-json">{{ JSON.stringify(selected,null,2) }}</pre>
    </section>
  </main>
</template>

<style scoped>
.logs-page{width:min(1380px,calc(100vw - 40px));margin:0 auto;padding:22px 0 38px;display:grid;gap:12px;color:#17201c}.logs-title{display:flex;justify-content:space-between;gap:24px;align-items:flex-start}.logs-title h1{margin:0;font-size:24px}.logs-title p:not(.eyebrow){margin:6px 0;color:#6f7872;font-size:12px}.logs-title code{padding:2px 4px;border-radius:4px;color:#205f45;background:#e7f1eb}.stream-actions{display:flex;align-items:center;gap:7px}.stream-actions button{display:inline-flex;align-items:center;gap:5px}.connection{display:inline-flex;align-items:center;gap:6px;padding:6px 9px;border:1px solid #dbe1db;border-radius:999px;color:#66716b;background:#fff;font-size:10px}.connection i{width:7px;height:7px;border-radius:50%;background:#99a19c}.connection.online i{background:#2a7b55}.connection.offline i{background:#b44a41}.connection.connecting i{background:#c09835;animation:pulse 1s infinite}.connection.paused i{background:#77817b}@keyframes pulse{50%{opacity:.3}}
.storage-strip{display:grid;grid-template-columns:140px 220px 130px 180px 1fr;border:1px solid #dfe5df;border-radius:8px;background:#fff}.storage-strip>div{display:grid;gap:2px;padding:10px 12px;border-right:1px solid #e6eae6}.storage-strip>div:last-child{border:0}.storage-strip span{color:#7a837e;font-size:9px}.storage-strip strong{font-size:11px;overflow-wrap:anywhere}.log-filters{display:grid;grid-template-columns:repeat(6,minmax(125px,1fr));gap:7px;align-items:end;padding:11px;border:1px solid #dfe5df;border-radius:8px;background:#fff}.log-filters label{display:grid;gap:3px;min-width:0}.log-filters .keyword{grid-column:span 2}.log-filters span{color:#717b75;font-size:9px;font-weight:650}.log-filters input,.log-filters select{width:100%;min-width:0;min-height:33px;border:1px solid #d7ddd7;border-radius:5px;padding:5px 7px;font-size:10px;background:#fff}.log-filters button{padding-inline:9px;font-size:10px}.export-bar{display:flex;justify-content:space-between;align-items:center;gap:15px;padding:9px 11px;border:1px solid #dce5de;border-radius:8px;background:#edf5f0}.export-bar p{display:flex;align-items:center;gap:7px;margin:0;color:#456052;font-size:10px}.export-bar>div{display:flex;gap:6px}.export-bar a{display:inline-flex;align-items:center;text-decoration:none;font-size:10px}.log-error{margin:0;padding:9px 11px;border:1px solid #e5b4ae;border-radius:7px;color:#8b372f;background:#fff7f5;font-size:11px}
.logs-panel{overflow:hidden;border:1px solid #dce3dc;border-radius:8px;background:#fff}.logs-table-wrap{max-height:460px;overflow:auto}.logs-panel table{width:100%;border-collapse:collapse;table-layout:fixed}.logs-panel th{position:sticky;top:0;z-index:1;padding:8px 9px;border-bottom:1px solid #dfe5df;text-align:left;color:#68736d;background:#f4f7f4;font-size:9px}.logs-panel th:nth-child(1){width:160px}.logs-panel th:nth-child(2){width:70px}.logs-panel th:nth-child(3){width:75px}.logs-panel th:nth-child(4){width:150px}.logs-panel th:nth-child(5){width:150px}.logs-panel th:nth-child(6){width:150px}.logs-panel th:nth-child(7){width:80px}.logs-panel th:nth-child(8){width:70px}.logs-panel td{padding:8px 9px;border-bottom:1px solid #edf0ed;vertical-align:top;font-size:9px;overflow-wrap:anywhere}.logs-panel tbody tr{cursor:pointer}.logs-panel tbody tr:hover,.logs-panel tbody tr.selected{background:#f0f6f2}.logs-panel td strong,.logs-panel td small{display:block}.logs-panel td small{margin-top:2px;color:#7a837e}.logs-panel .time,.mono{font-family:Consolas,"Courier New",monospace}.level{display:inline-block;padding:2px 5px;border-radius:4px;font-size:8px;font-weight:800}.level.debug{color:#5f6964;background:#edf0ed}.level.info{color:#1f6849;background:#e5f2e9}.level.warning{color:#80621d;background:#fbf1d2}.level.error,.level.critical{color:#933b32;background:#fbe8e5}.provider{font-weight:700}.httpError{color:#ad4037;font-weight:800}.message{color:#46534c}.empty{height:110px;text-align:center;color:#77817b}.logs-panel>footer{display:flex;justify-content:space-between;align-items:center;padding:9px 11px;color:#748078;background:#f8faf8;font-size:9px}.logs-panel>footer button{min-height:28px;font-size:9px}
.event-detail{padding:0;overflow:hidden;border-color:#dce3dc}.event-detail>header{display:flex;justify-content:space-between;gap:20px;padding:14px 16px;border-bottom:1px solid #e2e7e2}.event-detail h2{margin:0;font-size:16px}.event-detail header small{color:#77817b;font-family:Consolas,monospace;font-size:9px}.event-detail header>div:last-child{display:flex;align-items:flex-start;gap:7px}.event-detail header button{width:29px;height:29px;border:1px solid #d9dfd9;border-radius:6px;background:#fff;font-size:17px}.detail-link{display:inline-flex;align-items:center;gap:5px;padding:6px 8px;border:1px solid #bcd1c3;border-radius:6px;color:#205f45;text-decoration:none;background:#f1f7f3;font-size:9px;font-weight:700}.event-detail>nav{display:flex;gap:3px;padding:8px 14px;border-bottom:1px solid #e4e8e4;background:#f7f9f7}.event-detail>nav button{border:0;border-radius:5px;padding:6px 9px;color:#67726b;background:transparent;font-size:9px;font-weight:650}.event-detail>nav button.active{color:#174a36;background:#e5f0e9}.detail-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:#e6eae6}.detail-grid dl{min-width:0;margin:0;padding:11px;background:#fff}.detail-grid dt{color:#7a837e;font-size:9px}.detail-grid dd{margin:3px 0 0;font-size:10px;font-weight:650;overflow-wrap:anywhere}.detail-grid .wide{grid-column:1/-1}.detail-grid pre{max-height:210px;overflow:auto;margin:5px 0 0;padding:9px;border-radius:5px;color:#dceee3;background:#15231c;font:9px/1.5 Consolas,monospace}.wide-note{grid-column:1/-1;margin:0;padding:11px;color:#765d27;background:#fbf5df;font-size:10px}.raw-json{max-height:360px;overflow:auto;margin:0;padding:15px;color:#d7e9de;background:#15231c;font:9px/1.5 Consolas,monospace}
.detail-error{margin:0;padding:8px 14px;color:#8b372f;background:#fff3f1;font-size:10px}.detail-loading{padding:8px 14px;color:#5f6d65;background:#f7f9f7;font-size:10px}
</style>
