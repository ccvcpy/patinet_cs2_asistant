<script setup lang="ts">
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, ref, watch } from "vue";
import { RouterLink, useRoute } from "vue-router";
import FolioIcon from "../components/FolioIcon.vue";
import { formatCountdown, formatLocal, responseError, unwrapPayload } from "./guadao_shared";

type RebuyAttempt = {
  id: number;
  operationId?: string;
  status?: string;
  stage?: string;
  isCurrent?: boolean;
  createdAt?: string | null;
  completedAt?: string | null;
  expectedPrice?: number | null;
  actualPrice?: number | null;
  c5OrderId?: string | null;
  c5TradeOrderId?: string | null;
  c5OutTradeNo?: string | null;
  c5OrderSubmittedAt?: string | null;
  c5DeliveryDeadlineAt?: string | null;
  failureAt?: string | null;
  failureCode?: string | null;
  failureReason?: string | null;
  replacementOperationId?: number | null;
  replacementForOperationId?: number | null;
  replacementReason?: string | null;
  replacementMaxPrice?: number | null;
};
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
  c5TradeOrderId?: string | null;
  c5OutTradeNo?: string | null;
  steamListPrice?: number | null;
  steamNetAmount?: number | null;
  c5RebuyPrice?: number | null;
  frozenRebuyPrice?: number | null;
  currentRebuyRatio?: number | null;
  actualRebuyRatio?: number | null;
  manualRebuyRefrozenAt?: string | null;
  manualExternalRebuySource?: string | null;
  rebuyOperationId?: number | null;
  batchActionEligible?: boolean;
  batchActionBlockReason?: string | null;
  steamId?: string | null;
  steamSoldAt?: string | null;
  c5OrderSubmittedAt?: string | null;
  c5DeliveryDeadlineAt?: string | null;
  createdAt?: string | null;
  updatedAt?: string | null;
  nextAttemptAt?: string | null;
  nextTaskLabel?: string | null;
  nextTaskReason?: string | null;
  rebuyAttemptCount?: number;
  failedRebuyCount?: number;
  hasPreviousRebuyFailure?: boolean;
  rebuyAttempts?: RebuyAttempt[];
  timeline?: Array<{ at?: string; label?: string; detail?: string; status?: string }>;
};
type ItemOption = { marketHashName: string; displayName?: string | null; count: number };
type ResponsePayload = { operations?: Operation[]; total?: number; page?: number; pageSize?: number; summary?: Record<string, number>; itemOptions?: ItemOption[]; accounts?: Array<{id?:string;name?:string;steamId?:string}>; runtime?: {enabled?:boolean;status?:string;runtimeStatus?:string} };
type BatchResultItem = { operationId:number;tradeNo?:string;rebuyOperationId?:number|null;marketHashName?:string|null;ok:boolean;code:string;message:string;oldFrozenRebuyPrice?:number|null;newFrozenRebuyPrice?:number|null;oldFrozenRebuyRatio?:number|null;newFrozenRebuyRatio?:number|null;actualRebuyPrice?:number|null;actualRebuyRatio?:number|null;idempotentReplay?:boolean };
type BatchResponse = { ok?:boolean;batchId?:string;requestId?:string;successCount?:number;failedCount?:number;results?:BatchResultItem[] };

const steps = ["锁定资产", "Steam 上架", "挂单确认", "Steam 在售", "创建补仓", "C5 订单确认", "闭环"];
const operations = ref<Operation[]>([]);
const total = ref(0);
const summary = ref<Record<string, number>>({});
const accountOptions = ref<string[]>([]);
const itemOptions = ref<ItemOption[]>([]);
const selectedId = ref<string | number | null>(null);
const loading = ref(false);
const error = ref("");
const keyword = ref("");
const account = ref("");
const marketHashName = ref("");
const itemSearch = ref("");
const itemMenuOpen = ref(false);
const itemFilterRoot = ref<HTMLElement | null>(null);
const status = ref("");
const startAt = ref("");
const endAt = ref("");
const page = ref(1);
const pageSize = ref(10);
const route = useRoute();
const runtime = ref<ResponsePayload["runtime"]>({});
const selectedOperationIds = ref<number[]>([]);
const batchDialog = ref<"refreeze"|"manual"|null>(null);
const batchSubmitting = ref(false);
const batchError = ref("");
const batchResponse = ref<BatchResponse|null>(null);
const batchPrice = ref("");
const batchExecuteNow = ref(true);
const batchConfirmed = ref(false);
const batchReason = ref("C5 当前价格不合适，人工重新冻结补仓价格");
const manualSource = ref("其他平台");
const manualMemo = ref("");
const manualExternalRef = ref("");
const manualCompletedAt = ref("");
keyword.value = String(route.query.q || "");
let timer: ReturnType<typeof setInterval> | null = null;

const selected = computed(() => operations.value.find(row => row.id === selectedId.value) || operations.value[0] || null);
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)));
const accounts = computed(() => [...new Set([...accountOptions.value,...operations.value.map(row => row.accountName).filter(Boolean) as string[]])]);
const selectedItem = computed(() => itemOptions.value.find(row => row.marketHashName === marketHashName.value) || null);
const itemOptionTotal = computed(() => itemOptions.value.reduce((totalCount, row) => totalCount + Number(row.count || 0), 0));
const filteredItemOptions = computed(() => {
  const queryText = itemSearch.value.trim().toLocaleLowerCase();
  if (!queryText) return itemOptions.value;
  return itemOptions.value.filter(row => `${row.displayName || ""} ${row.marketHashName}`.toLocaleLowerCase().includes(queryText));
});
const batchModeEnabled = computed(() => status.value === "sold");
const selectedOperations = computed(() => operations.value.filter(row => selectedOperationIds.value.includes(Number(row.id))));
const selectablePageOperations = computed(() => operations.value.filter(row => row.status === "sold" && row.batchActionEligible));
const selectedMarketNames = computed(() => [...new Set(selectedOperations.value.map(row => row.marketHashName).filter(Boolean))]);
const batchSameItem = computed(() => selectedMarketNames.value.length === 1);
const selectedSteamNetTotal = computed(() => selectedOperations.value.reduce((sum,row)=>sum+Number(row.steamNetAmount||0),0));
const allSelectableSelected = computed(() => selectablePageOperations.value.length > 0 && selectablePageOperations.value.every(row => selectedOperationIds.value.includes(Number(row.id))));
const previewPrice = computed(() => Number(batchPrice.value));
const ratioPreviews = computed(() => selectedOperations.value.map(row => ({id:Number(row.id),oldRatio:row.currentRebuyRatio??row.maxRebuyRatioAtOpen??row.listingRatioAtOpen??null,newRatio:previewPrice.value>0&&Number(row.steamNetAmount)>0?previewPrice.value/Number(row.steamNetAmount):null})));
const ratioPreviewRange = computed(() => { const values=ratioPreviews.value.map(row=>row.newRatio).filter((value):value is number=>value!=null);if(!values.length)return"—";const min=Math.min(...values);const max=Math.max(...values);return Math.abs(max-min)<0.000001?pct(min):`${pct(min)} ～ ${pct(max)}`; });
const metricCards = computed(() => [
  ["全部", summary.value.total ?? total.value], ["挂单待确认", summary.value.pendingConfirmation ?? 0], ["Steam 在售", summary.value.steamListed ?? 0], ["已卖出待补仓", summary.value.pendingRebuy ?? 0], ["C5 补仓待查证据", summary.value.c5EvidencePending ?? summary.value.submissionUnconfirmed ?? 0], ["C5 已购买待收货", summary.value.deliveryPending ?? 0], ["已闭环", summary.value.completed ?? 0],
]);

function pct(value?: number | null): string { return value == null ? "—" : `${(value * (value <= 1 ? 100 : 1)).toFixed(2)}%`; }
function money(value?: number | null): string { return value == null ? "—" : `¥ ${Number(value).toFixed(2)}`; }
function stageTone(row: Operation): string { if (row.status === "completed") return "success"; if (row.status?.includes("failed") || row.status === "manual_required") return "danger"; return "warning"; }
function showRebuyFailureHistory(row?: Operation | null): boolean { return Boolean(row && ["sold","delivery_pending"].includes(row.status || "") && (row.failedRebuyCount || 0) > 0); }
function attemptTone(attempt: RebuyAttempt): string { if (attempt.status === "completed") return "success"; if (attempt.status?.includes("failed")) return "danger"; return "warning"; }
function attemptPrice(attempt: RebuyAttempt): string { return money(attempt.actualPrice ?? attempt.expectedPrice); }
function relatedLogsTo(row: Operation): { path: string; query: Record<string,string> } { return { path: "/guadao/logs", query: { operationId: String(row.operationId || row.id), marketHashName: row.marketHashName || "", account: row.accountName || "" } }; }
function itemLabel(row?: ItemOption | null): string { return row?.displayName || row?.marketHashName || "全部挂刀物品"; }
function selectItem(value: string): void { marketHashName.value = value; itemSearch.value = ""; itemMenuOpen.value = false; }
function handleDocumentPointer(event: PointerEvent): void { if (!itemFilterRoot.value?.contains(event.target as Node)) itemMenuOpen.value = false; }
function localDateTimeValue(date=new Date()):string{const offset=date.getTimezoneOffset()*60_000;return new Date(date.getTime()-offset).toISOString().slice(0,19);}
function requestId():string{return globalThis.crypto?.randomUUID?.()||`${Date.now()}-${Math.random().toString(16).slice(2)}`;}
function isSelected(row:Operation):boolean{return selectedOperationIds.value.includes(Number(row.id));}
function toggleOperation(row:Operation):void{if(!batchModeEnabled.value||!row.batchActionEligible)return;const id=Number(row.id);selectedOperationIds.value=isSelected(row)?selectedOperationIds.value.filter(value=>value!==id):[...selectedOperationIds.value,id];}
function togglePageSelection():void{const ids=selectablePageOperations.value.map(row=>Number(row.id));selectedOperationIds.value=allSelectableSelected.value?selectedOperationIds.value.filter(id=>!ids.includes(id)):[...new Set([...selectedOperationIds.value,...ids])];}
function clearBatchSelection():void{selectedOperationIds.value=[];batchDialog.value=null;batchConfirmed.value=false;batchError.value="";}
function openBatch(kind:"refreeze"|"manual"):void{if(!selectedOperations.value.length||!batchSameItem.value)return;const defaultPrice=Math.max(...selectedOperations.value.map(row=>Number(row.frozenRebuyPrice||row.c5RebuyPrice||0)));batchPrice.value=defaultPrice>0?defaultPrice.toFixed(2):"";batchReason.value=kind==="manual"?"C5 当前价格不合适，已在其他平台真实完成补仓":"C5 当前价格不合适，人工重新冻结补仓价格";manualCompletedAt.value=localDateTimeValue();batchConfirmed.value=false;batchError.value="";batchDialog.value=kind;}
async function submitBatch():Promise<void>{if(!batchDialog.value||!batchConfirmed.value||batchSubmitting.value)return;batchSubmitting.value=true;batchError.value="";try{const isManual=batchDialog.value==="manual";const endpoint=isManual?"/api/guadao/operations/batch-manual-complete":"/api/guadao/operations/batch-refreeze-rebuy";const body=isManual?{operationIds:selectedOperationIds.value,actualRebuyPrice:Number(batchPrice.value),source:manualSource.value,completedAt:new Date(manualCompletedAt.value).toISOString(),memo:manualMemo.value,externalOrderRef:manualExternalRef.value,confirmed:true,requestId:requestId(),reason:batchReason.value}:{operationIds:selectedOperationIds.value,rebuyPrice:Number(batchPrice.value),executeNow:batchExecuteNow.value,confirmed:true,requestId:requestId(),reason:batchReason.value};const response=await fetch(endpoint,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});if(!response.ok)throw new Error(await responseError(response));batchResponse.value=unwrapPayload<BatchResponse>(await response.json());batchDialog.value=null;selectedOperationIds.value=[];await refresh();}catch(reason){batchError.value=reason instanceof Error?reason.message:String(reason);}finally{batchSubmitting.value=false;}}
function closeBatchResult():void{batchResponse.value=null;}
const runtimeText = computed(() => { const value=String(runtime.value?.runtimeStatus||runtime.value?.status||""); if(value==="closing_only")return"存量闭环中";if(value==="preparing")return"启动准备中";return runtime.value?.enabled?"运行中":"已关闭"; });

async function refresh(): Promise<void> {
  loading.value = true;
  try {
    const params = new URLSearchParams({ page: String(page.value), pageSize: String(pageSize.value) });
    if (keyword.value.trim()) params.set("q", keyword.value.trim());
    if (account.value) params.set("account", account.value);
    if (marketHashName.value) params.set("marketHashName", marketHashName.value);
    if (status.value) params.set("status", status.value);
    if (startAt.value) params.set("startAt", new Date(startAt.value).toISOString());
    if (endAt.value) params.set("endAt", new Date(endAt.value).toISOString());
    const response = await fetch(`/api/guadao/operations?${params}`, { cache: "no-store" });
    if (!response.ok) throw new Error(await responseError(response));
    const data = unwrapPayload<ResponsePayload>(await response.json());
    operations.value = data.operations || [];
    total.value = Number(data.total ?? operations.value.length);
    summary.value = data.summary || {};
    itemOptions.value = data.itemOptions || [];
    runtime.value = data.runtime || {};
    accountOptions.value = (data.accounts || []).map(row => row.name || row.id || "").filter(Boolean);
    selectedOperationIds.value = selectedOperationIds.value.filter(id => operations.value.some(row => Number(row.id)===id && row.status==="sold" && row.batchActionEligible));
    if (selectedId.value == null || !operations.value.some(row => row.id === selectedId.value)) selectedId.value = operations.value[0]?.id ?? null;
    error.value = "";
  } catch (reason) { error.value = reason instanceof Error ? reason.message : String(reason); }
  finally { loading.value = false; }
}
function query(): void { page.value = 1; void refresh(); }
function startPolling(): void { if (timer === null) timer = setInterval(() => void refresh(), 15_000); }
function stopPolling(): void { if (timer !== null) clearInterval(timer); timer = null; }
watch(()=>route.query.q,value=>{const next=String(value||"");if(next===keyword.value)return;keyword.value=next;query()});
watch(status,value=>{if(value!=="sold")clearBatchSelection();});
onMounted(() => { keyword.value=String(route.query.q||"");document.addEventListener("pointerdown",handleDocumentPointer);void refresh(); startPolling(); }); onActivated(startPolling); onDeactivated(stopPolling); onUnmounted(() => { stopPolling(); document.removeEventListener("pointerdown",handleDocumentPointer); });
</script>

<template>
  <main class="page operations-page">
    <header class="page-title-row"><div><p class="eyebrow">Guadao Operations</p><h1>挂刀流水状态</h1><p>每笔流水独立推进，下一步由 nextAttemptAt 决定</p></div><RouterLink class="runtime-link" to="/guadao/overview"><span></span>挂刀执行器 · {{ runtimeText }} · 前往总览控制</RouterLink></header>
    <p v-if="error" class="api-error">流水 API 请求失败：{{ error }}</p>
    <section class="operation-metrics"><article v-for="metric in metricCards" :key="String(metric[0])"><span>{{ metric[0] }}</span><strong>{{ metric[1] }}</strong></article></section>
    <section class="panel filters">
      <label class="filter-keyword"><span>关键词</span><input v-model="keyword" placeholder="物品 / 账号 / listingId / assetId" @keyup.enter="query" /></label>
      <label class="filter-account"><span>Steam 账号</span><select v-model="account"><option value="">全部</option><option v-for="name in accounts" :key="name" :value="name">{{ name }}</option></select></label>
      <div ref="itemFilterRoot" class="item-filter">
        <span>挂刀物品</span>
        <div class="item-select-wrap">
          <button class="item-select" type="button" :aria-expanded="itemMenuOpen" aria-haspopup="listbox" @click="itemMenuOpen=!itemMenuOpen">
            <span>{{ itemLabel(selectedItem) }}</span><FolioIcon :name="itemMenuOpen ? 'chevron-up' : 'chevron-down'" :size="14" />
          </button>
          <div v-if="itemMenuOpen" class="item-menu" role="listbox" @keydown.esc="itemMenuOpen=false">
            <input v-model="itemSearch" type="search" placeholder="输入中文名或 marketHashName 搜索" autofocus />
            <div class="item-options">
              <button v-if="!itemSearch.trim()" type="button" :class="{ selected: !marketHashName }" @click="selectItem('')"><span><strong>全部挂刀物品</strong></span><b>{{ itemOptionTotal }}</b></button>
              <button v-for="option in filteredItemOptions" :key="option.marketHashName" type="button" :class="{ selected: marketHashName===option.marketHashName }" @click="selectItem(option.marketHashName)">
                <span><strong>{{ itemLabel(option) }}</strong><small v-if="option.displayName && option.displayName!==option.marketHashName">{{ option.marketHashName }}</small></span><b>{{ option.count }}</b>
              </button>
              <p v-if="itemSearch.trim() && !filteredItemOptions.length">没有匹配的挂刀物品</p>
            </div>
          </div>
        </div>
      </div>
      <label class="filter-status"><span>流水状态</span><select v-model="status"><option value="">全部阶段</option><option value="listing_pending">挂单待确认</option><option value="listed">Steam 在售</option><option value="sold">已卖出待补仓</option><option value="c5_submission_unconfirmed">C5 补仓待查证据</option><option value="delivery_pending">C5 已购买待收货</option><option value="completed">已闭环</option><option value="manual_required">人工处理</option></select></label>
      <label class="filter-date"><span>创建时间（北京时间）</span><div class="date-range"><input v-model="startAt" type="datetime-local" /><i>至</i><input v-model="endAt" type="datetime-local" /></div></label>
      <button class="primary-button filter-submit" type="button" :disabled="loading" @click="query">{{ loading ? "查询中…" : "查询流水" }}</button>
    </section>

    <section v-if="batchModeEnabled" class="panel batch-toolbar">
      <label class="batch-select-all"><input type="checkbox" :checked="allSelectableSelected" :disabled="!selectablePageOperations.length" @change="togglePageSelection" /><span>本页全选</span></label>
      <div class="batch-summary"><strong>已选择 {{ selectedOperations.length }} 笔</strong><span>{{ selectedOperations.length ? (batchSameItem ? `${selectedOperations[0]?.displayName || selectedOperations[0]?.marketHashName} · 同一品类` : "已选择多个品类，不能共用一个补仓价") : "只可选择已卖出待补仓且无 C5 未决订单的流水" }}</span><small v-if="selectedOperations.length">Steam 税后到手合计 {{ money(selectedSteamNetTotal) }}</small></div>
      <button class="batch-button primary" type="button" :disabled="!selectedOperations.length||!batchSameItem" @click="openBatch('refreeze')">批量重设补仓价并执行</button>
      <button class="batch-button warning" type="button" :disabled="!selectedOperations.length||!batchSameItem" @click="openBatch('manual')">批量手动完结</button>
      <button class="batch-cancel" type="button" :disabled="!selectedOperations.length" @click="clearBatchSelection">取消选择</button>
    </section>

    <section class="operation-workbench">
      <div class="panel operation-list">
        <article v-for="row in operations" :key="row.id" :class="['operation-row', { selected: selected?.id === row.id, 'batch-selected': isSelected(row) }]" @click="selectedId = row.id">
          <div class="operation-summary"><div class="operation-title"><input v-if="batchModeEnabled" type="checkbox" :checked="isSelected(row)" :disabled="!row.batchActionEligible" :title="row.batchActionBlockReason || '选择此流水'" @click.stop="toggleOperation(row)" /><div><strong>{{ row.displayName || row.marketHashName || "未命名饰品" }}</strong><span>{{ row.accountName || "账号未记录" }}</span><small v-if="batchModeEnabled&&!row.batchActionEligible">{{ row.batchActionBlockReason }}</small></div></div><span :class="['status-pill', stageTone(row)]">{{ row.stage || row.status || "状态未知" }}</span></div>
          <div class="operation-values"><div><span>当前挂刀比例</span><strong>{{ pct(row.currentRebuyRatio ?? row.listingRatioAtOpen) }}</strong></div><div><span>冻结补仓价 / 比例</span><strong>{{ money(row.frozenRebuyPrice) }} · {{ pct(row.maxRebuyRatioAtOpen) }}</strong></div><div><span>规则</span><strong>{{ row.manualRebuyRefrozenAt ? "人工重新冻结" : (row.ratioRuleSource || "全局") }}</strong></div><div><span>下一任务</span><strong>{{ row.nextTaskLabel || formatCountdown(row.nextAttemptAt) }}</strong></div><time>{{ formatLocal(row.updatedAt) }}</time></div>
          <div v-if="showRebuyFailureHistory(row)" class="rebuy-failure-banner"><strong>曾补仓失败 {{ row.failedRebuyCount }} 次</strong><span>原失败流水已保留；当前展示第 {{ row.rebuyAttemptCount }} 次补仓尝试</span></div>
          <div class="stepper" :aria-label="`${row.displayName || row.marketHashName} 执行进度`"><div v-for="(label,index) in steps" :key="label" :class="{ done: (row.stepIndex || 0) > index, active: (row.stepIndex || 0) === index }"><i>{{ (row.stepIndex || 0) > index ? "✓" : index + 1 }}</i><span>{{ label }}</span></div></div>
        </article>
        <div v-if="!operations.length" class="empty-state">{{ loading ? "正在读取流水…" : "当前筛选没有后端流水记录。" }}</div>
        <footer v-if="total" class="pagination"><span>共 {{ total }} 条 · 第 {{ page }} / {{ pageCount }} 页</span><select v-model.number="pageSize" @change="page=1;refresh()"><option :value="10">10 条/页</option><option :value="20">20 条/页</option><option :value="50">50 条/页</option></select><button :disabled="page<=1" @click="page--;refresh()">上一页</button><button :disabled="page>=pageCount" @click="page++;refresh()">下一页</button></footer>
      </div>

      <aside class="panel operation-detail">
        <template v-if="selected"><div class="detail-head"><div><strong>{{ selected.displayName || selected.marketHashName }}</strong><span class="mono">运行 ID：{{ selected.operationId || selected.id }}</span><small v-if="isSelected(selected)" class="batch-detail-tag">已加入批量操作</small></div><span :class="['status-pill', stageTone(selected)]">{{ selected.stage || selected.status }}</span></div>
          <dl class="detail-grid"><div><dt>assetId</dt><dd>{{ selected.assetId || "—" }}</dd></div><div><dt>Steam 账号</dt><dd>{{ selected.accountName || "—" }}</dd></div><div><dt>SteamID</dt><dd>{{ selected.steamId || "—" }}</dd></div><div><dt>listingId</dt><dd>{{ selected.listingId || "—" }}</dd></div><div><dt>C5 资产订单号</dt><dd>{{ selected.c5OrderId || "未生成" }}</dd></div><div><dt>C5 交易订单号</dt><dd>{{ selected.c5TradeOrderId || "未生成" }}</dd></div><div><dt>C5 请求流水号</dt><dd>{{ selected.c5OutTradeNo || "—" }}</dd></div><div><dt>规则来源</dt><dd>{{ selected.manualRebuyRefrozenAt ? "人工重新冻结" : (selected.ratioRuleSource || "全局") }}{{ selected.ratioRuleId ? ` · ${selected.ratioRuleId}` : "" }}{{ selected.ratioRuleVersion ? ` · v${selected.ratioRuleVersion}` : "" }}</dd></div><div><dt>初始挂刀比例</dt><dd>{{ pct(selected.listingRatioAtOpen) }}</dd></div><div><dt>当前冻结比例</dt><dd>{{ pct(selected.currentRebuyRatio ?? selected.maxRebuyRatioAtOpen) }}</dd></div><div><dt>当前冻结补仓价</dt><dd>{{ money(selected.frozenRebuyPrice) }}</dd></div><div><dt>Steam 挂价</dt><dd>{{ money(selected.steamListPrice) }}</dd></div><div><dt>Steam 税后到手</dt><dd>{{ money(selected.steamNetAmount) }}</dd></div><div><dt>C5 补仓价</dt><dd>{{ money(selected.c5RebuyPrice) }}</dd></div><div><dt>Steam 官方卖出时间</dt><dd>{{ formatLocal(selected.steamSoldAt) }}</dd></div><div><dt>C5 下单时间</dt><dd>{{ formatLocal(selected.c5OrderSubmittedAt) }}</dd></div><div><dt>C5 12小时复查时间</dt><dd>{{ formatLocal(selected.c5DeliveryDeadlineAt) }}</dd></div></dl>
          <section v-if="showRebuyFailureHistory(selected)" class="rebuy-attempt-history"><header><div><h2>补仓尝试历史</h2><p>失败流水不会删除；主卡片当前展示最新的替换补仓。</p></div><span>失败 {{ selected.failedRebuyCount }} 次 · 共 {{ selected.rebuyAttemptCount }} 次</span></header><div class="attempt-list"><article v-for="attempt in selected.rebuyAttempts" :key="attempt.id" :class="{ current: attempt.isCurrent }"><div class="attempt-head"><strong>{{ attempt.operationId || `GD-${attempt.id}` }}</strong><span :class="['status-pill',attemptTone(attempt)]">{{ attempt.isCurrent ? `当前 · ${attempt.stage}` : attempt.stage }}</span></div><dl><div><dt>C5 资产订单号</dt><dd>{{ attempt.c5OrderId || "未生成" }}</dd></div><div><dt>补仓价格</dt><dd>{{ attemptPrice(attempt) }}</dd></div><div><dt>下单时间</dt><dd>{{ formatLocal(attempt.c5OrderSubmittedAt) }}</dd></div><div><dt>{{ attempt.failureReason ? "失败时间" : "替换上限" }}</dt><dd>{{ attempt.failureReason ? formatLocal(attempt.failureAt) : money(attempt.replacementMaxPrice) }}</dd></div></dl><p v-if="attempt.failureReason" class="attempt-error"><strong>{{ attempt.failureReason }}</strong><span v-if="attempt.failureCode">错误码：{{ attempt.failureCode }}</span><span v-if="attempt.replacementOperationId">已创建替换补仓 GD-{{ attempt.replacementOperationId }}</span></p><p v-else-if="attempt.replacementForOperationId" class="attempt-link">替换自失败补仓 GD-{{ attempt.replacementForOperationId }}；最高不超过 {{ money(attempt.replacementMaxPrice) }}</p></article></div></section>
          <section class="timeline"><h2>状态时间线</h2><div v-if="selected.timeline?.length"><article v-for="(event,index) in selected.timeline" :key="`${event.at}-${index}`" :class="event.status"><i></i><div><strong>{{ event.label || "状态更新" }}</strong><span>{{ event.detail }}</span></div><time>{{ formatLocal(event.at) }}</time></article></div><p v-else>后端暂未返回该流水的状态时间线。</p></section>
          <RouterLink class="related-log-link" :to="relatedLogsTo(selected)"><FolioIcon name="clock" :size="13" />查看关联实时日志</RouterLink><div class="next-task"><span>下一个任务</span><strong>{{ selected.nextTaskLabel || "尚未安排" }} · {{ formatCountdown(selected.nextAttemptAt) }}</strong><p v-if="selected.nextTaskReason">原因：{{ selected.nextTaskReason }}</p></div>
        </template><div v-else class="empty-state">选择一笔流水查看冻结口径、远端标识和时间线。</div>
      </aside>
    </section>

    <div v-if="batchDialog" class="modal-backdrop" @click.self="batchDialog=null">
      <section class="batch-modal panel" role="dialog" aria-modal="true">
        <header><div><p class="eyebrow">{{ batchDialog==='refreeze' ? 'Refreeze & Retry' : 'Manual External Completion' }}</p><h2>{{ batchDialog==='refreeze' ? '批量重设补仓价并执行' : '批量手动完结' }}</h2><span>仅处理当前选中的 {{ selectedOperations.length }} 笔同品类流水</span></div><button type="button" @click="batchDialog=null">×</button></header>
        <div class="batch-modal-summary"><strong>{{ selectedOperations[0]?.displayName || selectedOperations[0]?.marketHashName }}</strong><span>Steam 税后到手合计 {{ money(selectedSteamNetTotal) }}</span></div>
        <label><span>{{ batchDialog==='refreeze' ? '新的冻结补仓单价' : '其他平台实际补仓单价' }}</span><input v-model="batchPrice" type="number" min="0.01" step="0.01" placeholder="0.00" /></label>
        <div class="ratio-comparison"><div><span>原冻结比例</span><strong>{{ pct(selectedOperations[0]?.currentRebuyRatio ?? selectedOperations[0]?.maxRebuyRatioAtOpen) }}</strong></div><i>→</i><div><span>{{ batchDialog==='refreeze' ? '新冻结比例' : '实际闭环比例' }}</span><strong>{{ ratioPreviewRange }}</strong></div></div>
        <template v-if="batchDialog==='refreeze'">
          <label class="toggle-line"><input v-model="batchExecuteNow" type="checkbox" /><span>保存后立即将这些补仓任务重新排到期</span></label>
          <div class="batch-warning"><FolioIcon name="warning" :size="16" /><p>新价格和新比例将替换这些流水当前的冻结值；旧冻结价格、旧比例和原全局上限会保留在审计历史，但不再拦截本次补仓。</p></div>
        </template>
        <template v-else>
          <label><span>补仓来源</span><input v-model="manualSource" placeholder="其他平台" /></label>
          <label><span>实际完成时间（北京时间）</span><input v-model="manualCompletedAt" type="datetime-local" step="1" /></label>
          <label><span>外部订单号</span><input v-model="manualExternalRef" placeholder="与备注至少填写一项" /></label>
          <label><span>平台及操作备注</span><textarea v-model="manualMemo" rows="3" placeholder="购买平台、成交方式或其他可核对信息"></textarea></label>
          <div class="batch-safe-note"><FolioIcon name="shield" :size="16" /><p>只更新本地补仓子流水，不向 C5 提交订单；Steam 卖出资产继续保持 sold，不伪造新 assetId。</p></div>
        </template>
        <label><span>操作原因</span><input v-model="batchReason" /></label>
        <label class="confirm-line"><input v-model="batchConfirmed" type="checkbox" /><span>{{ batchDialog==='refreeze' ? `我确认按新价格和新比例重新冻结 ${selectedOperations.length} 笔流水` : `我确认这些物品已在其他平台真实完成补仓` }}</span></label>
        <p v-if="batchError" class="api-error">{{ batchError }}</p>
        <footer><button type="button" @click="batchDialog=null">取消</button><button class="primary-button" type="button" :disabled="batchSubmitting||!batchConfirmed||!(Number(batchPrice)>0)||(batchDialog==='manual'&&(!manualSource.trim()||(!manualMemo.trim()&&!manualExternalRef.trim())))" @click="submitBatch">{{ batchSubmitting ? '提交中…' : (batchDialog==='refreeze' ? `确认重设 ${selectedOperations.length} 笔` : `确认手动完结 ${selectedOperations.length} 笔`) }}</button></footer>
      </section>
    </div>

    <div v-if="batchResponse" class="modal-backdrop" @click.self="closeBatchResult">
      <section class="batch-modal result-modal panel" role="dialog" aria-modal="true"><header><div><p class="eyebrow">Batch Result</p><h2>批量操作结果</h2><span>批次 {{ batchResponse.batchId }} · 成功 {{ batchResponse.successCount || 0 }} · 待处理 {{ batchResponse.failedCount || 0 }}</span></div><button type="button" @click="closeBatchResult">×</button></header><div class="batch-results"><article v-for="result in batchResponse.results" :key="`${result.operationId}-${result.code}`" :class="result.ok?'success':'failed'"><FolioIcon :name="result.ok?'success':'warning'" :size="16" /><div><strong>{{ result.tradeNo || `GD-${result.operationId}` }} · {{ result.message }}</strong><span v-if="result.newFrozenRebuyPrice">新冻结价 {{ money(result.newFrozenRebuyPrice) }} · 新比例 {{ pct(result.newFrozenRebuyRatio) }}</span><span v-else-if="result.actualRebuyPrice">实际补仓价 {{ money(result.actualRebuyPrice) }} · 实际比例 {{ pct(result.actualRebuyRatio) }}</span><small v-if="result.idempotentReplay">重复提交已按原结果返回，未再次修改</small></div></article></div><div class="batch-safe-note"><FolioIcon name="clock" :size="16" /><p>旧值、新值和操作原因已写入追加审计；实时日志可按 operationId 查询。</p></div><footer><button class="primary-button" type="button" @click="closeBatchResult">返回流水列表</button></footer></section>
    </div>
  </main>
</template>

<style scoped>
.operations-page{width:min(1360px,calc(100vw - 44px));gap:14px}.page-title-row{display:flex;justify-content:space-between;align-items:center;padding:6px 8px}.page-title-row h1{margin:0;font-size:32px;letter-spacing:-.045em}.page-title-row p:last-child{margin:6px 0 0;color:var(--folio-muted);font-size:12px}.runtime-link{display:flex;align-items:center;gap:7px;border:1px solid var(--folio-line);border-radius:11px;padding:10px 13px;color:var(--folio-green);background:#fff;font-size:11px;font-weight:700}.runtime-link span{width:7px;height:7px;border-radius:50%;background:currentColor}.api-error{margin:0;border-radius:10px;padding:9px 12px;color:var(--folio-red);background:var(--folio-red-soft);font-size:12px}.operation-metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px}.operation-metrics article{min-height:82px;display:grid;align-content:space-between;border:1px solid var(--folio-line);border-radius:14px;padding:13px 15px;background:#fff;box-shadow:var(--folio-shadow)}.operation-metrics span{color:var(--folio-muted);font-size:10px}.operation-metrics strong{font-size:22px}.filters{display:grid;grid-template-columns:1.6fr .8fr .9fr 120px;gap:12px;align-items:end;padding:14px}.filters label{display:grid;gap:5px}.filters label>span{color:var(--folio-muted);font-size:10px;font-weight:700}.filters input,.filters select{width:100%;min-height:40px;border:1px solid #dfe4df;border-radius:10px;padding:8px 11px;color:var(--folio-ink);background:#fff}.operation-workbench{display:grid;grid-template-columns:minmax(0,1.8fr) minmax(360px,.8fr);gap:12px;align-items:start}.operation-list{padding:0;overflow:hidden}.operation-row{padding:16px 18px;border-bottom:1px solid var(--folio-line);cursor:pointer;transition:background .16s}.operation-row:hover{background:#fafcfa}.operation-row.selected{background:#f2f8f4;box-shadow:inset 3px 0 0 var(--folio-green)}.operation-summary,.operation-values,.detail-head{display:flex;align-items:center}.operation-summary{justify-content:space-between}.operation-summary>div{display:grid;gap:3px}.operation-summary strong{font-size:13px}.operation-summary span,.operation-values span{color:var(--folio-muted);font-size:9px}.status-pill{display:inline-flex;border-radius:999px;padding:5px 9px;font-size:9px;font-weight:800}.status-pill.success{color:var(--folio-green);background:var(--folio-green-soft)}.status-pill.warning{color:var(--folio-amber);background:var(--folio-amber-soft)}.status-pill.danger{color:var(--folio-red);background:var(--folio-red-soft)}.operation-values{display:grid;grid-template-columns:repeat(4,minmax(90px,1fr)) 120px;gap:9px;margin-top:12px}.operation-values div{display:grid;gap:3px}.operation-values strong{font-size:10px}.operation-values time{align-self:center;color:var(--folio-muted);font-size:9px;text-align:right}.stepper{display:grid;grid-template-columns:repeat(7,1fr);margin-top:14px}.stepper div{position:relative;display:grid;place-items:center;gap:4px;color:#9aa29d}.stepper div::before{content:"";position:absolute;top:8px;right:50%;left:-50%;height:2px;background:#e1e5e1}.stepper div:first-child::before{display:none}.stepper i{position:relative;z-index:1;display:grid;place-items:center;width:18px;height:18px;border:1px solid #d6ddd8;border-radius:50%;background:#fff;font-size:8px;font-style:normal}.stepper span{font-size:7px}.stepper .done,.stepper .active{color:var(--folio-green)}.stepper .done::before,.stepper .active::before{background:var(--folio-green)}.stepper .done i,.stepper .active i{border-color:var(--folio-green)}.stepper .done i{color:#fff;background:var(--folio-green)}.pagination{display:flex;justify-content:flex-end;align-items:center;gap:8px;padding:12px 16px;color:var(--folio-muted);font-size:10px}.pagination span{margin-right:auto}.pagination select,.pagination button{min-height:30px;border:1px solid var(--folio-line);border-radius:8px;padding:5px 9px;background:#fff}.operation-detail{position:sticky;top:122px;min-height:560px}.detail-head{justify-content:space-between;gap:12px;border-bottom:1px solid var(--folio-line);padding-bottom:12px}.detail-head>div{display:grid;gap:4px}.detail-head strong{font-size:15px}.detail-head span{color:var(--folio-muted);font-size:9px}.detail-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 14px;margin:13px 0}.detail-grid div{border-bottom:1px solid #edf0ed;padding:8px 0}.detail-grid dt{color:var(--folio-muted);font-size:9px}.detail-grid dd{margin:3px 0 0;overflow-wrap:anywhere;font-size:10px;font-weight:650}.timeline{margin-top:15px}.timeline h2{font-size:14px}.timeline>div{display:grid}.timeline article{position:relative;display:grid;grid-template-columns:12px minmax(0,1fr) auto;gap:7px;padding:7px 0}.timeline article:not(:last-child)::before{content:"";position:absolute;top:18px;bottom:-4px;left:5px;width:1px;background:#d9dfda}.timeline i{z-index:1;width:11px;height:11px;margin-top:2px;border:2px solid var(--folio-green);border-radius:50%;background:#fff}.timeline article>div{display:grid;gap:2px}.timeline strong{font-size:10px}.timeline span,.timeline time,.timeline>p{color:var(--folio-muted);font-size:9px}.next-task{margin-top:14px;border-radius:11px;padding:12px;color:#765319;background:var(--folio-amber-soft)}.next-task span{font-size:9px}.next-task strong{display:block;margin-top:3px;font-size:11px}.next-task p{margin:4px 0 0;font-size:9px}.empty-state{margin:14px;border:1px dashed #d8ded9;border-radius:11px;padding:26px;color:var(--folio-muted);text-align:center;background:var(--folio-surface-soft);font-size:11px}
  .runtime-link{text-decoration:none}.filters{grid-template-columns:1.25fr .68fr 1fr .78fr 1.25fr 112px;grid-template-areas:"keyword account item status status submit" "date date date date date submit"}.filter-keyword{grid-area:keyword}.filter-account{grid-area:account}.item-filter{grid-area:item;position:relative;display:grid;gap:5px;min-width:0}.item-filter>span{color:var(--folio-muted);font-size:10px;font-weight:700}.filter-status{grid-area:status}.filter-date{grid-area:date;max-width:650px}.filter-submit{grid-area:submit;align-self:end}.date-range{display:grid;grid-template-columns:1fr auto 1fr;gap:5px;align-items:center}.date-range i{color:var(--folio-muted);font-size:9px;font-style:normal}.item-select-wrap{position:relative}.item-select{display:flex;align-items:center;justify-content:space-between;gap:8px;width:100%;min-height:40px;border:1px solid #dfe4df;border-radius:10px;padding:8px 10px;color:var(--folio-ink);background:#fff;text-align:left}.item-select>span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.item-select:hover,.item-select[aria-expanded="true"]{border-color:#7cab8d;box-shadow:0 0 0 2px rgba(35,106,76,.08)}.item-menu{position:absolute;z-index:30;top:calc(100% + 6px);left:0;width:max(360px,100%);border:1px solid var(--folio-line);border-radius:12px;padding:8px;background:#fff;box-shadow:0 18px 42px rgba(27,54,41,.16)}.item-menu>input{min-height:36px}.item-options{max-height:280px;margin-top:7px;overflow-y:auto}.item-options button{display:flex;align-items:center;justify-content:space-between;gap:12px;width:100%;border:0;border-radius:8px;padding:8px 9px;color:var(--folio-ink);background:transparent;text-align:left}.item-options button:hover{background:#f4f8f5}.item-options button.selected{color:var(--folio-green);background:var(--folio-green-soft)}.item-options button>span{display:grid;gap:2px;min-width:0}.item-options strong{overflow:hidden;text-overflow:ellipsis;font-size:10px;white-space:nowrap}.item-options small{overflow:hidden;text-overflow:ellipsis;color:var(--folio-muted);font-size:8px;white-space:nowrap}.item-options b{min-width:24px;color:var(--folio-muted);font-size:9px;text-align:right}.item-options button.selected b{color:var(--folio-green)}.item-options p{margin:0;padding:18px 8px;color:var(--folio-muted);font-size:10px;text-align:center}.related-log-link{display:inline-flex;align-items:center;gap:6px;margin-top:10px;color:var(--folio-green);font-size:10px;font-weight:700;text-decoration:none}
.rebuy-failure-banner{display:flex;align-items:center;gap:8px;margin-top:11px;border:1px solid #ead0a2;border-radius:9px;padding:7px 9px;color:#77551d;background:#fff8e9}.rebuy-failure-banner strong{font-size:10px}.rebuy-failure-banner span{color:#8a724d;font-size:9px}.rebuy-attempt-history{margin-top:15px;border:1px solid #ead8b8;border-radius:12px;padding:12px;background:#fffbf3}.rebuy-attempt-history>header{display:flex;align-items:start;justify-content:space-between;gap:12px}.rebuy-attempt-history h2{margin:0;font-size:14px}.rebuy-attempt-history header p{margin:4px 0 0;color:var(--folio-muted);font-size:9px}.rebuy-attempt-history>header>span{border-radius:999px;padding:4px 7px;color:#78551b;background:#f6e8ca;font-size:9px;font-weight:750;white-space:nowrap}.attempt-list{display:grid;gap:8px;margin-top:10px}.attempt-list>article{border:1px solid #e5e8e4;border-radius:10px;padding:10px;background:#fff}.attempt-list>article.current{border-color:#a9cfb7;box-shadow:inset 3px 0 0 var(--folio-green)}.attempt-head{display:flex;align-items:center;justify-content:space-between;gap:8px}.attempt-head>strong{font-size:11px}.attempt-list dl{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 12px;margin:7px 0 0}.attempt-list dl div{border-bottom:1px solid #eef1ee;padding:6px 0}.attempt-list dt{color:var(--folio-muted);font-size:8px}.attempt-list dd{margin:2px 0 0;overflow-wrap:anywhere;font-size:9px;font-weight:650}.attempt-error,.attempt-link{display:grid;gap:2px;margin:8px 0 0;border-radius:8px;padding:7px 8px;font-size:9px}.attempt-error{color:#8b3d34;background:#fff0ed}.attempt-error span{color:#9a625c}.attempt-link{color:#426b56;background:#edf6f0}
.batch-toolbar{display:grid;grid-template-columns:auto minmax(240px,1fr) auto auto auto;align-items:center;gap:12px;padding:13px 15px;border-color:#bfd7c8;background:linear-gradient(135deg,#f7fbf8 0%,#fff 72%);box-shadow:0 9px 24px rgba(28,76,54,.07)}
.batch-select-all{display:flex;align-items:center;gap:8px;min-height:38px;padding-right:13px;border-right:1px solid var(--folio-line);color:var(--folio-ink);font-size:10px;font-weight:750;white-space:nowrap}.batch-select-all input,.operation-title>input,.toggle-line input,.confirm-line input{width:15px;height:15px;margin:0;accent-color:var(--folio-green);cursor:pointer}.batch-select-all input:disabled,.operation-title>input:disabled{cursor:not-allowed;opacity:.45}
.batch-summary{display:grid;gap:2px;min-width:0}.batch-summary strong{font-size:12px}.batch-summary span,.batch-summary small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.batch-summary span{color:var(--folio-muted);font-size:9px}.batch-summary small{color:var(--folio-green);font-size:9px;font-weight:700}
.batch-button,.batch-cancel{min-height:36px;border-radius:9px;padding:8px 12px;font-size:10px;font-weight:750;white-space:nowrap;transition:border-color .15s,background .15s,transform .15s}.batch-button:not(:disabled):active,.batch-cancel:not(:disabled):active{transform:translateY(1px)}.batch-button.primary{border:1px solid var(--folio-green);color:#fff;background:var(--folio-green)}.batch-button.primary:hover:not(:disabled){background:#184d39}.batch-button.warning{border:1px solid #d6a75c;color:#704a10;background:#fff8e9}.batch-button.warning:hover:not(:disabled){border-color:#bd8733;background:#fbedcf}.batch-cancel{border:1px solid var(--folio-line);color:var(--folio-muted);background:#fff}.batch-cancel:hover:not(:disabled){border-color:#bec8c0;color:var(--folio-ink)}.batch-button:disabled,.batch-cancel:disabled{cursor:not-allowed;opacity:.45}
.operation-row.batch-selected{background:#edf6f0;box-shadow:inset 3px 0 0 var(--folio-green)}.operation-row.selected.batch-selected{background:#e8f2ec;box-shadow:inset 4px 0 0 var(--folio-green)}.operation-title{display:flex!important;grid-template-columns:none!important;align-items:flex-start;gap:9px}.operation-title>input{flex:0 0 auto;margin-top:2px}.operation-title>div{display:grid;gap:3px;min-width:0}.operation-title small{max-width:520px;color:#8a6240;font-size:8px;line-height:1.35}.batch-detail-tag{width:max-content;border-radius:999px;padding:3px 7px;color:var(--folio-green)!important;background:var(--folio-green-soft);font-size:8px!important;font-weight:750}
.modal-backdrop{position:fixed;z-index:100;inset:0;display:grid;place-items:center;padding:24px;background:rgba(17,32,25,.48);backdrop-filter:blur(3px)}
.batch-modal{width:min(600px,calc(100vw - 40px));max-height:calc(100vh - 48px);overflow-y:auto;border-color:#dbe4dd;border-radius:17px;padding:0;background:#fff;box-shadow:0 28px 76px rgba(13,37,26,.28)}.batch-modal>header{position:sticky;z-index:2;top:0;display:flex;align-items:flex-start;justify-content:space-between;gap:18px;border-bottom:1px solid var(--folio-line);padding:18px 20px 14px;background:rgba(255,255,255,.96);backdrop-filter:blur(8px)}.batch-modal>header h2{margin:2px 0 4px;font-size:21px;letter-spacing:-.025em}.batch-modal>header span{color:var(--folio-muted);font-size:10px}.batch-modal>header>button{display:grid;place-items:center;flex:0 0 auto;width:30px;height:30px;border:1px solid var(--folio-line);border-radius:9px;color:var(--folio-muted);background:#fff;font-size:19px;line-height:1}.batch-modal>header>button:hover{color:var(--folio-ink);background:var(--folio-surface-soft)}
.batch-modal>label{display:grid;gap:6px;margin:13px 20px 0}.batch-modal>label>span{color:var(--folio-muted);font-size:10px;font-weight:700}.batch-modal input[type="text"],.batch-modal input[type="number"],.batch-modal input[type="datetime-local"],.batch-modal>label>input:not([type="checkbox"]),.batch-modal textarea{width:100%;border:1px solid #dce3de;border-radius:10px;padding:9px 11px;color:var(--folio-ink);background:#fff;font:inherit;font-size:11px;outline:none}.batch-modal input:not([type="checkbox"]){min-height:40px}.batch-modal textarea{resize:vertical;line-height:1.5}.batch-modal input:not([type="checkbox"]):focus,.batch-modal textarea:focus{border-color:#7cab8d;box-shadow:0 0 0 3px rgba(35,106,76,.09)}
.batch-modal-summary{display:flex;align-items:center;justify-content:space-between;gap:18px;margin:16px 20px 0;border:1px solid #dce8e0;border-radius:11px;padding:11px 13px;background:#f5faf7}.batch-modal-summary strong{min-width:0;overflow:hidden;text-overflow:ellipsis;font-size:11px;white-space:nowrap}.batch-modal-summary span{flex:0 0 auto;color:var(--folio-green);font-size:9px;font-weight:750}
.ratio-comparison{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:12px;margin:13px 20px 0;border:1px solid var(--folio-line);border-radius:12px;padding:12px;background:var(--folio-surface-soft)}.ratio-comparison div{display:grid;gap:3px}.ratio-comparison div:last-child{text-align:right}.ratio-comparison span{color:var(--folio-muted);font-size:9px}.ratio-comparison strong{font-size:18px}.ratio-comparison div:last-child strong{color:var(--folio-green)}.ratio-comparison i{color:#9aa69e;font-size:16px;font-style:normal}
.batch-modal>.toggle-line,.batch-modal>.confirm-line{display:flex;align-items:flex-start;gap:9px;border-radius:10px;padding:10px 11px}.toggle-line{border:1px solid #dce8e0;background:#f6faf7}.confirm-line{border:1px solid #c8ddd0;background:var(--folio-green-soft)}.toggle-line input,.confirm-line input{flex:0 0 auto;margin-top:1px}.batch-modal>.toggle-line>span,.batch-modal>.confirm-line>span{color:var(--folio-ink);font-size:10px;line-height:1.45}
.batch-warning,.batch-safe-note{display:flex;align-items:flex-start;gap:9px;margin:13px 20px 0;border-radius:11px;padding:11px 12px}.batch-warning{border:1px solid #ead4aa;color:#75541e;background:#fff8e9}.batch-safe-note{border:1px solid #cfe1d6;color:#315e48;background:#f1f8f4}.batch-warning svg,.batch-safe-note svg{flex:0 0 auto;margin-top:1px}.batch-warning p,.batch-safe-note p{margin:0;font-size:9px;line-height:1.55}.batch-modal>.api-error{margin:13px 20px 0}
.batch-modal>footer{display:flex;justify-content:flex-end;gap:9px;margin-top:17px;border-top:1px solid var(--folio-line);padding:14px 20px 18px}.batch-modal>footer button{min-height:36px;border:1px solid var(--folio-line);border-radius:9px;padding:8px 14px;color:var(--folio-ink);background:#fff;font-size:10px;font-weight:750}.batch-modal>footer .primary-button{border-color:var(--folio-green);color:#fff;background:var(--folio-green)}.batch-modal>footer button:disabled{cursor:not-allowed;opacity:.5}
.result-modal{width:min(660px,calc(100vw - 40px))}.batch-results{display:grid;gap:8px;margin:16px 20px 0}.batch-results article{display:flex;align-items:flex-start;gap:10px;border:1px solid var(--folio-line);border-radius:11px;padding:11px 12px}.batch-results article.success{border-color:#cfe1d6;background:#f4faf6}.batch-results article.failed{border-color:#ebcfc9;background:#fff5f3}.batch-results article>svg{flex:0 0 auto;margin-top:1px}.batch-results article>div{display:grid;gap:3px;min-width:0}.batch-results strong{font-size:10px;line-height:1.45}.batch-results span,.batch-results small{color:var(--folio-muted);font-size:9px;line-height:1.4}.batch-results article.success span{color:#3f6b54}.batch-results article.failed strong{color:var(--folio-red)}
@media (max-width:1100px){.batch-toolbar{grid-template-columns:auto minmax(180px,1fr) auto auto}.batch-cancel{grid-column:4}.batch-button{white-space:normal}.operation-workbench{grid-template-columns:1fr}.operation-detail{position:static}.operation-values{grid-template-columns:repeat(4,minmax(80px,1fr))}.operation-values time{grid-column:1/-1;text-align:left}}
</style>
