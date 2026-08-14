<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { RouterLink, RouterView } from "vue-router";
import FolioIcon from "../components/FolioIcon.vue";

type SharedDashboard = {
  config?: { allowRealExecution?: boolean };
  listingsCircuit?: ListingsCircuit;
};

type RuntimeCookieAccount = {
  accountId?: string;
  accountName?: string;
  name?: string;
  steamId?: string;
  valid?: boolean;
  status?: string;
  lastCheckedAt?: string | null;
  error?: string | null;
  nextRetryAt?: string | null;
  currencyId?: number | null;
  currency?: string | null;
  currencyStatus?: "cny" | "non_cny" | "unknown" | string;
  currencyCheckedAt?: string | null;
  currencyError?: string | null;
};
type RuntimeCookies = {
  status?: string;
  validCount?: number;
  totalCount?: number;
  accounts?: RuntimeCookieAccount[];
};
type ExecutorRuntime = { enabled?: boolean; status?: string; preparing?: boolean };

type ListingsCircuit = {
  status?: "closed" | "open";
  isBlocking?: boolean;
  remainingSeconds?: number;
  first429At?: string | null;
  last429At?: string | null;
  cooldownUntil?: string | null;
  triggerAccountName?: string | null;
  triggerAccountId?: string | null;
  triggerSteamId?: string | null;
  triggerMarketHashName?: string | null;
  consecutive429Count?: number;
  cooldownSeconds?: number;
};

const apiOnline = ref<boolean | null>(null);
const realExecutionAllowed = ref(false);
const error = ref("");
const listingsCircuit = ref<ListingsCircuit>({ status: "closed", isBlocking: false });
const runtimeCookies = ref<RuntimeCookies>({});
const profitRuntime = ref<ExecutorRuntime>({});
const cookieExpanded = ref(false);
const nowMs = ref(Date.now());
let statusTimer: ReturnType<typeof setInterval> | null = null;
let countdownTimer: ReturnType<typeof setInterval> | null = null;

const circuitVisible = computed(() => {
  if (listingsCircuit.value.status !== "open") return false;
  const target = listingsCircuit.value.cooldownUntil;
  if (!target) return true;
  const targetMs = new Date(target).getTime();
  return !Number.isFinite(targetMs) || targetMs > nowMs.value;
});
const circuitRemainingLabel = computed(() => {
  const target = listingsCircuit.value.cooldownUntil;
  if (!target) return "-";
  const seconds = Math.max(0, Math.ceil((new Date(target).getTime() - nowMs.value) / 1000));
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
});
function localTime(value?: string | null): string {
  if (!value) return "-";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("zh-CN", { hour12: false });
}

function cookieCurrencyLabel(account: RuntimeCookieAccount): string {
  if (account.currencyStatus === "cny") {
    return `${account.currency || "CNY"} · ID ${account.currencyId ?? 23}`;
  }
  if (account.currencyStatus === "non_cny") {
    return `${account.currency || "非人民币"} · ID ${account.currencyId ?? "?"}`;
  }
  return "币种未知";
}

function handleDashboardStatus(event: Event): void {
  const detail = (event as CustomEvent<{ allowRealExecution?: boolean }>).detail;
  if (typeof detail?.allowRealExecution === "boolean") {
    realExecutionAllowed.value = detail.allowRealExecution;
    apiOnline.value = true;
  }
}

async function readError(response: Response): Promise<string> {
  try {
    const body = await response.json() as { error?: string; detail?: string };
    return body.error || body.detail || response.statusText;
  } catch {
    return response.statusText;
  }
}

async function refreshSharedStatus(): Promise<void> {
  try {
    const [response, cookieResponse, runtimeResponse] = await Promise.all([
      fetch("/api/profit-trade/dashboard", { cache: "no-store" }),
      fetch("/api/runtime/cookies", { cache: "no-store" }),
      fetch("/api/runtime/state?executor=profit_trade", { cache: "no-store" }),
    ]);
    if (!response.ok) throw new Error(await readError(response));
    const payload = await response.json() as SharedDashboard;
    apiOnline.value = true;
    realExecutionAllowed.value = Boolean(payload.config?.allowRealExecution);
    listingsCircuit.value = payload.listingsCircuit || { status: "closed", isBlocking: false };
    if (cookieResponse.ok) {
      const cookiePayload = await cookieResponse.json() as RuntimeCookies & { gate?: RuntimeCookies | string; data?: RuntimeCookies };
      runtimeCookies.value = cookiePayload.gate && typeof cookiePayload.gate === "object"
        ? cookiePayload.gate
        : cookiePayload.data || cookiePayload;
    }
    if (runtimeResponse.ok) {
      const runtimePayload = await runtimeResponse.json() as { state?: ExecutorRuntime; data?: ExecutorRuntime } & ExecutorRuntime;
      profitRuntime.value = runtimePayload.state || runtimePayload.data || runtimePayload;
    }
    error.value = "";
  } catch (reason) {
    apiOnline.value = false;
    error.value = reason instanceof Error ? reason.message : String(reason);
  }
}

onMounted(() => {
  void refreshSharedStatus();
  statusTimer = setInterval(() => void refreshSharedStatus(), 30_000);
  countdownTimer = setInterval(() => { nowMs.value = Date.now(); }, 1000);
  window.addEventListener("profit-trade:dashboard-status", handleDashboardStatus);
});

onUnmounted(() => {
  if (statusTimer !== null) clearInterval(statusTimer);
  if (countdownTimer !== null) clearInterval(countdownTimer);
  window.removeEventListener("profit-trade:dashboard-status", handleDashboardStatus);
});
</script>

<template>
  <div class="profit-trade-workspace">
    <header class="profit-workspace-bar">
      <div class="profit-workspace-brand">
        <span class="profit-workspace-mark"><FolioIcon name="scan" :size="18" /></span>
        <div>
          <strong>Profit Trade</strong>
          <small>搬砖做 T 运营台</small>
        </div>
      </div>

      <nav class="profit-subnav" aria-label="Profit Trade 页面">
        <RouterLink to="/profit-trade/overview">总览</RouterLink>
        <RouterLink to="/profit-trade/interruptions">中断追踪</RouterLink>
        <RouterLink to="/profit-trade/logs">实时日志</RouterLink>
      </nav>

      <div class="profit-runtime-strip" aria-label="Profit Trade 运行状态">
        <span :class="['runtime-dot', apiOnline === true ? 'online' : apiOnline === false ? 'offline' : 'unknown']">
          API {{ apiOnline === true ? "在线" : apiOnline === false ? "离线" : "检查中" }}
        </span>
        <span :class="['runtime-dot', realExecutionAllowed ? 'danger' : 'safe']">
          真实执行 {{ realExecutionAllowed ? "开放" : "关闭" }}
        </span>
        <span :class="['runtime-dot', profitRuntime.enabled ? 'online' : 'unknown']">
          后端 Worker {{ profitRuntime.preparing ? "准备中" : profitRuntime.enabled ? "运行中" : "已关闭" }}
        </span>
        <button class="runtime-cookie-button" type="button" @click="cookieExpanded = !cookieExpanded">
          <FolioIcon name="shield" :size="14" />
          Cookie {{ runtimeCookies.validCount ?? "—" }}/{{ runtimeCookies.totalCount ?? "—" }}
        </button>
      </div>
    </header>

    <section v-if="cookieExpanded" class="profit-cookie-panel">
      <header><div><strong>共享 Steam Cookie 健康</strong><span>与挂刀执行器共用同一门禁与刷新批次</span></div><RouterLink to="/guadao/overview">前往挂刀运行总览</RouterLink></header>
      <div v-if="runtimeCookies.accounts?.length" class="profit-cookie-grid">
        <article v-for="account in runtimeCookies.accounts" :key="account.accountId || account.steamId">
          <div><strong>{{ account.accountName || account.name || "未命名账号" }}</strong><span>{{ account.steamId || "—" }}</span></div>
          <b :class="{ valid: account.valid }">{{ account.valid ? "有效" : account.error || account.status || "未知" }}</b>
          <small :class="['cookie-currency', account.currencyStatus === 'cny' ? 'currency-cny' : account.currencyStatus === 'non_cny' ? 'currency-invalid' : 'currency-unknown']">{{ cookieCurrencyLabel(account) }}</small>
          <time>Cookie {{ localTime(account.lastCheckedAt) }} · 币种 {{ localTime(account.currencyCheckedAt) }}</time>
          <em v-if="account.currencyError">币种检测失败：{{ account.currencyError }}</em>
        </article>
      </div>
      <p v-else>共享 Cookie API 暂未返回账号状态。</p>
    </section>

    <p v-if="error" class="profit-layout-error">状态检查失败：{{ error }}</p>

    <section
      v-if="circuitVisible"
      class="listings-circuit-banner"
      aria-live="polite"
    >
      <div class="circuit-icon"><FolioIcon name="clock" :size="18" /></div>
      <div class="circuit-main">
        <strong>Steam listings 查询冷却中</strong>
        <span>指定卖单查询暂停；符合条件的机会会重新校验行情并改走安全求购，ROI、orderbook、C5 同步和收益结算继续运行。</span>
      </div>
      <dl>
        <div><dt>触发账号</dt><dd>{{ listingsCircuit.triggerAccountName || listingsCircuit.triggerAccountId || "-" }}</dd></div>
        <div><dt>连续 429</dt><dd>{{ listingsCircuit.consecutive429Count || 0 }} 次</dd></div>
        <div><dt>剩余时间</dt><dd>{{ circuitRemainingLabel }}</dd></div>
        <div><dt>冷却结束</dt><dd>{{ localTime(listingsCircuit.cooldownUntil) }}</dd></div>
      </dl>
    </section>

    <RouterView v-slot="{ Component }">
      <KeepAlive>
        <component :is="Component" />
      </KeepAlive>
    </RouterView>
  </div>
</template>

<style scoped>
.profit-trade-workspace{min-width:1120px;background:#f4f6f2;min-height:calc(100vh - 57px)}
.profit-workspace-bar{position:sticky;top:57px;z-index:9;display:grid;grid-template-columns:auto auto 1fr;gap:24px;align-items:center;padding:11px max(20px,calc((100vw - 1280px)/2));border-bottom:1px solid #dde3dc;background:rgba(250,251,248,.97);box-shadow:0 5px 18px rgba(28,57,43,.045);backdrop-filter:blur(10px)}
.profit-workspace-brand{display:flex;gap:10px;align-items:center;color:#17201c;white-space:nowrap}.profit-workspace-brand>div{display:grid}.profit-workspace-brand small{color:#6f7872;font-size:11px}.profit-workspace-mark{display:grid;place-items:center;width:34px;height:34px;border-radius:8px;color:#fff;background:#236a4c}
.profit-subnav{display:flex;gap:4px;padding:3px;border-radius:9px;background:#edf1ec}.profit-subnav a{padding:7px 13px;border-radius:7px;color:#627068;text-decoration:none;font-size:13px;font-weight:650}.profit-subnav a.router-link-active{color:#174a36;background:#fff;box-shadow:0 1px 5px rgba(20,59,46,.09)}
.profit-runtime-strip{display:flex;justify-content:flex-end;align-items:center;gap:8px;min-width:0}.runtime-dot{display:inline-flex;align-items:center;gap:6px;padding:5px 8px;border:1px solid #dfe5df;border-radius:999px;color:#66716b;background:#fff;font-size:11px;white-space:nowrap}.runtime-dot::before{content:"";width:6px;height:6px;border-radius:50%;background:#a3aaa6}.runtime-dot.online::before{background:#2f805b}.runtime-dot.offline::before,.runtime-dot.danger::before{background:#b64c42}.runtime-dot.safe::before{background:#2f805b}
.profit-layout-error{width:min(1280px,calc(100vw - 40px));margin:10px auto 0;padding:8px 11px;border:1px solid #e4b4ae;border-radius:7px;color:#8c382f;background:#fff7f5;font-size:12px}
.runtime-cookie-button{display:inline-flex;align-items:center;gap:5px;min-height:28px;border:1px solid #dce4de;border-radius:999px;padding:4px 8px;color:#335b48;background:#fff;font-size:10px;font-weight:700}.profit-cookie-panel{width:min(1280px,calc(100vw - 40px));margin:10px auto 0;border:1px solid var(--folio-line);border-radius:13px;padding:12px 14px;background:#fff;box-shadow:var(--folio-shadow)}.profit-cookie-panel header{display:flex;justify-content:space-between;align-items:center}.profit-cookie-panel header>div{display:grid;gap:2px}.profit-cookie-panel header span,.profit-cookie-panel>p{color:var(--folio-muted);font-size:9px}.profit-cookie-panel header a{color:var(--folio-green);font-size:9px;font-weight:700}.profit-cookie-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:7px;margin-top:9px}.profit-cookie-grid article{display:grid;grid-template-columns:1fr auto;gap:4px;border:1px solid var(--folio-line);border-radius:9px;padding:8px;background:var(--folio-surface-soft)}.profit-cookie-grid article>div{display:grid;gap:2px}.profit-cookie-grid strong{font-size:9px}.profit-cookie-grid span,.profit-cookie-grid time{color:var(--folio-muted);font-size:7px}.profit-cookie-grid b{color:var(--folio-amber);font-size:8px}.profit-cookie-grid b.valid{color:var(--folio-green)}.profit-cookie-grid .cookie-currency{grid-column:1/-1;font-size:8px;font-weight:700}.profit-cookie-grid .currency-cny{color:var(--folio-green)}.profit-cookie-grid .currency-invalid{color:#b64c42}.profit-cookie-grid .currency-unknown{color:var(--folio-muted)}.profit-cookie-grid time,.profit-cookie-grid em{grid-column:1/-1}.profit-cookie-grid time{font-style:normal}.profit-cookie-grid em{color:#b64c42;font-size:7px;font-style:normal}
.listings-circuit-banner{width:min(1280px,calc(100vw - 40px));margin:12px auto 0;display:grid;grid-template-columns:auto minmax(280px,1fr) auto;gap:12px;align-items:center;padding:12px 14px;border:1px solid #dfc77e;border-radius:9px;color:#4f4524;background:#fff9e9;box-shadow:0 6px 18px rgba(79,69,36,.06)}.circuit-icon{display:grid;place-items:center;width:34px;height:34px;border-radius:8px;color:#7c6423;background:#f5e8b9}.circuit-main{display:grid;gap:3px}.circuit-main strong{font-size:13px}.circuit-main span{color:#746842;font-size:11px}.listings-circuit-banner dl{display:grid;grid-template-columns:repeat(4,auto);gap:8px 14px;margin:0}.listings-circuit-banner dl>div{display:grid}.listings-circuit-banner dt{color:#8a7d53;font-size:9px}.listings-circuit-banner dd{margin:2px 0 0;font-size:10px;font-weight:700;white-space:nowrap}.listings-circuit-banner.recovered{border-color:#b8d7c3;color:#205b42;background:#edf7f1}.listings-circuit-banner.recovered .circuit-icon{color:#236a4c;background:#dcefe3}.listings-circuit-banner.recovered .circuit-main span,.listings-circuit-banner.recovered dt{color:#61776b}
</style>
