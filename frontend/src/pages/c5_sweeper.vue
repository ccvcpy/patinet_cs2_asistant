<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import FolioIcon from "../components/FolioIcon.vue";
import OperationVisualAtom from "../components/OperationVisualAtom.vue";

type RoundStatus = "draft" | "paused" | "running" | "stopped" | "completed";

interface CatalogItem {
  marketHashName: string;
  displayName: string;
  c5ItemId?: string | null;
  custom?: boolean;
}

interface ReceivingAccount {
  id: string;
  name: string;
  steamId: string;
  steamIdMasked: string;
  c5Nickname?: string | null;
  c5Bound: boolean;
  hasTradeUrl: boolean;
  tradeUrlMatches: boolean;
  available: boolean;
}

interface SweeperRound {
  id: string;
  roundNumber: number;
  marketHashName: string;
  displayName: string;
  maxPrice: number;
  budget: number;
  targetCount: number;
  intervalSeconds: number;
  delivery: number;
  status: RoundStatus;
  stopReason?: string | null;
  createdAt: string;
  startedAt?: string | null;
  completedAt?: string | null;
  nextRunAt?: string | null;
  lastRunAt?: string | null;
  lastPrice?: number | null;
  lastMessage: string;
  attemptCount: number;
  receivingAccountId?: string | null;
  receivingAccountName?: string | null;
  receivingSteamId?: string | null;
}

interface RoundSummary {
  id: string;
  roundNumber: number;
  marketHashName: string;
  displayName: string;
  status: RoundStatus;
  stopReason?: string | null;
  budget: number;
  committedAmount: number;
  settledAmount: number;
  targetCount: number;
  deliveredCount: number;
  pendingCount: number;
  failedCount: number;
  averageAcceptedPrice: number;
  createdAt: string;
  startedAt?: string | null;
  completedAt?: string | null;
  receivingAccountId?: string | null;
  receivingAccountName?: string | null;
  receivingSteamId?: string | null;
}

interface SweeperOrder {
  id: string;
  acceptedAt: string;
  status: "pending" | "delivered" | "failed";
  orderAssetId: string;
  tradeOrderId?: string | null;
  actualPay: number;
  failedCode?: string | null;
  failedDesc?: string | null;
  receivingAccountId?: string | null;
  receivingAccountName?: string | null;
  receivingSteamId?: string | null;
}

interface SweeperEvent {
  at: string;
  status: string;
  message: string;
}

interface Dashboard {
  apiOnline: boolean;
  realExecutionRunning: boolean;
  round: SweeperRound | null;
  counts: { accepted: number; delivered: number; pending: number; failed: number };
  money: {
    budget: number;
    acceptedAmount: number;
    committedAmount: number;
    settledAmount: number;
    failedAmount: number;
    remainingBudget: number;
    averageAcceptedPrice: number;
    averageDeliveredPrice: number;
    maxAffordableCount: number;
    targetEstimatedCost: number;
  };
  orders: SweeperOrder[];
  events: SweeperEvent[];
  rounds: RoundSummary[];
  recentItems: CatalogItem[];
}

const dashboard = ref<Dashboard | null>(null);
const apiError = ref("");
const actionError = ref("");
const actionMessage = ref("");
const busy = ref(false);
const clock = ref(Date.now());
const creatingNew = ref(false);
const formDirty = ref(false);
const selectedRoundId = ref<string | null>(null);
const receivingAccounts = ref<ReceivingAccount[]>([]);
const selectedReceivingAccountId = ref("");
const accountsBusy = ref(false);

const itemQuery = ref("");
const selectedItem = ref<CatalogItem | null>(null);
const itemSuggestions = ref<CatalogItem[]>([]);
const itemSearchOpen = ref(false);
const itemSearchBusy = ref(false);
const itemSearchHasMore = ref(false);
const itemSearchNextOffset = ref(0);
const maxPrice = ref(1.1);
const budget = ref(300);
const targetCount = ref(200);
const intervalSeconds = ref(60);

const confirmationOpen = ref(false);
const confirmation = ref("");
let pollTimer: number | undefined;
let clockTimer: number | undefined;
let searchTimer: number | undefined;

async function fetchJson(path: string, options?: RequestInit) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options?.headers || {}) },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `请求失败 (${response.status})`);
  return payload;
}

function syncForm(round: SweeperRound | null) {
  if (!round) return;
  selectedItem.value = { marketHashName: round.marketHashName, displayName: round.displayName };
  itemQuery.value = `${round.displayName} · ${round.marketHashName}`;
  maxPrice.value = round.maxPrice;
  budget.value = round.budget;
  targetCount.value = round.targetCount;
  intervalSeconds.value = round.intervalSeconds;
  selectedReceivingAccountId.value = round.receivingAccountId || "";
  formDirty.value = false;
}

function applyDashboard(value: Dashboard, forceForm = false) {
  if (!Array.isArray(value.rounds) || !("round" in value)) {
    throw new Error("8765 后端仍是旧版，请在后端终端按 Ctrl+C 后重新执行原启动命令");
  }
  dashboard.value = value;
  if (creatingNew.value) return;
  if (value.round) selectedRoundId.value = value.round.id;
  if (forceForm || !formDirty.value) syncForm(value.round);
}

async function loadDashboard(silent = false, roundId?: string | null) {
  if (creatingNew.value && silent) return;
  const id = roundId === undefined ? selectedRoundId.value : roundId;
  const suffix = id ? `?roundId=${encodeURIComponent(id)}` : "";
  try {
    const payload = (await fetchJson(`/api/c5-sweeper/dashboard${suffix}`)) as Dashboard;
    applyDashboard(payload, !silent);
    apiError.value = "";
  } catch (error) {
    apiError.value = error instanceof Error ? error.message : String(error);
    if (!silent) actionError.value = apiError.value;
  }
}

async function loadReceivingAccounts(refresh = false) {
  accountsBusy.value = true;
  try {
    const payload = await fetchJson(`/api/c5-sweeper/accounts${refresh ? "?refresh=1" : ""}`);
    receivingAccounts.value = payload.accounts || [];
    if (!selectedReceivingAccountId.value) {
      selectedReceivingAccountId.value = receivingAccounts.value.find((row) => row.available)?.id || "";
    }
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : String(error);
  } finally {
    accountsBusy.value = false;
  }
}

async function postAction(path: string, body: Record<string, unknown>, success: string) {
  busy.value = true;
  actionError.value = "";
  actionMessage.value = "";
  try {
    const payload = await fetchJson(path, { method: "POST", body: JSON.stringify(body) });
    if (payload.dashboard) {
      creatingNew.value = false;
      applyDashboard(payload.dashboard as Dashboard, true);
    }
    actionMessage.value = success;
    return payload;
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : String(error);
    return null;
  } finally {
    busy.value = false;
  }
}

async function searchItems(query = itemQuery.value, append = false) {
  itemSearchBusy.value = true;
  try {
    const normalizedQuery = query.trim();
    const offset = append ? itemSearchNextOffset.value : 0;
    const payload = await fetchJson(`/api/c5-sweeper/items?query=${encodeURIComponent(normalizedQuery)}&limit=20&offset=${offset}`);
    if (normalizedQuery !== itemQuery.value.trim()) return;
    const incoming = payload.items || [];
    if (append) {
      const merged = new Map(itemSuggestions.value.map((item) => [item.marketHashName, item]));
      for (const item of incoming) merged.set(item.marketHashName, item);
      itemSuggestions.value = [...merged.values()];
    } else {
      itemSuggestions.value = incoming;
    }
    itemSearchHasMore.value = Boolean(payload.pagination?.hasMore);
    itemSearchNextOffset.value = Number(payload.pagination?.nextOffset ?? 0);
    itemSearchOpen.value = true;
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : String(error);
  } finally {
    itemSearchBusy.value = false;
  }
}

function onItemInput() {
  selectedItem.value = null;
  formDirty.value = true;
  itemSearchOpen.value = true;
  itemSearchHasMore.value = false;
  itemSearchNextOffset.value = 0;
  if (searchTimer) window.clearTimeout(searchTimer);
  searchTimer = window.setTimeout(() => searchItems(), 250);
}

function chooseItem(item: CatalogItem) {
  selectedItem.value = item;
  itemQuery.value = `${item.displayName} · ${item.marketHashName}`;
  itemSearchOpen.value = false;
  formDirty.value = true;
}

const displayRound = computed(() => creatingNew.value ? null : dashboard.value?.round || null);
const displayCounts = computed(() => creatingNew.value
  ? { accepted: 0, delivered: 0, pending: 0, failed: 0 }
  : dashboard.value?.counts || { accepted: 0, delivered: 0, pending: 0, failed: 0 });
const displayMoney = computed(() => creatingNew.value
  ? { budget: Number(budget.value), committedAmount: 0, remainingBudget: Number(budget.value), averageAcceptedPrice: 0 }
  : dashboard.value?.money || { budget: 0, committedAmount: 0, remainingBudget: 0, averageAcceptedPrice: 0 });
const selectedReceivingAccount = computed(() => receivingAccounts.value.find((row) => row.id === selectedReceivingAccountId.value) || null);
const currentStatus = computed<RoundStatus | "empty">(() => displayRound.value?.status || "empty");
const canEdit = computed(() => !apiError.value && (creatingNew.value || ["draft", "paused"].includes(currentStatus.value)));
const hasOpenRound = computed(() => dashboard.value?.rounds.some((row) => !["completed", "stopped"].includes(row.status)) ?? false);
const canCreateNext = computed(() => !apiError.value && !hasOpenRound.value && !dashboard.value?.realExecutionRunning);
const quantityProgress = computed(() => {
  if (!displayRound.value) return 0;
  return Math.min(100, displayCounts.value.delivered / Math.max(1, displayRound.value.targetCount) * 100);
});
const budgetProgress = computed(() => {
  const total = Number(displayMoney.value.budget) || Number(budget.value) || 0;
  const used = Number(displayMoney.value.committedAmount) || 0;
  return total > 0 ? Math.min(100, used / total * 100) : 0;
});
const formAffordableCount = computed(() => {
  const price = Number(maxPrice.value);
  return price > 0 ? Math.floor(Number(budget.value) / price) : 0;
});
const formTargetCost = computed(() => Number(maxPrice.value) * Number(targetCount.value));
const countdown = computed(() => {
  const next = displayRound.value?.nextRunAt;
  if (!next || currentStatus.value !== "running") return "—";
  return `${Math.max(0, Math.ceil((new Date(next).getTime() - clock.value) / 1000))} 秒`;
});
const priceSafe = computed(() => {
  const live = displayRound.value?.lastPrice;
  return live != null && live <= Number(maxPrice.value);
});

function markDirty() {
  formDirty.value = true;
}

function resetNewRound() {
  if (!canCreateNext.value) {
    actionError.value = "当前轮次尚未结束，请先继续或停止当前轮次。";
    return;
  }
  creatingNew.value = true;
  selectedRoundId.value = null;
  const defaultItem = dashboard.value?.recentItems?.[0] || { marketHashName: "Kilowatt Case", displayName: "千瓦武器箱" };
  selectedItem.value = defaultItem;
  itemQuery.value = `${defaultItem.displayName} · ${defaultItem.marketHashName}`;
  maxPrice.value = 1.1;
  budget.value = 300;
  targetCount.value = 200;
  intervalSeconds.value = 60;
  formDirty.value = false;
  actionError.value = "";
  selectedReceivingAccountId.value = receivingAccounts.value.find((row) => row.available)?.id || "";
  itemSearchOpen.value = false;
  actionMessage.value = "";
}

async function saveRound(startAfterSave = false) {
  if (!selectedItem.value) {
    actionError.value = "请从搜索结果中选择饰品；自定义 market_hash_name 也需要点击对应结果。";
    return;
  }
  if (!selectedReceivingAccountId.value) {
    actionError.value = "请选择接收武器箱的 Steam 账号。";
    return;
  }
  const roundId = creatingNew.value ? null : dashboard.value?.round?.id || null;
  const result = await postAction("/api/c5-sweeper/round", {
    roundId,
    marketHashName: selectedItem.value.marketHashName,
    displayName: selectedItem.value.displayName,
    receivingAccountId: selectedReceivingAccountId.value,
    maxPrice: Number(maxPrice.value),
    budget: Number(budget.value),
    targetCount: Number(targetCount.value),
    intervalSeconds: Number(intervalSeconds.value),
    delivery: 2,
  }, startAfterSave ? "轮次参数已保存，请输入确认词开启真实购买" : "轮次草稿已保存，不会自动购买");
  if (result && startAfterSave) confirmationOpen.value = true;
}

async function confirmStart() {
  const id = dashboard.value?.round?.id;
  if (!id) return;
  const result = await postAction("/api/c5-sweeper/start", {
    roundId: id,
    confirmation: confirmation.value,
  }, "本轮已启动：立即提交一次批量购买，之后每 60 秒提交下一批");
  if (result) {
    confirmationOpen.value = false;
    confirmation.value = "";
  }
}

async function selectRound(round: RoundSummary) {
  creatingNew.value = false;
  selectedRoundId.value = round.id;
  formDirty.value = false;
  await loadDashboard(false, round.id);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function pauseRound() {
  const id = dashboard.value?.round?.id;
  if (id) await postAction("/api/c5-sweeper/pause", { roundId: id }, "本轮已暂停，待交付订单仍会继续审计");
}

async function stopRound() {
  const id = dashboard.value?.round?.id;
  if (id) await postAction("/api/c5-sweeper/stop", { roundId: id }, "本轮已停止并归档，现在可以新建下一轮");
}

async function refreshRound() {
  const id = dashboard.value?.round?.id;
  if (id) await postAction("/api/c5-sweeper/refresh", { roundId: id }, "价格与交付状态已刷新，没有发起购买");
}

function statusText(status?: RoundStatus | "empty") {
  return ({
    empty: "等待新建轮次",
    draft: "草稿",
    paused: "已暂停",
    running: "运行中",
    stopped: "已停止",
    completed: "已完成",
  } as Record<string, string>)[status || "empty"];
}

function stopReasonText(reason?: string | null) {
  return ({
    target_reached: "达到数量目标",
    budget_reached: "预算已用完",
    budget_limit: "剩余预算不足下一件",
    manual: "手动停止",
    buy_uncertain: "购买结果不确定，已暂停",
  } as Record<string, string>)[reason || ""] || "—";
}

function orderStatus(value: SweeperOrder["status"]) {
  return { pending: "等待交付", delivered: "交付成功", failed: "交付失败" }[value];
}

function money(value?: number | null) {
  return value == null ? "—" : `¥${Number(value).toFixed(2)}`;
}

function maskSteamId(value?: string | null) {
  if (!value) return "—";
  return value.length > 10 ? `${value.slice(0, 7)}***${value.slice(-4)}` : value;
}

function accountInitials(value?: string | null) {
  const text = (value || "ST").trim();
  return text.slice(0, 2).toUpperCase();
}

function dateTime(value?: string | null) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(new Date(value));
}

onMounted(async () => {
  await loadDashboard();
  await loadReceivingAccounts(true);
  if (!dashboard.value?.round) resetNewRound();
  pollTimer = window.setInterval(() => loadDashboard(true), 2000);
  clockTimer = window.setInterval(() => { clock.value = Date.now(); }, 1000);
});

onBeforeUnmount(() => {
  if (pollTimer) window.clearInterval(pollTimer);
  if (clockTimer) window.clearInterval(clockTimer);
  if (searchTimer) window.clearTimeout(searchTimer);
});
</script>

<template>
  <main class="sweeper-page sweeper-page--folio-refresh sweeper-page--minimal-v2">
    <section class="hero">
      <div class="hero-title">
        <OperationVisualAtom name="case-scanner" :size="70" />
        <div>
          <p>C5 QUICK BUY · 独立后台循环</p>
          <h1>C5 武器箱扫货中心</h1>
          <span>每 60 秒提交一次批量购买；按最高单价、剩余预算和剩余目标数量尽可能成交。</span>
        </div>
      </div>
      <div class="hero-status">
          <div><i class="status-icon api" :class="{ offline: apiError }"><FolioIcon name="link" :size="15" /></i><span>后端 API<strong>{{ apiError ? "未连接" : "已连接" }}</strong></span></div>
        <b></b>
          <div><i class="status-icon execution" :class="{ active: dashboard?.realExecutionRunning }"><FolioIcon :name="dashboard?.realExecutionRunning ? 'play' : 'pause'" :size="15" /></i><span>真实执行<strong>{{ dashboard?.realExecutionRunning ? "运行中" : "已暂停" }}</strong></span></div>
      </div>
    </section>

    <div v-if="apiError" class="alert error">API 未连接：{{ apiError }}。请启动 8765 后端。</div>
    <div v-if="actionError" class="alert error">{{ actionError }}</div>
    <div v-if="actionMessage" class="alert notice">{{ actionMessage }}</div>

    <section class="metrics">
      <article><div class="metric-label"><OperationVisualAtom name="case-single" :size="38" /><span>当前武器箱</span></div><strong class="item-name">{{ selectedItem?.displayName || displayRound?.displayName || "尚未选择" }}</strong><small>{{ selectedItem?.marketHashName || displayRound?.marketHashName || "选择后才会保存" }}</small><small class="account-caption">接收：{{ selectedReceivingAccount?.name || displayRound?.receivingAccountName || "未选择账号" }}</small></article>
      <article><div class="metric-label"><OperationVisualAtom name="price-tag" :size="38" /><span>C5 当前价</span></div><strong>{{ money(displayRound?.lastPrice) }}</strong><small :class="priceSafe ? 'green-text' : ''">{{ displayRound?.lastPrice == null ? "等待读取" : priceSafe ? "符合最高价" : "高于最高价" }}</small></article>
      <article><div class="metric-label"><OperationVisualAtom name="budget-wallet" :size="38" /><span>本轮预算</span></div><strong>{{ money(displayRound?.budget ?? Number(budget)) }}</strong><small>每轮独立设置</small></article>
      <article><div class="metric-label"><OperationVisualAtom name="clock" :size="38" /><span>预算已用</span></div><strong>{{ money(displayMoney.committedAmount) }} <em>/ {{ budgetProgress.toFixed(1) }}%</em></strong><div class="mini-track"><i :style="{ width: `${budgetProgress}%` }"></i></div></article>
      <article><div class="metric-label"><OperationVisualAtom name="delivery-success" :size="38" /><span>交付进度</span></div><strong>{{ displayCounts.delivered }} / {{ displayRound?.targetCount ?? targetCount }}</strong><div class="mini-track blue"><i :style="{ width: `${quantityProgress}%` }"></i></div></article>
      <article><div class="metric-label"><OperationVisualAtom name="refresh" :size="38" /><span>下一批</span></div><strong>{{ countdown }}</strong><small>到点提交下一次批量购买</small></article>
    </section>

    <section class="workspace">
      <article class="panel form-panel">
        <header><div><span>任务参数</span><h2>{{ creatingNew ? "新建扫货轮次" : `编辑第 ${dashboard?.round?.roundNumber ?? "—"} 轮` }}</h2></div><span class="round-chip">独立预算</span></header>

        <div class="field item-field">
          <label>扫描武器箱</label>
          <div class="search-wrap">
            <input
              v-model="itemQuery"
              :disabled="!canEdit"
              placeholder="输入武器箱中文名或以 Case 结尾的名称"
              @input="onItemInput"
              @focus="searchItems(itemQuery)"
            />
            <span>{{ itemSearchBusy ? "···" : "⌕" }}</span>
            <div v-if="itemSearchOpen && canEdit" class="suggestions">
              <button v-for="item in itemSuggestions" :key="item.marketHashName" type="button" @click="chooseItem(item)">
                <strong>{{ item.displayName }}</strong><small>{{ item.marketHashName }}</small><em v-if="item.custom">自定义</em>
              </button>
              <button v-if="itemSearchHasMore" class="catalog-load-more" type="button" :disabled="itemSearchBusy" @click="searchItems(itemQuery, true)">{{ itemSearchBusy ? "加载中…" : "加载更多结果" }}</button>
              <p v-if="!itemSuggestions.length">没有匹配的武器箱；胶囊、纪念包、钥匙和皮肤不会进入结果。</p>
            </div>
          </div>
        </div>

        <div v-if="dashboard?.recentItems.length" class="recent-items">
          <span>最近选择</span>
          <button v-for="item in dashboard.recentItems.slice(0, 4)" :key="item.marketHashName" type="button" :disabled="!canEdit" @click="chooseItem(item)">{{ item.displayName }}</button>
        </div>

        <div class="account-picker">
          <div class="account-heading">
            <div><span>交付目标</span><h3>接收 Steam 账号</h3></div>
            <button type="button" :disabled="accountsBusy" @click="loadReceivingAccounts(true)">{{ accountsBusy ? "校验中…" : "重新校验 C5 绑定" }}</button>
          </div>
          <div class="account-cards">
            <button
              v-for="account in receivingAccounts"
              :key="account.id"
              type="button"
              :disabled="!canEdit || !account.available"
              :class="{ selected: selectedReceivingAccountId === account.id, unavailable: !account.available }"
              @click="selectedReceivingAccountId = account.id; markDirty()"
            >
              <i>{{ accountInitials(account.name) }}</i>
              <span><strong>{{ account.name }}</strong><small>{{ account.steamIdMasked || maskSteamId(account.steamId) }}</small></span>
              <em>{{ account.available ? `C5 已绑定${account.c5Nickname ? ` · ${account.c5Nickname}` : ''}` : "不可用于接收" }}</em>
              <b>{{ selectedReceivingAccountId === account.id ? "已选择" : "选择" }}</b>
            </button>
            <p v-if="!receivingAccounts.length">{{ accountsBusy ? "正在读取五个 Steam 账号…" : "没有读取到可用接收账号" }}</p>
          </div>
        </div>

        <div class="form-body">
          <div class="form-fields">
            <label>最高单价（CNY）<input v-model.number="maxPrice" type="number" min="0.01" step="0.01" :disabled="!canEdit" @input="markDirty" /></label>
            <label class="budget-field">本轮总预算（CNY）<input v-model.number="budget" type="number" min="0.01" step="0.01" :disabled="!canEdit" @input="markDirty" /></label>
            <label>目标交付数量<input v-model.number="targetCount" type="number" min="1" step="1" :disabled="!canEdit" @input="markDirty" /></label>
            <label>批量提交间隔<input v-model.number="intervalSeconds" type="number" disabled /><small>固定 60 秒；每次只提交一个批量购买请求</small></label>
          </div>
          <div class="calculation">
            <div><span>按最高价可买</span><strong>{{ formAffordableCount }} 件</strong></div>
            <div><span>数量目标预计</span><strong>{{ money(formTargetCost) }}</strong></div>
            <p><b>实际停止条件</b>达到数量目标，或剩余预算不足购买当前价格的下一件。</p>
          </div>
        </div>

        <p v-if="apiError" class="archive-note">后端版本尚未更新，重启 8765 后端后即可编辑和创建轮次。</p>
        <div v-else-if="canEdit" class="form-actions">
          <button class="primary" type="button" :disabled="busy" @click="saveRound(true)">{{ currentStatus === "paused" ? "保存并继续本轮" : "保存并开始本轮" }}</button>
          <button class="secondary" type="button" :disabled="busy" @click="saveRound(false)">仅保存草稿</button>
        </div>
        <p v-else class="archive-note">该轮次已经归档，参数不可修改。可以查看明细，或点击下方“新建下一轮”。</p>
      </article>

      <article class="panel runtime-panel">
        <header><div><span>运行状态</span><h2>{{ displayRound ? `第 ${displayRound.roundNumber} 轮 · ${statusText(currentStatus)}` : "等待新轮次" }}</h2></div><span class="attempt-chip">第 {{ displayRound?.attemptCount ?? 0 }} 次批量提交</span></header>
        <div class="runtime-content">
          <div class="progress-ring" :style="{ '--progress': `${quantityProgress * 3.6}deg` }"><div><strong>{{ quantityProgress.toFixed(0) }}%</strong><span>交付进度</span></div></div>
          <dl>
            <div><dt>交付成功</dt><dd>{{ displayCounts.delivered }}</dd></div>
            <div><dt>待交付</dt><dd>{{ displayCounts.pending }}</dd></div>
            <div><dt>失败</dt><dd>{{ displayCounts.failed }}</dd></div>
            <div><dt>剩余预算</dt><dd class="blue-text">{{ money(displayMoney.remainingBudget) }}</dd></div>
          </dl>
        </div>
        <div class="wide-track"><i :style="{ width: `${quantityProgress}%` }"></i><span>{{ displayCounts.delivered }} / {{ displayRound?.targetCount ?? targetCount }}</span></div>
        <p class="runtime-message">{{ displayRound?.lastMessage || "保存轮次后才会读取行情；打开页面不会自动购买。" }}</p>
        <div class="runtime-meta"><span>接收账号<strong>{{ displayRound?.receivingAccountName || selectedReceivingAccount?.name || "未选择" }}</strong><small>{{ maskSteamId(displayRound?.receivingSteamId || selectedReceivingAccount?.steamId) }}</small></span><span>上次轮询<strong>{{ dateTime(displayRound?.lastRunAt) }}</strong></span><span>下一轮时间<strong>{{ dateTime(displayRound?.nextRunAt) }}</strong></span><span>结束原因<strong>{{ stopReasonText(displayRound?.stopReason) }}</strong></span></div>
        <div class="runtime-actions">
          <button v-if="currentStatus === 'running'" class="dark" type="button" :disabled="busy" @click="pauseRound">Ⅱ 暂停本轮</button>
          <button v-if="dashboard?.round" class="outline-red" type="button" :disabled="busy || ['completed','stopped'].includes(currentStatus)" @click="stopRound">□ 停止本轮</button>
          <button v-if="dashboard?.round" class="secondary" type="button" :disabled="busy" @click="refreshRound">只刷新，不购买</button>
        </div>
      </article>
    </section>

    <section class="panel rounds-panel">
      <header><div><span>历史账本</span><h2>扫货轮次</h2></div><button class="new-round" type="button" :disabled="!canCreateNext" @click="resetNewRound">＋ 新建下一轮</button></header>
      <div class="table-scroll">
        <table>
          <thead><tr><th>轮次</th><th>武器箱</th><th>接收账号</th><th>状态</th><th>预算</th><th>已占用</th><th>交付</th><th>均价</th><th>开始时间</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="row in dashboard?.rounds" :key="row.id" :class="{ selected: row.id === dashboard?.round?.id && !creatingNew }">
              <td>第 {{ row.roundNumber }} 轮</td>
              <td><strong>{{ row.displayName }}</strong><small>{{ row.marketHashName }}</small></td>
              <td><strong>{{ row.receivingAccountName || "—" }}</strong><small>{{ maskSteamId(row.receivingSteamId) }}</small></td>
              <td><span class="status-pill" :class="row.status">{{ statusText(row.status) }}</span></td>
              <td>{{ money(row.budget) }}</td><td>{{ money(row.committedAmount) }}</td><td>{{ row.deliveredCount }} / {{ row.targetCount }}</td><td>{{ money(row.averageAcceptedPrice) }}</td><td>{{ dateTime(row.startedAt || row.createdAt) }}</td>
              <td><button type="button" class="open-round" @click="selectRound(row)">{{ row.status === 'running' ? "打开本轮" : "查看详情" }}</button></td>
            </tr>
            <tr v-if="!dashboard?.rounds.length"><td colspan="10" class="empty">还没有扫货轮次。先在上方选择武器箱、接收账号并设置独立预算。</td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <section v-if="dashboard?.round" class="details-grid">
      <article class="panel detail-panel">
        <header><div><span>交付审计</span><h2>第 {{ dashboard.round.roundNumber }} 轮订单</h2></div><small>按 orderAssetId 查询最终状态</small></header>
        <div class="detail-scroll">
          <table><thead><tr><th>接受时间</th><th>接收账号</th><th>订单号</th><th>金额</th><th>状态</th><th>失败原因</th></tr></thead><tbody>
            <tr v-for="order in dashboard.orders" :key="order.id"><td>{{ dateTime(order.acceptedAt) }}</td><td><strong>{{ order.receivingAccountName || "—" }}</strong><small>{{ maskSteamId(order.receivingSteamId) }}</small></td><td class="mono">{{ order.orderAssetId }}</td><td>{{ money(order.actualPay) }}</td><td><span class="order-pill" :class="order.status">{{ orderStatus(order.status) }}</span></td><td>{{ order.failedDesc || order.failedCode || "—" }}</td></tr>
            <tr v-if="!dashboard.orders.length"><td colspan="6" class="empty">本轮还没有购买订单。</td></tr>
          </tbody></table>
        </div>
      </article>
      <article class="panel events-panel">
        <header><div><span>事件</span><h2>运行记录</h2></div><small>重复等待不会刷屏</small></header>
        <div class="timeline"><div v-for="event in dashboard.events" :key="`${event.at}-${event.status}`"><i></i><time>{{ dateTime(event.at) }}</time><p>{{ event.message }}</p></div><p v-if="!dashboard.events.length" class="empty">暂无事件</p></div>
      </article>
    </section>

    <div v-if="confirmationOpen" class="modal-backdrop" @click.self="confirmationOpen = false">
      <section class="confirm-modal">
        <div class="confirm-heading">
          <OperationVisualAtom name="warning" :size="52" />
          <div><span>真实购买确认</span><h2>开启第 {{ dashboard?.round?.roundNumber }} 轮扫货</h2></div>
        </div>
        <p>将立即提交一次批量购买；之后每 {{ dashboard?.round?.intervalSeconds }} 秒提交下一批。每批按最高单价、剩余预算和剩余目标数量选择在售。最高单价 {{ money(dashboard?.round?.maxPrice) }}，本轮总预算 {{ money(dashboard?.round?.budget) }}。</p>
        <div class="confirm-account"><span>{{ accountInitials(dashboard?.round?.receivingAccountName) }}</span><p><b>接收账号：{{ dashboard?.round?.receivingAccountName || "未选择" }}</b><small>SteamID：{{ maskSteamId(dashboard?.round?.receivingSteamId) }}</small></p></div>
        <label>请输入“开始扫货”<input v-model="confirmation" autocomplete="off" placeholder="开始扫货" @keyup.enter="confirmStart" /></label>
        <div><button class="secondary" type="button" @click="confirmationOpen = false">取消</button><button class="primary" type="button" :disabled="busy" @click="confirmStart">确认并开始</button></div>
      </section>
    </div>
  </main>
</template>

<style scoped>
.sweeper-page{width:min(1380px,calc(100vw - 32px));margin:0 auto;padding:18px 0 48px;display:grid;gap:14px;color:#101a31}.hero{min-height:164px;padding:30px 34px;border-radius:24px;display:flex;align-items:center;justify-content:space-between;gap:28px;color:#fff;background:radial-gradient(circle at 78% 12%,rgba(54,119,220,.32),transparent 34%),linear-gradient(120deg,#0d1833,#142c59 62%,#153f68);box-shadow:0 18px 44px rgba(13,30,63,.18);overflow:hidden}.hero-title{display:flex;align-items:flex-start;gap:18px}.hero-logo{width:48px;height:48px;display:grid;place-items:center;border:3px solid #ffbd4a;border-radius:12px;color:#ffbd4a;font-size:34px;font-weight:900;transform:rotate(45deg)}.hero-title p{margin:0 0 6px;color:#ffc65f;font-size:12px;font-weight:900;letter-spacing:1.4px}.hero-title h1{margin:0 0 8px;font-size:34px;line-height:1.15;letter-spacing:-1px}.hero-title span{color:#d1dbec}.hero-status{display:flex;align-items:center;gap:22px;padding:15px 22px;border:1px solid rgba(255,255,255,.28);border-radius:999px;background:rgba(6,17,43,.35);backdrop-filter:blur(8px)}.hero-status>div{display:flex;align-items:center;gap:10px}.hero-status i{width:10px;height:10px;border-radius:50%;background:#569cf7;box-shadow:0 0 0 5px rgba(86,156,247,.15)}.hero-status i.offline{background:#f17865}.hero-status i.execution{background:#8290aa}.hero-status i.execution.active{background:#ffb632;box-shadow:0 0 0 5px rgba(255,182,50,.15)}.hero-status span{display:grid;color:#acb9ce;font-size:12px}.hero-status strong{color:#fff;font-size:14px}.hero-status b{width:1px;height:35px;background:rgba(255,255,255,.25)}.alert{padding:10px 14px;border-radius:10px;font-size:13px}.alert.error{border:1px solid #ecb0a8;color:#8b3126;background:#fff2ef}.alert.notice{border:1px solid #aec8ee;color:#28558d;background:#eff6ff}.metrics{display:grid;grid-template-columns:1.15fr repeat(5,1fr);gap:10px}.metrics article{min-height:120px;padding:17px;border:1px solid #dce3ed;border-radius:16px;display:grid;align-content:space-between;background:#fff;box-shadow:0 7px 20px rgba(18,39,74,.045)}.metrics span{color:#65728a;font-size:12px;font-weight:700}.metrics strong{font-size:23px;letter-spacing:-.4px}.metrics strong.item-name{font-size:18px}.metrics strong em{color:#7e899b;font-size:12px;font-style:normal;font-weight:500}.metrics small{overflow:hidden;color:#8b95a7;font-size:11px;text-overflow:ellipsis;white-space:nowrap}.metrics small.amber{color:#b46f00}.mini-track{height:6px;border-radius:99px;background:#edf0f5;overflow:hidden}.mini-track i{display:block;height:100%;border-radius:inherit;background:#efa600}.mini-track.blue i{background:#2869d2}.workspace{display:grid;grid-template-columns:minmax(520px,1fr) minmax(560px,1fr);gap:14px}.panel{border:1px solid #dce3ed;border-radius:18px;padding:22px;background:#fff;box-shadow:0 8px 25px rgba(20,41,74,.05)}.panel header{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;margin-bottom:18px}.panel header span{color:#778297;font-size:12px;font-weight:700}.panel header h2{margin:2px 0 0;font-size:21px}.panel header small{color:#8a94a5}.round-chip,.attempt-chip{padding:6px 10px;border-radius:99px!important;color:#245fae!important;background:#edf4ff}.field{display:grid;grid-template-columns:110px 1fr;align-items:center;gap:12px}.field label,.form-fields label{color:#26344d;font-size:13px;font-weight:700}.search-wrap{position:relative}.search-wrap>input,.form-fields input{width:100%;height:40px;border:1px solid #cbd6e6;border-radius:8px;padding:0 12px;outline:none;background:#fbfcfe}.search-wrap>input:focus,.form-fields input:focus{border-color:#3d76c9;box-shadow:0 0 0 3px rgba(61,118,201,.1)}.search-wrap>span{position:absolute;right:13px;top:8px;color:#5883c2;font-size:20px}.suggestions{position:absolute;z-index:20;top:45px;left:0;right:0;max-height:300px;overflow:auto;border:1px solid #cbd6e6;border-radius:12px;padding:6px;background:#fff;box-shadow:0 18px 38px rgba(15,38,76,.16)}.suggestions button{width:100%;padding:9px 10px;border:0;border-radius:8px;display:grid;grid-template-columns:1fr auto;gap:2px 8px;text-align:left;background:#fff}.suggestions button:hover{background:#f0f5fc}.suggestions strong{font-size:13px}.suggestions small{grid-column:1;color:#7c8799}.suggestions em{grid-row:1/3;grid-column:2;align-self:center;color:#a76500;font-size:11px;font-style:normal}.suggestions p{margin:6px;padding:8px;color:#7b8799;font-size:12px}.recent-items{display:flex;align-items:center;flex-wrap:wrap;gap:7px;margin:10px 0 18px;padding-left:122px}.recent-items>span{color:#8993a4;font-size:11px}.recent-items button{border:1px solid #dce4ef;border-radius:99px;padding:5px 9px;color:#31547f;background:#f8fafd;font-size:11px}.form-body{display:grid;grid-template-columns:1fr 1fr;gap:18px}.form-fields{display:grid;gap:10px}.form-fields label{display:grid;grid-template-columns:1fr 148px;align-items:center;gap:10px}.form-fields label small{grid-column:2;color:#8a94a5;font-size:10px;font-weight:400}.form-fields .budget-field input{border-color:#e8a62d;background:#fffcf5;box-shadow:inset 3px 0 #e9a325}.calculation{display:grid;align-content:start;gap:8px}.calculation>div{display:flex;justify-content:space-between;padding:13px;border-radius:9px;background:#f4f6fa}.calculation span{color:#6f7b90;font-size:12px}.calculation strong{font-size:16px}.calculation p{margin:0;padding:13px;border-radius:9px;color:#566174;background:#fff8e9;font-size:11px}.calculation p b{display:block;margin-bottom:3px;color:#26334b}.form-actions{display:grid;grid-template-columns:1.25fr 1fr;gap:10px;margin-top:16px}.primary,.secondary,.dark,.outline-red,.new-round,.open-round{min-height:40px;border-radius:9px;padding:8px 14px;font-weight:800}.primary{border:1px solid #d58b00;color:#172039;background:linear-gradient(135deg,#ffbd35,#eda000)}.secondary{border:1px solid #b9c7db;color:#28568f;background:#fff}.archive-note{margin:16px 0 0;padding:12px;border-radius:9px;color:#5d687a;background:#f2f5f9}.runtime-panel{display:grid;align-content:start}.runtime-content{display:flex;align-items:center;justify-content:center;gap:65px;padding:8px 0 18px}.progress-ring{--progress:0deg;width:190px;height:190px;border-radius:50%;display:grid;place-items:center;position:relative;background:conic-gradient(#2468d2 var(--progress),#e8edf4 0)}.progress-ring:after{content:"";position:absolute;inset:16px;border-radius:50%;background:#fff}.progress-ring div{z-index:1;display:grid;text-align:center}.progress-ring strong{font-size:36px}.progress-ring span{color:#738096;font-size:11px}.runtime-content dl{display:grid;gap:13px;margin:0}.runtime-content dl div{display:grid;grid-template-columns:100px 70px;align-items:center}.runtime-content dt{color:#536179}.runtime-content dd{margin:0;text-align:right;font-size:20px;font-weight:900}.blue-text{color:#155bc2}.wide-track{position:relative;height:8px;margin:3px 0 14px;border-radius:99px;background:#e9edf4}.wide-track i{display:block;height:100%;border-radius:inherit;background:#2868d0}.wide-track span{position:absolute;right:0;top:10px;font-size:11px}.runtime-message{margin:20px 0 10px;padding:12px;border-radius:9px;color:#26578f;background:#edf5ff;font-size:13px}.runtime-meta{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.runtime-meta span{padding:9px;border-radius:8px;color:#8a94a4;background:#fafbfd;font-size:10px}.runtime-meta strong{display:block;margin-top:3px;color:#334057;font-size:11px}.runtime-actions{display:flex;justify-content:flex-end;flex-wrap:wrap;gap:9px;margin-top:15px}.dark{border:1px solid #12264c;color:#fff;background:#12264c}.outline-red{border:1px solid #e66c62;color:#a3342a;background:#fff}.rounds-panel{padding-bottom:10px}.new-round{border:1px solid #2b6bd1;color:#1f5ebf;background:#f8fbff}.new-round:disabled{opacity:.45}.table-scroll,.detail-scroll{max-height:410px;overflow:auto}.table-scroll table,.detail-scroll table{width:100%;border-collapse:collapse;white-space:nowrap}.table-scroll th,.table-scroll td,.detail-scroll th,.detail-scroll td{padding:11px 10px;border-bottom:1px solid #edf0f5;text-align:left;font-size:12px}.table-scroll th,.detail-scroll th{position:sticky;top:0;z-index:2;color:#fff;background:#10264a}.table-scroll td small{display:block;max-width:190px;overflow:hidden;color:#8791a2;text-overflow:ellipsis}.table-scroll tr.selected{background:#f0f6ff}.status-pill,.order-pill{display:inline-block;padding:4px 8px;border-radius:99px}.status-pill.running{color:#155bbb;background:#e8f1ff}.status-pill.draft,.status-pill.paused{color:#946300;background:#fff5dc}.status-pill.completed,.order-pill.delivered{color:#285a91;background:#eaf3ff}.status-pill.stopped,.order-pill.failed{color:#8f3a31;background:#fff0ed}.order-pill.pending{color:#8c6000;background:#fff4d7}.open-round{min-height:31px;padding:4px 9px;border:1px solid #3d75c8;color:#2259aa;background:#fff;font-size:11px}.empty{padding:28px!important;text-align:center!important;color:#8a94a5}.details-grid{display:grid;grid-template-columns:1.4fr .8fr;gap:14px}.detail-panel,.events-panel{min-width:0}.mono{font-family:Consolas,monospace;color:#50617b}.timeline{max-height:330px;overflow:auto}.timeline>div{display:grid;grid-template-columns:10px 120px 1fr;gap:8px;padding:8px 0;border-bottom:1px solid #edf0f5}.timeline i{width:7px;height:7px;margin-top:5px;border-radius:50%;background:#3f7bd1}.timeline time{color:#8791a2;font-size:10px}.timeline p{margin:0;font-size:11px}.modal-backdrop{position:fixed;z-index:100;inset:0;display:grid;place-items:center;padding:20px;background:rgba(7,15,32,.58);backdrop-filter:blur(4px)}.confirm-modal{width:min(500px,100%);padding:26px;border-radius:18px;background:#fff;box-shadow:0 25px 70px rgba(0,0,0,.25)}.confirm-modal>span{color:#bd7400;font-size:11px;font-weight:900;letter-spacing:1px}.confirm-modal h2{margin:5px 0 10px}.confirm-modal p{color:#627087;font-size:13px}.confirm-modal label{display:grid;gap:6px;color:#334159;font-size:12px;font-weight:700}.confirm-modal input{height:42px;border:1px solid #c6d3e5;border-radius:9px;padding:0 12px;outline:none}.confirm-modal>div{display:flex;justify-content:flex-end;gap:9px;margin-top:18px}button:disabled,input:disabled{cursor:not-allowed;opacity:.55}@media(max-width:1100px){.metrics{grid-template-columns:repeat(3,1fr)}.workspace,.details-grid{grid-template-columns:1fr}}@media(max-width:720px){.hero{align-items:flex-start;flex-direction:column}.hero-status{width:100%;justify-content:center}.metrics{grid-template-columns:repeat(2,1fr)}.field{grid-template-columns:1fr}.recent-items{padding-left:0}.form-body{grid-template-columns:1fr}.form-fields label{grid-template-columns:1fr}.form-fields label small{grid-column:1}.runtime-content{gap:25px}.progress-ring{width:150px;height:150px}.runtime-meta{grid-template-columns:1fr}.timeline>div{grid-template-columns:10px 1fr}.timeline time,.timeline p{grid-column:2}}
/* Krill component kit: compact operations-console density. */
.sweeper-page{width:min(1460px,calc(100vw - 48px));gap:9px;padding-top:12px}.hero{min-height:150px;padding:20px 26px 72px;border-radius:10px;box-shadow:0 10px 28px rgba(13,30,63,.14)}.hero-logo{width:40px;height:40px;border-radius:7px;font-size:27px}.hero-title h1{font-size:29px}.hero-status{padding:10px 15px;border-radius:8px}.metrics{position:relative;z-index:2;margin:-68px 12px 0;gap:7px}.metrics article{min-height:82px;padding:10px 12px;border-radius:7px;box-shadow:0 5px 14px rgba(18,39,74,.06)}.metrics strong{font-size:20px}.metrics strong.item-name{font-size:15px}.metrics .account-caption{color:#275fae;font-weight:700}.workspace{gap:9px}.panel{border-radius:8px;padding:14px;box-shadow:0 4px 14px rgba(20,41,74,.045)}.panel header{margin-bottom:10px}.panel header h2{font-size:18px}.round-chip,.attempt-chip{padding:4px 8px;border-radius:5px!important}.search-wrap>input,.form-fields input{height:34px}.suggestions{top:38px;border-radius:7px}.recent-items{margin:7px 0 10px}.form-body{gap:10px}.form-fields{gap:6px}.form-fields label{grid-template-columns:1fr 132px}.calculation{gap:6px}.calculation>div,.calculation p{padding:9px;border-radius:6px}.form-actions{margin-top:10px}.primary,.secondary,.dark,.outline-red,.new-round,.open-round{min-height:34px;border-radius:6px}.account-picker{margin:8px 0 12px;padding:10px;border:1px solid #dce3ed;border-radius:7px;background:#f8fafc}.account-heading{display:flex;align-items:center;justify-content:space-between;margin-bottom:7px}.account-heading span{color:#7c8799;font-size:10px;font-weight:800;letter-spacing:.5px}.account-heading h3{margin:1px 0 0;font-size:14px}.account-heading>button{border:0;color:#2869c8;background:transparent;font-size:11px;font-weight:700}.account-cards{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:7px}.account-cards>button{position:relative;min-height:86px;padding:9px;border:1px solid #d9e1ec;border-radius:7px;display:grid;grid-template-columns:30px minmax(0,1fr);grid-template-rows:auto auto;gap:5px 8px;text-align:left;background:#fff}.account-cards>button:hover:not(:disabled){border-color:#7ea4df}.account-cards>button.selected{border-color:#286ad6;background:#f1f6ff;box-shadow:inset 0 0 0 1px #286ad6}.account-cards>button.unavailable{background:#f2f3f5}.account-cards i{width:30px;height:30px;border-radius:50%;display:grid;place-items:center;color:#fff;background:#10264a;font-size:10px;font-style:normal;font-weight:900}.account-cards span{min-width:0;display:grid}.account-cards strong{overflow:hidden;color:#17243a;font-size:11px;text-overflow:ellipsis;white-space:nowrap}.account-cards small{overflow:hidden;color:#7f899a;font-size:9px;text-overflow:ellipsis;white-space:nowrap}.account-cards em{grid-column:1/3;color:#50719d;font-size:9px;font-style:normal}.account-cards b{position:absolute;right:7px;bottom:6px;color:#286ad6;font-size:9px}.account-cards>p{grid-column:1/-1;margin:10px;color:#7f899a;text-align:center;font-size:11px}.runtime-content{gap:42px;padding:0 0 8px}.progress-ring{width:145px;height:145px}.progress-ring:after{inset:12px}.progress-ring strong{font-size:29px}.runtime-content dl{gap:8px}.runtime-content dl div{grid-template-columns:92px 70px}.runtime-content dd{font-size:17px}.runtime-message{margin:16px 0 8px;padding:9px}.runtime-meta{grid-template-columns:repeat(4,1fr);gap:6px}.runtime-meta span{padding:7px;border:1px solid #edf0f4;border-radius:5px}.runtime-meta small{display:block;margin-top:2px;color:#7d899b}.table-scroll th,.table-scroll td,.detail-scroll th,.detail-scroll td{padding:8px}.rounds-panel{padding-bottom:8px}.confirm-account{display:flex!important;align-items:center;justify-content:flex-start!important;gap:10px!important;margin:12px 0!important;padding:10px;border:1px solid #dbe4f1;border-radius:7px;background:#f5f8fd}.confirm-account>span{width:34px;height:34px;border-radius:50%;display:grid;place-items:center;color:#fff;background:#10264a;font-size:10px;font-weight:900}.confirm-account p{display:grid;margin:0!important}.confirm-account b{color:#1c2b43}.confirm-account small{color:#728097}.detail-scroll td>small{display:block;color:#8791a2;font-size:10px}
@media(max-width:1250px){.account-cards{grid-template-columns:repeat(3,minmax(0,1fr))}.runtime-meta{grid-template-columns:repeat(2,1fr)}}
@media(max-width:720px){.sweeper-page{width:min(100% - 20px,1460px)}.metrics{margin:-50px 5px 0}.account-cards{grid-template-columns:1fr 1fr}.runtime-meta{grid-template-columns:1fr}}

/* Exact icon and compact-chart system from the approved component mockup. */
.hero-logo{border:0;border-radius:0;transform:none;color:#ffbd43}.hero-logo svg{width:40px;height:40px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}.hero-status .status-icon{width:25px;height:25px;display:grid;place-items:center;box-shadow:none}.hero-status .status-icon svg{width:15px;height:15px;fill:none;stroke:#fff;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round}.hero-status .status-icon.api{background:#b97913}.hero-status .status-icon.api.offline{background:#b94b42}.hero-status .status-icon.execution{background:#315baa}.hero-status .status-icon.execution.active{background:#286bd5;box-shadow:none}.metrics article{display:flex;flex-direction:column;justify-content:center;align-items:flex-start;gap:3px}.metric-label{width:100%;display:flex;align-items:center;gap:6px}.metric-label>i{width:22px;height:22px;display:grid;place-items:center;font-style:normal}.metric-label svg{width:21px;height:21px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}.metric-label i.amber{color:#e18b00}.metric-label i.blue{color:#2868ce}.metric-label i.navy{color:#172d56}.metric-label span{color:#526078;font-size:11px;font-weight:700}.metric-label b{color:#9aa5b5;font-size:9px;font-weight:500}.metrics strong{padding-left:28px}.metrics small{max-width:100%;padding-left:28px}.metrics .mini-track{width:calc(100% - 28px);margin-left:28px}.metrics small.amber-text{color:#b46f00}.workspace{align-items:start}.runtime-panel{width:100%}.runtime-content{display:grid;grid-template-columns:150px 190px;justify-content:center;gap:34px;padding:2px 0 10px}.progress-ring{width:132px;height:132px;justify-self:center;background:conic-gradient(#2868d0 var(--progress),#e6ecf4 0)}.progress-ring:after{inset:11px}.progress-ring strong{font-size:28px}.progress-ring span{font-size:10px}.runtime-content dl{width:190px}.runtime-content dl div{grid-template-columns:112px 68px}.runtime-content dt{font-size:13px}.runtime-content dd{font-size:16px}.wide-track{height:7px;margin-top:1px}
/* FOLIO compact data theme */
.sweeper-page{color:var(--folio-ink)}.hero{border-radius:19px;background:radial-gradient(circle at 82% 12%,rgba(167,211,184,.14),transparent 30%),linear-gradient(130deg,#143b2e,#184d39 66%,#236a4c);box-shadow:0 20px 52px rgba(20,59,46,.15)}.hero-logo{color:#a7d3b8}.hero-title p{color:#a7d3b8}.hero-title span{color:rgba(255,255,255,.63)}.hero-status{border-color:rgba(255,255,255,.15);border-radius:13px;background:rgba(0,0,0,.1)}.hero-status .status-icon.api{background:#8c6527}.hero-status .status-icon.api.offline{background:var(--folio-red)}.hero-status .status-icon.execution{background:rgba(255,255,255,.12)}.hero-status .status-icon.execution.active{background:#4d8c6d}.hero-status span{color:rgba(255,255,255,.52)}
.alert{border-radius:11px}.alert.error{border-color:#ebceca;color:var(--folio-red);background:var(--folio-red-soft)}.alert.notice{border-color:#c8dfd0;color:var(--folio-green);background:var(--folio-green-soft)}
.metrics article{border-color:var(--folio-line);border-radius:15px;background:#fff;box-shadow:var(--folio-shadow)}.metrics span{color:var(--folio-muted)}.metrics strong{color:var(--folio-ink)}.metrics small{color:#88918b}.metrics .account-caption,.metrics small.green-text{color:var(--folio-green)}.metric-label i.green{color:var(--folio-green)}.metric-label i.amber{color:var(--folio-amber)}.metric-label i.navy{color:var(--folio-blue)}.mini-track{background:#e9eee9}.mini-track i{background:var(--folio-amber)}.mini-track.blue i{background:var(--folio-green)}
.panel{border-color:var(--folio-line);border-radius:16px;background:#fff;box-shadow:var(--folio-shadow)}.panel header span{color:var(--folio-muted)}.panel header h2{color:var(--folio-ink)}.round-chip,.attempt-chip{color:var(--folio-green)!important;background:var(--folio-green-soft)}.search-wrap>input,.form-fields input{border-color:#dfe4df;border-radius:11px;color:var(--folio-ink);background:#fff}.search-wrap>input:focus,.form-fields input:focus{border-color:var(--folio-green);box-shadow:0 0 0 3px rgba(35,106,76,.1)}.search-wrap>span{color:var(--folio-green)}.suggestions{border-color:var(--folio-line);border-radius:12px;box-shadow:0 18px 54px rgba(34,49,41,.1)}.suggestions button:hover{background:var(--folio-green-soft)}.recent-items button{border-color:var(--folio-line);color:var(--folio-green-dark);background:var(--folio-surface-soft)}.form-fields .budget-field input{border-color:#dfcda8;background:#fdfaf4;box-shadow:inset 3px 0 var(--folio-amber)}.calculation>div{background:var(--folio-surface-soft)}.calculation p{color:#665b43;background:var(--folio-amber-soft)}
.account-picker{border-color:var(--folio-line);border-radius:13px;background:var(--folio-surface-soft)}.account-heading>button{color:var(--folio-green)}.account-cards>button{border-color:var(--folio-line);border-radius:12px;background:#fff}.account-cards>button:hover:not(:disabled){border-color:#b9d0c1}.account-cards>button.selected{border-color:#8db7a0;background:#f2f8f4;box-shadow:inset 0 0 0 1px rgba(35,106,76,.18)}.account-cards i{background:var(--folio-green-deep)}.account-cards em,.account-cards b{color:var(--folio-green)}
.primary{border-color:var(--folio-green);color:#fff;background:var(--folio-green);box-shadow:0 8px 18px rgba(35,106,76,.17)}.primary:hover:not(:disabled){background:var(--folio-green-dark);transform:translateY(-1px)}.secondary{border-color:#e1e6e1;color:#405048;background:#f1f4f0}.dark{border-color:var(--folio-green-deep);background:var(--folio-green-deep)}.outline-red{border-color:#e6c5c1;color:var(--folio-red);background:var(--folio-red-soft)}.archive-note{color:var(--folio-muted);background:var(--folio-surface-soft)}
.progress-ring{background:conic-gradient(var(--folio-green) var(--progress),#e6ece7 0)}.wide-track{background:#e9eee9}.wide-track i{background:var(--folio-green)}.runtime-message{color:var(--folio-green-dark);background:var(--folio-green-soft)}.runtime-meta span{border-color:#edf0ed;color:#8a938d;background:var(--folio-surface-soft)}.runtime-meta strong{color:#35433b}.blue-text{color:var(--folio-green)}
.new-round{border-color:#bfd6c7;color:var(--folio-green);background:#f5faf7}.table-scroll th,.detail-scroll th{color:#4c5851;background:var(--folio-surface-soft);border-bottom:1px solid var(--folio-line)}.table-scroll td,.detail-scroll td{border-bottom-color:#edf0ed}.table-scroll tr.selected{background:#f2f8f4}.status-pill.running,.status-pill.completed,.order-pill.delivered{color:var(--folio-green);background:var(--folio-green-soft)}.status-pill.draft,.status-pill.paused,.order-pill.pending{color:var(--folio-amber);background:var(--folio-amber-soft)}.status-pill.stopped,.order-pill.failed{color:var(--folio-red);background:var(--folio-red-soft)}.open-round{border-color:#c5d8cb;color:var(--folio-green);background:#fff}.timeline i{background:var(--folio-green)}.confirm-modal{border-radius:18px;box-shadow:0 18px 54px rgba(34,49,41,.14)}.confirm-modal>span{color:var(--folio-green)}.confirm-account{border-color:var(--folio-line);background:var(--folio-surface-soft)}.confirm-account>span{background:var(--folio-green-deep)}

/* 2026 Folio refresh: visual hierarchy only.  All scan controls, polling,
   confirmation rules, and API calls above deliberately remain unchanged. */
.sweeper-page--folio-refresh {
  width: min(1440px, calc(100vw - 44px));
  gap: 18px;
  padding: 24px 0 58px;
}

.sweeper-page--folio-refresh .hero {
  min-height: 206px;
  padding: 30px 34px 80px;
  border: 1px solid rgba(255, 255, 255, .12);
  border-radius: 24px;
  background:
    radial-gradient(circle at 82% 0%, rgba(171, 222, 185, .2), transparent 31%),
    radial-gradient(circle at 28% 110%, rgba(115, 177, 137, .12), transparent 32%),
    linear-gradient(132deg, #123b2d 0%, #184d39 55%, #2b7253 100%);
  box-shadow: 0 24px 58px rgba(20, 59, 46, .18);
}

.sweeper-page--folio-refresh .hero-title { gap: 16px; }
.sweeper-page--folio-refresh .hero-logo { color: #b8dfc2; }
.sweeper-page--folio-refresh .hero-title p { color: #c6e5ce; letter-spacing: .14em; }
.sweeper-page--folio-refresh .hero-title h1 { font-size: clamp(28px, 3vw, 38px); letter-spacing: -.045em; }
.sweeper-page--folio-refresh .hero-title span { max-width: 720px; line-height: 1.75; }
.sweeper-page--folio-refresh .hero-status {
  gap: 18px;
  padding: 12px 16px;
  border-radius: 15px;
  background: rgba(8, 34, 24, .22);
  box-shadow: inset 0 1px rgba(255, 255, 255, .08);
}

.sweeper-page--folio-refresh .metrics {
  grid-template-columns: minmax(220px, 1.22fr) repeat(5, minmax(135px, 1fr));
  gap: 12px;
  margin: -52px 18px 0;
}

.sweeper-page--folio-refresh .metrics article {
  min-height: 116px;
  padding: 15px 16px;
  border-color: rgba(215, 227, 218, .95);
  border-radius: 16px;
  background: rgba(255, 255, 255, .98);
  box-shadow: 0 12px 30px rgba(26, 48, 36, .075);
}

.sweeper-page--folio-refresh .metrics strong { font-size: 22px; }
.sweeper-page--folio-refresh .metrics strong.item-name { font-size: 16px; }
.sweeper-page--folio-refresh .metric-label { gap: 8px; }
.sweeper-page--folio-refresh .metric-label > i {
  width: 25px;
  height: 25px;
  border-radius: 8px;
  background: var(--folio-surface-tint);
}
.sweeper-page--folio-refresh .metrics strong,
.sweeper-page--folio-refresh .metrics small,
.sweeper-page--folio-refresh .metrics .mini-track { margin-left: 0; padding-left: 0; width: 100%; }
.sweeper-page--folio-refresh .metrics small { line-height: 1.35; }
.sweeper-page--folio-refresh .mini-track { height: 5px; }

.sweeper-page--folio-refresh .workspace {
  grid-template-columns: minmax(0, 1.18fr) minmax(380px, .82fr);
  gap: 18px;
}

.sweeper-page--folio-refresh .panel {
  border-color: var(--folio-line);
  border-radius: 19px;
  padding: 21px;
  box-shadow: var(--folio-shadow);
}

.sweeper-page--folio-refresh .panel header { margin-bottom: 17px; }
.sweeper-page--folio-refresh .panel header h2 { font-size: 20px; letter-spacing: -.03em; }
.sweeper-page--folio-refresh .round-chip,
.sweeper-page--folio-refresh .attempt-chip {
  padding: 6px 10px;
  border-radius: 999px !important;
  font-size: 11px;
}

.sweeper-page--folio-refresh .field { grid-template-columns: 104px minmax(0, 1fr); }
.sweeper-page--folio-refresh .field label,
.sweeper-page--folio-refresh .form-fields label { color: var(--folio-muted-strong); }
.sweeper-page--folio-refresh .search-wrap > input,
.sweeper-page--folio-refresh .form-fields input {
  min-height: 42px;
  height: 42px;
  border-color: var(--folio-line-strong);
  border-radius: 11px;
  background: #fff;
}
.sweeper-page--folio-refresh .search-wrap > input:focus,
.sweeper-page--folio-refresh .form-fields input:focus { box-shadow: var(--folio-focus-ring); }
.sweeper-page--folio-refresh .suggestions { border-radius: 14px; box-shadow: var(--folio-shadow-float); }
.sweeper-page--folio-refresh .recent-items { margin: 13px 0 17px; }
.sweeper-page--folio-refresh .recent-items button { padding: 6px 10px; }

.sweeper-page--folio-refresh .account-picker {
  margin: 14px 0 17px;
  padding: 13px;
  border-color: var(--folio-line);
  border-radius: 15px;
  background: linear-gradient(135deg, #f7faf7, #f3f8f5);
}
.sweeper-page--folio-refresh .account-cards { gap: 9px; }
.sweeper-page--folio-refresh .account-cards > button {
  min-height: 92px;
  border-color: var(--folio-line);
  border-radius: 12px;
}
.sweeper-page--folio-refresh .account-cards > button.selected {
  border-color: #8ebca1;
  background: #f1f8f3;
  box-shadow: inset 0 0 0 1px rgba(35, 106, 76, .14);
}
.sweeper-page--folio-refresh .form-body { gap: 14px; }
.sweeper-page--folio-refresh .calculation > div,
.sweeper-page--folio-refresh .calculation p { border-radius: 11px; }
.sweeper-page--folio-refresh .form-actions { gap: 12px; margin-top: 18px; }
.sweeper-page--folio-refresh .primary,
.sweeper-page--folio-refresh .secondary,
.sweeper-page--folio-refresh .dark,
.sweeper-page--folio-refresh .outline-red,
.sweeper-page--folio-refresh .new-round,
.sweeper-page--folio-refresh .open-round { border-radius: 10px; }

.sweeper-page--folio-refresh .runtime-panel {
  background: linear-gradient(160deg, #ffffff, #f5f9f6);
}
.sweeper-page--folio-refresh .runtime-content { gap: clamp(22px, 4vw, 52px); padding: 4px 0 15px; }
.sweeper-page--folio-refresh .progress-ring {
  width: 158px;
  height: 158px;
  box-shadow: 0 12px 24px rgba(35, 106, 76, .08);
}
.sweeper-page--folio-refresh .runtime-content dl { gap: 11px; }
.sweeper-page--folio-refresh .runtime-content dl div { grid-template-columns: 118px 78px; }
.sweeper-page--folio-refresh .runtime-content dd { font-size: 18px; }
.sweeper-page--folio-refresh .runtime-meta { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; }
.sweeper-page--folio-refresh .runtime-meta span { min-height: 58px; padding: 10px 11px; border-radius: 10px; }
.sweeper-page--folio-refresh .runtime-actions { margin-top: 18px; }

.sweeper-page--folio-refresh .rounds-panel,
.sweeper-page--folio-refresh .detail-panel,
.sweeper-page--folio-refresh .events-panel { overflow: hidden; }
.sweeper-page--folio-refresh .table-scroll,
.sweeper-page--folio-refresh .detail-scroll { margin: 0 -4px; padding: 0 4px; }
.sweeper-page--folio-refresh .table-scroll th,
.sweeper-page--folio-refresh .detail-scroll th {
  padding-top: 11px;
  padding-bottom: 11px;
  background: #f5f8f5;
}
.sweeper-page--folio-refresh .table-scroll td,
.sweeper-page--folio-refresh .detail-scroll td { padding-top: 10px; padding-bottom: 10px; }
.sweeper-page--folio-refresh .table-scroll tbody tr:hover { background: #f7fbf8; }
.sweeper-page--folio-refresh .details-grid { gap: 18px; }
.sweeper-page--folio-refresh .timeline > div { grid-template-columns: 12px 112px minmax(0, 1fr); padding: 10px 0; }
.sweeper-page--folio-refresh .modal-backdrop { background: rgba(12, 30, 21, .55); }
.sweeper-page--folio-refresh .confirm-modal {
  border: 1px solid rgba(255, 255, 255, .6);
  border-radius: 22px;
  box-shadow: var(--folio-shadow-float);
}

@media (max-width: 1250px) {
  .sweeper-page--folio-refresh .metrics { grid-template-columns: repeat(3, minmax(0, 1fr)); margin-inline: 10px; }
  .sweeper-page--folio-refresh .workspace { grid-template-columns: 1fr; }
}

@media (max-width: 720px) {
  .sweeper-page--folio-refresh { width: min(100vw - 22px, 1440px); padding-top: 14px; gap: 14px; }
  .sweeper-page--folio-refresh .hero { min-height: 0; padding: 22px 18px 58px; border-radius: 18px; }
  .sweeper-page--folio-refresh .hero-title h1 { font-size: 27px; }
  .sweeper-page--folio-refresh .hero-status { align-self: stretch; }
  .sweeper-page--folio-refresh .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); margin: -38px 4px 0; gap: 8px; }
  .sweeper-page--folio-refresh .metrics article { min-height: 104px; padding: 12px; }
  .sweeper-page--folio-refresh .metrics article:first-child { grid-column: span 2; }
  .sweeper-page--folio-refresh .panel { padding: 15px; border-radius: 16px; }
  .sweeper-page--folio-refresh .field { grid-template-columns: 1fr; gap: 7px; }
  .sweeper-page--folio-refresh .runtime-content { grid-template-columns: 130px minmax(0, 1fr); gap: 14px; }
  .sweeper-page--folio-refresh .progress-ring { width: 130px; height: 130px; }
  .sweeper-page--folio-refresh .runtime-content dl div { grid-template-columns: 1fr auto; }
  .sweeper-page--folio-refresh .timeline > div { grid-template-columns: 10px minmax(0, 1fr); }
}

/* Preview-aligned green operations system with cropped Krill scanner artwork. */
.sweeper-page--minimal-v2 {
  color: var(--minimal-ink);
  gap: 16px;
}
.sweeper-page--minimal-v2 .hero {
  min-height: 154px;
  padding: 20px 24px;
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 20px;
  overflow: hidden;
  color: var(--minimal-ink);
  border: 1px solid var(--minimal-line);
  border-radius: 20px;
  background: var(--minimal-surface);
  box-shadow: var(--minimal-shadow);
}
.sweeper-page--minimal-v2 .hero-title { align-items: center; gap: 16px; }
.sweeper-page--minimal-v2 .hero-title > .operation-visual-atom {
  box-shadow: 0 9px 22px rgba(20, 114, 63, .09);
}
.sweeper-page--minimal-v2 .hero-logo {
  width: 46px;
  height: 46px;
  color: var(--minimal-success);
  border: 1px solid var(--minimal-line);
  border-radius: 14px;
  background: var(--minimal-success-soft);
}
.sweeper-page--minimal-v2 .hero-title p { color: var(--minimal-success); }
.sweeper-page--minimal-v2 .hero-title h1 { color: var(--minimal-ink); }
.sweeper-page--minimal-v2 .hero-title span { color: var(--minimal-muted); }
.sweeper-page--minimal-v2 .hero-status {
  min-width: 238px;
  padding: 12px 14px;
  gap: 14px;
  color: var(--minimal-ink);
  border: 1px solid var(--minimal-line);
  border-radius: 14px;
  background: var(--minimal-surface-soft);
  box-shadow: none;
}
.sweeper-page--minimal-v2 .hero-status span { color: var(--minimal-muted); }
.sweeper-page--minimal-v2 .hero-status strong { color: var(--minimal-ink); }
.sweeper-page--minimal-v2 .hero-status b { background: var(--minimal-line-strong); }
.sweeper-page--minimal-v2 .hero-status .status-icon.api { background: var(--minimal-success); }
.sweeper-page--minimal-v2 .hero-status .status-icon.api.offline { background: var(--minimal-danger); }
.sweeper-page--minimal-v2 .hero-status .status-icon.execution { background: #93ad9c; }
.sweeper-page--minimal-v2 .hero-status .status-icon.execution.active { background: var(--minimal-blue); }

.sweeper-page--minimal-v2 .metrics {
  margin: 0;
  gap: 10px;
}
.sweeper-page--minimal-v2 .metrics article {
  min-height: 108px;
  border-color: var(--minimal-line);
  background: linear-gradient(145deg, #fff, #fbfdfb);
  box-shadow: var(--minimal-shadow);
}
.sweeper-page--minimal-v2 .metrics article:hover {
  border-color: #c7dccd;
  box-shadow: 0 10px 28px rgba(26, 62, 42, .07);
}
.sweeper-page--minimal-v2 .metrics span,
.sweeper-page--minimal-v2 .metrics small { color: var(--minimal-muted); }
.sweeper-page--minimal-v2 .metrics strong { color: var(--minimal-ink); }
.sweeper-page--minimal-v2 .metrics strong,
.sweeper-page--minimal-v2 .metrics small,
.sweeper-page--minimal-v2 .metrics .mini-track {
  width: calc(100% - 46px);
  margin-left: 46px;
  padding-left: 0;
}
.sweeper-page--minimal-v2 .metrics .account-caption,
.sweeper-page--minimal-v2 .metrics small.green-text { color: var(--minimal-blue); }
.sweeper-page--minimal-v2 .metric-label > i { background: var(--minimal-surface-soft); }
.sweeper-page--minimal-v2 .metric-label i.green,
.sweeper-page--minimal-v2 .metric-label i.navy { color: var(--minimal-blue); }
.sweeper-page--minimal-v2 .metric-label i.amber { color: var(--minimal-amber); }
.sweeper-page--minimal-v2 .mini-track { background: #eceeef; }
.sweeper-page--minimal-v2 .mini-track i { background: var(--minimal-amber); }
.sweeper-page--minimal-v2 .mini-track.blue i { background: var(--minimal-blue); }

.sweeper-page--minimal-v2 .panel {
  border-color: var(--minimal-line);
  background: var(--minimal-surface);
  box-shadow: var(--minimal-shadow);
}
.sweeper-page--minimal-v2 .panel:hover { border-color: #c7dccd; }
.sweeper-page--minimal-v2 .panel header span,
.sweeper-page--minimal-v2 .panel header small { color: var(--minimal-muted); }
.sweeper-page--minimal-v2 .panel header h2 { color: var(--minimal-ink); }
.sweeper-page--minimal-v2 .round-chip,
.sweeper-page--minimal-v2 .attempt-chip {
  color: var(--minimal-success) !important;
  background: var(--minimal-blue-soft);
}
.sweeper-page--minimal-v2 .search-wrap > input,
.sweeper-page--minimal-v2 .form-fields input {
  color: var(--minimal-ink);
  border-color: var(--minimal-line-strong);
  background: #fff;
}
.sweeper-page--minimal-v2 .search-wrap > input:focus,
.sweeper-page--minimal-v2 .form-fields input:focus {
  border-color: #79ad89;
  box-shadow: 0 0 0 3px rgba(36, 131, 79, .14);
}
.sweeper-page--minimal-v2 .search-wrap > span { color: var(--minimal-blue); }
.sweeper-page--minimal-v2 .suggestions { border-color: var(--minimal-line); box-shadow: var(--minimal-shadow-float); }
.sweeper-page--minimal-v2 .suggestions button:hover { background: var(--minimal-blue-soft); }
.sweeper-page--minimal-v2 .recent-items button {
  color: var(--minimal-ink);
  border-color: var(--minimal-line);
  background: var(--minimal-surface-soft);
}
.sweeper-page--minimal-v2 .account-picker {
  border-color: var(--minimal-line);
  background: #f7f7f6;
}
.sweeper-page--minimal-v2 .account-heading > button { color: var(--minimal-success); }
.sweeper-page--minimal-v2 .account-cards > button { border-color: var(--minimal-line); }
.sweeper-page--minimal-v2 .account-cards > button:hover:not(:disabled) { border-color: #9bc6a8; }
.sweeper-page--minimal-v2 .account-cards > button.selected {
  border-color: #79ad89;
  background: var(--minimal-blue-soft);
  box-shadow: inset 0 0 0 1px rgba(36, 131, 79, .16);
}
.sweeper-page--minimal-v2 .account-cards i { background: var(--minimal-success); }
.sweeper-page--minimal-v2 .account-cards em,
.sweeper-page--minimal-v2 .account-cards b { color: var(--minimal-success); }
.sweeper-page--minimal-v2 .form-fields .budget-field input {
  border-color: #ead4ad;
  background: #fffbf4;
  box-shadow: inset 3px 0 var(--minimal-amber);
}
.sweeper-page--minimal-v2 .calculation > div { background: var(--minimal-surface-soft); }
.sweeper-page--minimal-v2 .calculation p { color: #6f5b39; background: var(--minimal-amber-soft); }
.sweeper-page--minimal-v2 .primary {
  color: #fff;
  border-color: var(--minimal-success);
  background: var(--minimal-success);
  box-shadow: 0 9px 20px rgba(20, 114, 63, .16);
}
.sweeper-page--minimal-v2 .primary:hover:not(:disabled) { background: #0d5e35; }
.sweeper-page--minimal-v2 .secondary {
  color: var(--minimal-ink);
  border-color: var(--minimal-line-strong);
  background: #fff;
}
.sweeper-page--minimal-v2 .dark { border-color: var(--minimal-success); background: var(--minimal-success); }
.sweeper-page--minimal-v2 .outline-red { color: var(--minimal-danger); border-color: #edc9c7; background: var(--minimal-danger-soft); }
.sweeper-page--minimal-v2 .archive-note { color: var(--minimal-muted); background: var(--minimal-surface-soft); }

.sweeper-page--minimal-v2 .runtime-panel { background: linear-gradient(160deg, #fff, #f3f8f4); }
.sweeper-page--minimal-v2 .progress-ring {
  background: conic-gradient(var(--minimal-blue) var(--progress), #e8eaec 0);
  box-shadow: none;
}
.sweeper-page--minimal-v2 .wide-track { background: #e8eaec; }
.sweeper-page--minimal-v2 .wide-track i { background: var(--minimal-blue); }
.sweeper-page--minimal-v2 .runtime-message { color: var(--minimal-success); background: var(--minimal-blue-soft); }
.sweeper-page--minimal-v2 .runtime-meta span { color: #858a90; border-color: var(--minimal-line); background: #fff; }
.sweeper-page--minimal-v2 .runtime-meta strong { color: var(--minimal-ink); }
.sweeper-page--minimal-v2 .blue-text { color: var(--minimal-success); }
.sweeper-page--minimal-v2 .new-round,
.sweeper-page--minimal-v2 .open-round { color: var(--minimal-ink); border-color: var(--minimal-line-strong); background: #fff; }
.sweeper-page--minimal-v2 .table-scroll th,
.sweeper-page--minimal-v2 .detail-scroll th { color: #506057; border-color: var(--minimal-line); background: var(--minimal-surface-soft); }
.sweeper-page--minimal-v2 .table-scroll td,
.sweeper-page--minimal-v2 .detail-scroll td { border-color: #eceeef; }
.sweeper-page--minimal-v2 .table-scroll tr.selected { background: var(--minimal-blue-soft); }
.sweeper-page--minimal-v2 .table-scroll tbody tr:hover { background: #f5faf6; }
.sweeper-page--minimal-v2 .status-pill.running { color: var(--minimal-success); background: var(--minimal-blue-soft); }
.sweeper-page--minimal-v2 .status-pill.completed,
.sweeper-page--minimal-v2 .order-pill.delivered { color: var(--minimal-success); background: var(--minimal-success-soft); }
.sweeper-page--minimal-v2 .status-pill.draft,
.sweeper-page--minimal-v2 .status-pill.paused,
.sweeper-page--minimal-v2 .order-pill.pending { color: #9b650e; background: var(--minimal-amber-soft); }
.sweeper-page--minimal-v2 .status-pill.stopped,
.sweeper-page--minimal-v2 .order-pill.failed { color: var(--minimal-danger); background: var(--minimal-danger-soft); }
.sweeper-page--minimal-v2 .timeline i { background: var(--minimal-blue); }
.sweeper-page--minimal-v2 .modal-backdrop { background: rgba(24, 26, 29, .48); }
.sweeper-page--minimal-v2 .confirm-modal { border-color: var(--minimal-line); box-shadow: var(--minimal-shadow-float); }
.sweeper-page--minimal-v2 .confirm-modal > span { color: var(--minimal-amber); }
.sweeper-page--minimal-v2 .confirm-account { border-color: var(--minimal-line); background: var(--minimal-surface-soft); }
.sweeper-page--minimal-v2 .confirm-account > span { background: var(--minimal-success); }

@media (max-width: 1250px) {
  .sweeper-page--minimal-v2 .hero { grid-template-columns: minmax(0, 1fr) auto; }
}
@media (max-width: 720px) {
  .sweeper-page--minimal-v2 .hero { padding: 19px 16px; grid-template-columns: 1fr; }
  .sweeper-page--minimal-v2 .hero-status { min-width: 0; }
  .sweeper-page--minimal-v2 .metrics { margin: 0; }
  .sweeper-page--minimal-v2 .hero-title > .operation-visual-atom { width: 56px; height: 56px; }
}

/* Operations component board: the selectors below intentionally come last.
   They only change presentation; the sweep state machine and API calls stay above. */
.sweeper-page--minimal-v2 {
  font-family: var(
    --ops-font,
    "Source Han Sans SC",
    "Noto Sans CJK SC",
    "Microsoft YaHei UI",
    "PingFang SC",
    sans-serif
  );
  color: var(--ops-neutral-900, #1f2937);
}

.sweeper-page--minimal-v2 .hero {
  border-color: var(--ops-neutral-300, #d1d5db);
  border-radius: var(--ops-radius-lg, 12px);
  background:
    radial-gradient(circle at 84% 18%, rgba(49, 174, 95, .11), transparent 25%),
    linear-gradient(145deg, #ffffff, var(--ops-primary-soft, #e9f6ed));
  box-shadow: var(--ops-shadow-medium, 0 8px 24px rgba(20, 58, 38, .075));
}
.sweeper-page--minimal-v2 .hero-title p { color: var(--ops-primary, #0f8a45); }
.sweeper-page--minimal-v2 .hero-title h1 { color: var(--ops-neutral-900, #1f2937); }
.sweeper-page--minimal-v2 .hero-title span,
.sweeper-page--minimal-v2 .hero-status span { color: var(--ops-neutral-700, #4b5563); }
.sweeper-page--minimal-v2 .hero-status {
  border-color: var(--ops-neutral-300, #d1d5db);
  border-radius: var(--ops-radius-md, 8px);
  color: var(--ops-neutral-900, #1f2937);
  background: rgba(255, 255, 255, .82);
  box-shadow: var(--ops-shadow-shallow, 0 1px 3px rgba(20, 58, 38, .07));
}
.sweeper-page--minimal-v2 .hero-status strong { color: var(--ops-neutral-900, #1f2937); }
.sweeper-page--minimal-v2 .hero-status b { background: var(--ops-neutral-300, #d1d5db); }
.sweeper-page--minimal-v2 .hero-status .status-icon.api { background: var(--ops-success, #15803d); }
.sweeper-page--minimal-v2 .hero-status .status-icon.api.offline { background: var(--ops-error, #c2413b); }
.sweeper-page--minimal-v2 .hero-status .status-icon.execution,
.sweeper-page--minimal-v2 .hero-status .status-icon.execution.active {
  background: var(--ops-neutral-700, #4b5563);
}

.sweeper-page--minimal-v2 .alert {
  border-radius: var(--ops-radius-sm, 6px);
  box-shadow: var(--ops-shadow-shallow, 0 1px 3px rgba(20, 58, 38, .07));
}
.sweeper-page--minimal-v2 .alert.error {
  color: var(--ops-error, #c2413b);
  border-color: #efc0bd;
  background: var(--ops-error-soft, #fdf0ef);
}
.sweeper-page--minimal-v2 .alert.notice {
  color: var(--ops-success, #15803d);
  border-color: #b9ddc5;
  background: var(--ops-success-soft, #eaf7ee);
}

.sweeper-page--minimal-v2 .metrics article {
  border-color: var(--ops-neutral-300, #d1d5db);
  border-radius: var(--ops-radius-md, 8px);
  background: #fff;
  box-shadow: var(--ops-shadow-shallow, 0 1px 3px rgba(20, 58, 38, .07));
}
.sweeper-page--minimal-v2 .metrics article:hover {
  border-color: #a8cfb4;
  box-shadow: var(--ops-shadow-medium, 0 8px 24px rgba(20, 58, 38, .075));
}
.sweeper-page--minimal-v2 .metrics span,
.sweeper-page--minimal-v2 .metrics small { color: var(--ops-neutral-700, #4b5563); }
.sweeper-page--minimal-v2 .metrics strong { color: var(--ops-neutral-900, #1f2937); }
.sweeper-page--minimal-v2 .metrics .account-caption,
.sweeper-page--minimal-v2 .metrics small.green-text { color: var(--ops-success, #15803d); }
.sweeper-page--minimal-v2 .metric-label > i { background: var(--ops-primary-soft, #e9f6ed); }
.sweeper-page--minimal-v2 .mini-track,
.sweeper-page--minimal-v2 .wide-track { background: var(--ops-neutral-100, #f3f4f6); }
.sweeper-page--minimal-v2 .mini-track i { background: var(--ops-warning, #b7791f); }
.sweeper-page--minimal-v2 .mini-track.blue i,
.sweeper-page--minimal-v2 .wide-track i { background: var(--ops-primary, #0f8a45); }

.sweeper-page--minimal-v2 .panel {
  border-color: var(--ops-neutral-300, #d1d5db);
  border-radius: var(--ops-radius-lg, 12px);
  background: #fff;
  box-shadow: var(--ops-shadow-medium, 0 8px 24px rgba(20, 58, 38, .075));
}
.sweeper-page--minimal-v2 .panel:hover { border-color: var(--ops-neutral-300, #d1d5db); }
.sweeper-page--minimal-v2 .panel header span,
.sweeper-page--minimal-v2 .panel header small { color: var(--ops-neutral-700, #4b5563); }
.sweeper-page--minimal-v2 .panel header h2 { color: var(--ops-neutral-900, #1f2937); }
.sweeper-page--minimal-v2 .round-chip,
.sweeper-page--minimal-v2 .attempt-chip {
  color: var(--ops-primary, #0f8a45) !important;
  background: var(--ops-primary-soft, #e9f6ed);
}

.sweeper-page--minimal-v2 .search-wrap > input,
.sweeper-page--minimal-v2 .form-fields input,
.sweeper-page--minimal-v2 .confirm-modal input {
  border-color: var(--ops-neutral-300, #d1d5db);
  border-radius: var(--ops-radius-sm, 6px);
  color: var(--ops-neutral-900, #1f2937);
  background: #fff;
  transition: border-color .16s ease, box-shadow .16s ease;
}
.sweeper-page--minimal-v2 .search-wrap > input:hover:not(:disabled),
.sweeper-page--minimal-v2 .form-fields input:hover:not(:disabled),
.sweeper-page--minimal-v2 .confirm-modal input:hover:not(:disabled) {
  border-color: #9cae9f;
}
.sweeper-page--minimal-v2 .search-wrap > input:focus,
.sweeper-page--minimal-v2 .form-fields input:focus,
.sweeper-page--minimal-v2 .confirm-modal input:focus {
  border-color: var(--ops-primary, #0f8a45);
  box-shadow: var(--ops-focus-ring, 0 0 0 3px rgba(19, 134, 71, .15));
}
.sweeper-page--minimal-v2 .search-wrap > input:disabled,
.sweeper-page--minimal-v2 .form-fields input:disabled {
  color: var(--ops-neutral-500, #9ca3af);
  background: var(--ops-neutral-100, #f3f4f6);
}
.sweeper-page--minimal-v2 .search-wrap > span { color: var(--ops-primary, #0f8a45); }
.sweeper-page--minimal-v2 .suggestions {
  border-color: var(--ops-neutral-300, #d1d5db);
  border-radius: var(--ops-radius-md, 8px);
  box-shadow: var(--ops-shadow-deep, 0 24px 64px rgba(15, 52, 32, .18));
}
.sweeper-page--minimal-v2 .suggestions button {
  border-radius: var(--ops-radius-sm, 6px);
  color: var(--ops-neutral-900, #1f2937);
}
.sweeper-page--minimal-v2 .suggestions button:hover { background: var(--ops-primary-soft, #e9f6ed); }
.sweeper-page--minimal-v2 .suggestions small,
.sweeper-page--minimal-v2 .suggestions p { color: var(--ops-neutral-700, #4b5563); }
.sweeper-page--minimal-v2 .suggestions em { color: var(--ops-warning, #b7791f); }
.sweeper-page--minimal-v2 .recent-items button {
  border-color: var(--ops-neutral-300, #d1d5db);
  border-radius: var(--ops-radius-sm, 6px);
  color: var(--ops-neutral-700, #4b5563);
  background: #fff;
}
.sweeper-page--minimal-v2 .recent-items button:hover:not(:disabled) {
  color: var(--ops-primary, #0f8a45);
  border-color: #a8cfb4;
  background: var(--ops-primary-soft, #e9f6ed);
}

.sweeper-page--minimal-v2 .account-picker {
  border-color: var(--ops-neutral-300, #d1d5db);
  border-radius: var(--ops-radius-lg, 12px);
  background: var(--ops-neutral-100, #f3f4f6);
}
.sweeper-page--minimal-v2 .account-heading > button {
  border: 0;
  color: var(--ops-primary, #0f8a45);
  background: transparent;
}
.sweeper-page--minimal-v2 .account-heading > button:hover:not(:disabled) { color: var(--ops-primary-hover, #0b7439); }
.sweeper-page--minimal-v2 .account-cards > button {
  border-color: var(--ops-neutral-300, #d1d5db);
  border-radius: var(--ops-radius-md, 8px);
  color: var(--ops-neutral-900, #1f2937);
  background: #fff;
  box-shadow: var(--ops-shadow-shallow, 0 1px 3px rgba(20, 58, 38, .07));
}
.sweeper-page--minimal-v2 .account-cards > button:hover:not(:disabled) {
  border-color: #92c3a1;
  box-shadow: var(--ops-shadow-medium, 0 8px 24px rgba(20, 58, 38, .075));
}
.sweeper-page--minimal-v2 .account-cards > button.selected {
  border-color: var(--ops-primary, #0f8a45);
  background: var(--ops-primary-soft, #e9f6ed);
  box-shadow: inset 0 0 0 1px rgba(15, 138, 69, .2);
}
.sweeper-page--minimal-v2 .account-cards > button.unavailable {
  color: var(--ops-neutral-500, #9ca3af);
  border-color: var(--ops-neutral-300, #d1d5db);
  background: var(--ops-neutral-100, #f3f4f6);
  box-shadow: none;
}
.sweeper-page--minimal-v2 .account-cards i { background: var(--ops-primary, #0f8a45); }
.sweeper-page--minimal-v2 .account-cards em,
.sweeper-page--minimal-v2 .account-cards b { color: var(--ops-primary, #0f8a45); }
.sweeper-page--minimal-v2 .account-cards > button.unavailable em,
.sweeper-page--minimal-v2 .account-cards > button.unavailable b { color: var(--ops-neutral-500, #9ca3af); }

.sweeper-page--minimal-v2 .form-fields .budget-field input {
  border-color: #e2c48d;
  background: #fffbf2;
  box-shadow: inset 3px 0 var(--ops-warning, #b7791f);
}
.sweeper-page--minimal-v2 .calculation > div {
  border-radius: var(--ops-radius-sm, 6px);
  background: var(--ops-neutral-100, #f3f4f6);
}
.sweeper-page--minimal-v2 .calculation p {
  border-radius: var(--ops-radius-sm, 6px);
  color: #76531b;
  background: var(--ops-warning-soft, #fff6df);
}

.sweeper-page--minimal-v2 .primary,
.sweeper-page--minimal-v2 .secondary,
.sweeper-page--minimal-v2 .dark,
.sweeper-page--minimal-v2 .outline-red,
.sweeper-page--minimal-v2 .new-round,
.sweeper-page--minimal-v2 .open-round {
  border-radius: var(--ops-radius-sm, 6px);
  transition: color .16s ease, border-color .16s ease, background .16s ease, box-shadow .16s ease;
}
.sweeper-page--minimal-v2 .primary {
  color: #fff;
  border-color: var(--ops-primary, #0f8a45);
  background: var(--ops-primary, #0f8a45);
  box-shadow: var(--ops-shadow-shallow, 0 1px 3px rgba(20, 58, 38, .07));
}
.sweeper-page--minimal-v2 .primary:hover:not(:disabled) {
  color: #fff;
  border-color: var(--ops-primary-hover, #0b7439);
  background: var(--ops-primary-hover, #0b7439);
  box-shadow: var(--ops-shadow-medium, 0 8px 24px rgba(20, 58, 38, .075));
  transform: none;
}
.sweeper-page--minimal-v2 .secondary,
.sweeper-page--minimal-v2 .new-round,
.sweeper-page--minimal-v2 .open-round {
  color: var(--ops-primary, #0f8a45);
  border-color: #9bc6a8;
  background: #fff;
}
.sweeper-page--minimal-v2 .secondary:hover:not(:disabled),
.sweeper-page--minimal-v2 .new-round:hover:not(:disabled),
.sweeper-page--minimal-v2 .open-round:hover:not(:disabled) {
  color: var(--ops-primary-hover, #0b7439);
  border-color: var(--ops-primary, #0f8a45);
  background: var(--ops-primary-soft, #e9f6ed);
}
.sweeper-page--minimal-v2 .dark {
  color: #fff;
  border-color: var(--ops-neutral-700, #4b5563);
  background: var(--ops-neutral-700, #4b5563);
}
.sweeper-page--minimal-v2 .dark:hover:not(:disabled) { background: var(--ops-neutral-900, #1f2937); }
.sweeper-page--minimal-v2 .outline-red {
  color: var(--ops-error, #c2413b);
  border-color: #e4aaa6;
  background: #fff;
}
.sweeper-page--minimal-v2 .outline-red:hover:not(:disabled) {
  border-color: var(--ops-error, #c2413b);
  background: var(--ops-error-soft, #fdf0ef);
}
.sweeper-page--minimal-v2 button:focus-visible {
  outline: 3px solid rgba(15, 138, 69, .2);
  outline-offset: 2px;
}

.sweeper-page--minimal-v2 .archive-note {
  border-radius: var(--ops-radius-sm, 6px);
  color: var(--ops-neutral-700, #4b5563);
  background: var(--ops-neutral-100, #f3f4f6);
}
.sweeper-page--minimal-v2 .runtime-panel {
  background: linear-gradient(160deg, #fff, var(--ops-primary-soft, #e9f6ed));
}
.sweeper-page--minimal-v2 .progress-ring {
  background: conic-gradient(var(--ops-primary, #0f8a45) var(--progress), var(--ops-neutral-100, #f3f4f6) 0);
  box-shadow: var(--ops-shadow-medium, 0 8px 24px rgba(20, 58, 38, .075));
}
.sweeper-page--minimal-v2 .runtime-message {
  border-radius: var(--ops-radius-sm, 6px);
  color: var(--ops-neutral-700, #4b5563);
  background: var(--ops-neutral-100, #f3f4f6);
}
.sweeper-page--minimal-v2 .runtime-meta span {
  border-color: var(--ops-neutral-300, #d1d5db);
  border-radius: var(--ops-radius-sm, 6px);
  color: var(--ops-neutral-500, #9ca3af);
  background: #fff;
}
.sweeper-page--minimal-v2 .runtime-meta strong { color: var(--ops-neutral-900, #1f2937); }
.sweeper-page--minimal-v2 .blue-text { color: var(--ops-primary, #0f8a45); }

.sweeper-page--minimal-v2 .table-scroll th,
.sweeper-page--minimal-v2 .detail-scroll th {
  color: var(--ops-neutral-700, #4b5563);
  border-color: var(--ops-neutral-300, #d1d5db);
  background: var(--ops-neutral-100, #f3f4f6);
}
.sweeper-page--minimal-v2 .table-scroll td,
.sweeper-page--minimal-v2 .detail-scroll td {
  color: var(--ops-neutral-900, #1f2937);
  border-color: #e5e7eb;
}
.sweeper-page--minimal-v2 .table-scroll tr.selected { background: var(--ops-primary-soft, #e9f6ed); }
.sweeper-page--minimal-v2 .table-scroll tbody tr:hover { background: #f4faf6; }
.sweeper-page--minimal-v2 .status-pill,
.sweeper-page--minimal-v2 .order-pill {
  font-size: 11px;
  font-weight: 700;
}
.sweeper-page--minimal-v2 .status-pill.running {
  color: var(--ops-neutral-state, #59645d);
  background: var(--ops-neutral-state-soft, #f0f2f0);
}
.sweeper-page--minimal-v2 .status-pill.completed,
.sweeper-page--minimal-v2 .order-pill.delivered {
  color: var(--ops-success, #15803d);
  background: var(--ops-success-soft, #eaf7ee);
}
.sweeper-page--minimal-v2 .status-pill.draft,
.sweeper-page--minimal-v2 .status-pill.paused,
.sweeper-page--minimal-v2 .order-pill.pending {
  color: var(--ops-warning, #a16207);
  background: var(--ops-warning-soft, #fff6df);
}
.sweeper-page--minimal-v2 .status-pill.stopped,
.sweeper-page--minimal-v2 .order-pill.failed {
  color: var(--ops-error, #c2413b);
  background: var(--ops-error-soft, #fdf0ef);
}
.sweeper-page--minimal-v2 .empty {
  color: var(--ops-neutral-500, #9ca3af);
  background: linear-gradient(180deg, #fff, var(--ops-neutral-100, #f3f4f6));
}
.sweeper-page--minimal-v2 .empty::before {
  content: "◇";
  display: block;
  margin-bottom: 5px;
  color: #aeb5bd;
  font-size: 22px;
  line-height: 1;
}
.sweeper-page--minimal-v2 .timeline i { background: var(--ops-primary, #0f8a45); }

.sweeper-page--minimal-v2 .modal-backdrop {
  background: rgba(31, 41, 55, .52);
  backdrop-filter: blur(5px);
}
.sweeper-page--minimal-v2 .confirm-modal {
  border: 1px solid var(--ops-neutral-300, #d1d5db);
  border-radius: var(--ops-radius-lg, 12px);
  box-shadow: var(--ops-shadow-deep, 0 24px 64px rgba(15, 52, 32, .18));
}
.sweeper-page--minimal-v2 .confirm-heading {
  display: flex;
  align-items: center;
  gap: 12px;
}
.sweeper-page--minimal-v2 .confirm-heading > .operation-visual-atom {
  flex: 0 0 auto;
  border-radius: var(--ops-radius-md, 8px);
}
.sweeper-page--minimal-v2 .confirm-heading span {
  color: var(--ops-warning, #a16207);
  font-size: 11px;
  font-weight: 800;
  letter-spacing: .08em;
}
.sweeper-page--minimal-v2 .confirm-heading h2 {
  margin: 3px 0 0;
  color: var(--ops-neutral-900, #1f2937);
}
.sweeper-page--minimal-v2 .confirm-modal > p { color: var(--ops-neutral-700, #4b5563); }
.sweeper-page--minimal-v2 .confirm-account {
  border-color: var(--ops-neutral-300, #d1d5db);
  border-radius: var(--ops-radius-md, 8px);
  background: var(--ops-neutral-100, #f3f4f6);
}
.sweeper-page--minimal-v2 .confirm-account > span { background: var(--ops-primary, #0f8a45); }

@media (max-width: 720px) {
  .sweeper-page--minimal-v2 .hero,
  .sweeper-page--minimal-v2 .panel,
  .sweeper-page--minimal-v2 .account-picker,
  .sweeper-page--minimal-v2 .confirm-modal { border-radius: var(--ops-radius-lg, 12px); }
}
.suggestions .catalog-load-more{display:block;grid-template-columns:1fr;color:var(--folio-green,#28714d);text-align:center;font-weight:750}.suggestions .catalog-load-more:disabled{opacity:.55}
</style>
