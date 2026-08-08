<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import FolioIcon from "../components/FolioIcon.vue";
import OperationVisualAtom from "../components/OperationVisualAtom.vue";

type SteamBalanceStatus = "ok" | "error" | "skipped";

type SteamBalanceRow = {
  id: string;
  account: string;
  steamId: string | null;
  realBalance: number | null;
  pendingBalance: number | null;
  totalBalance: number | null;
  currency: string | null;
  currencyId: number | null;
  status: SteamBalanceStatus;
  error: string | null;
  stale: boolean;
};

type SteamBalanceResponse = {
  ok: boolean;
  accounts?: SteamBalanceRow[];
  hasSnapshot?: boolean;
  updatedAt?: string | null;
  source?: "cache" | "live";
  error?: string;
};

type CurrencySummary = {
  currency: string;
  currencyId: number | null;
  accountCount: number;
  realBalance: number;
  pendingBalance: number;
  totalBalance: number;
};

const rows = ref<SteamBalanceRow[]>([]);
const loading = ref(false);
const loadError = ref("");
const hasRead = ref(false);
const loadedAt = ref("");
const dataSource = ref<"cache" | "live" | "">("");

const accountCount = computed(() => rows.value.length);
const successfulCount = computed(() => rows.value.filter((row) => row.status === "ok").length);
const currencySummaries = computed<CurrencySummary[]>(() => {
  const groups = new Map<string, CurrencySummary>();
  for (const row of rows.value) {
    if (!row.currency) continue;
    const group = groups.get(row.currency) || {
      currency: row.currency,
      currencyId: row.currencyId,
      accountCount: 0,
      realBalance: 0,
      pendingBalance: 0,
      totalBalance: 0,
    };
    group.accountCount += 1;
    group.realBalance += Number(row.realBalance || 0);
    group.pendingBalance += Number(row.pendingBalance || 0);
    group.totalBalance = group.realBalance + group.pendingBalance;
    groups.set(row.currency, group);
  }
  return [...groups.values()].sort((left, right) => left.currency.localeCompare(right.currency));
});

function formatMoney(value: number | null): string {
  return value === null ? "--" : value.toFixed(2);
}

function rowCurrency(row: SteamBalanceRow): string {
  return row.currency || "--";
}

function applyPayload(payload: SteamBalanceResponse): void {
  rows.value = payload.accounts || [];
  hasRead.value = Boolean(payload.hasSnapshot);
  loadedAt.value = payload.updatedAt
    ? new Date(payload.updatedAt).toLocaleString("zh-CN", { hour12: false })
    : "";
  dataSource.value = payload.source || "";
}

async function loadSavedBalances(): Promise<void> {
  try {
    const response = await fetch("/api/steam-balances", { cache: "no-store" });
    const payload = await response.json() as SteamBalanceResponse;
    if (!response.ok || !payload.ok) return;
    applyPayload(payload);
  } catch {
    // The page remains usable when the API is offline; refresh will show the error.
  }
}

async function readBalances(): Promise<void> {
  loading.value = true;
  loadError.value = "";
  try {
    const response = await fetch("/api/steam-balances/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    const payload = await response.json() as SteamBalanceResponse;
    if (!response.ok || !payload.ok) {
      throw new Error(payload.error || `读取失败（HTTP ${response.status}）`);
    }
    applyPayload(payload);
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : String(error);
  } finally {
    loading.value = false;
  }
}

onMounted(loadSavedBalances);
</script>

<template>
  <main class="page steam-balance-page steam-balance-page--folio-refresh steam-balance-page--minimal-v2">
    <header class="page-header">
      <div class="page-title-cluster">
        <OperationVisualAtom name="steam-balance" :size="68" />
        <div>
          <p class="eyebrow">Steam Balances</p>
          <h1>Steam 余额统计</h1>
          <span>五个交易账号的实时钱包、待入账和读取状态</span>
        </div>
      </div>
      <button class="primary-button read-button" type="button" :disabled="loading" @click="readBalances">
        <FolioIcon name="refresh" :class="{ spinning: loading }" />
        {{ loading ? "正在读取" : "读取余额" }}
      </button>
    </header>

    <p v-if="loadError" class="balance-message error-message">
      <FolioIcon name="error" />
      {{ loadError }}
    </p>
    <p v-else-if="loadedAt" class="balance-message success-message">
      <FolioIcon name="success" />
      {{ dataSource === "cache" ? "上次读取" : "本次读取" }} {{ successfulCount }}/{{ accountCount }} 个 Cookie 账号，更新时间 {{ loadedAt }}
    </p>

    <section class="metrics-grid">
      <article class="metric-card">
        <OperationVisualAtom name="accounts" :size="46" />
        <div class="metric-copy">
          <span>Cookie 账号数</span>
          <strong>{{ hasRead ? accountCount : "--" }}</strong>
        </div>
      </article>
      <article class="metric-card">
        <OperationVisualAtom name="steam-balance" :size="46" />
        <div class="metric-copy">
          <span>Steam 可用余额</span>
          <strong v-if="!hasRead">--</strong>
          <div v-else class="currency-totals">
            <strong v-for="group in currencySummaries" :key="group.currency">
              {{ group.currency }} {{ formatMoney(group.realBalance) }}
            </strong>
          </div>
        </div>
      </article>
      <article class="metric-card">
        <OperationVisualAtom name="pending-wallet" :size="46" />
        <div class="metric-copy">
          <span>入账冻结 / 待入账</span>
          <strong v-if="!hasRead">--</strong>
          <div v-else class="currency-totals">
            <strong v-for="group in currencySummaries" :key="group.currency">
              {{ group.currency }} {{ formatMoney(group.pendingBalance) }}
            </strong>
          </div>
        </div>
      </article>
      <article class="metric-card">
        <OperationVisualAtom name="total-database" :size="46" />
        <div class="metric-copy">
          <span>Steam 总余额</span>
          <strong v-if="!hasRead">--</strong>
          <div v-else class="currency-totals">
            <strong v-for="group in currencySummaries" :key="group.currency">
              {{ group.currency }} {{ formatMoney(group.totalBalance) }}
            </strong>
          </div>
        </div>
      </article>
    </section>

    <section class="panel">
      <div v-if="!hasRead" class="balance-empty">
        <OperationVisualAtom name="steam-balance" :size="72" />
        <strong>尚未读取 Steam 余额</strong>
        <span>点击右上角“读取余额”，从当前已导入 Cookie 的账号获取实时钱包信息。</span>
      </div>
      <div v-else class="table-wrap">
        <table class="data-table steam-balance-table">
          <thead>
            <tr>
              <th>账号</th>
              <th>Steam ID</th>
              <th>可用余额</th>
              <th>入账冻结 / 待入账</th>
              <th>总余额</th>
              <th>读取状态</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in rows" :key="row.id">
              <td class="account-name">{{ row.account }}</td>
              <td class="mono">{{ row.steamId || "--" }}</td>
              <td class="amount">{{ rowCurrency(row) }} {{ formatMoney(row.realBalance) }}</td>
              <td class="amount">{{ rowCurrency(row) }} {{ formatMoney(row.pendingBalance) }}</td>
              <td class="amount">{{ rowCurrency(row) }} {{ formatMoney(row.totalBalance) }}</td>
              <td>
                <span class="read-status" :class="row.status">
                  <FolioIcon :name="row.status === 'ok' ? 'success' : row.status === 'error' ? 'error' : 'warning'" :size="15" />
                  {{ row.stale ? "上次数据" : row.status === "ok" ? "读取成功" : row.status === "error" ? "读取失败" : "已跳过" }}
                </span>
                <p v-if="row.error" class="row-error">{{ row.error }}</p>
              </td>
            </tr>
          </tbody>
          <tfoot>
            <tr>
              <td colspan="2">合计</td>
              <td><span v-for="group in currencySummaries" :key="group.currency" class="footer-total">{{ group.currency }} {{ formatMoney(group.realBalance) }}</span></td>
              <td><span v-for="group in currencySummaries" :key="group.currency" class="footer-total">{{ group.currency }} {{ formatMoney(group.pendingBalance) }}</span></td>
              <td><span v-for="group in currencySummaries" :key="group.currency" class="footer-total">{{ group.currency }} {{ formatMoney(group.totalBalance) }}</span></td>
              <td>{{ successfulCount }}/{{ accountCount }} 成功</td>
            </tr>
          </tfoot>
        </table>
      </div>
    </section>
  </main>
</template>

<style scoped>
.read-button { display: inline-flex; align-items: center; gap: 8px; }
.balance-message { margin: 0; display: flex; align-items: center; gap: 8px; font-size: 13px; font-weight: 600; }
.success-message { color: #236a4c; }
.error-message { color: #9b443d; }
.balance-empty { min-height: 220px; display: grid; place-items: center; align-content: center; gap: 8px; color: #6f7872; text-align: center; }
.balance-empty strong { color: #17201c; font-size: 16px; }
.account-name { color: #17201c; font-weight: 700; }
.currency-totals { display: grid; gap: 3px; }
.footer-total { display: block; }
.steam-balance-table { min-width: 980px; }
.read-status { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; font-weight: 700; }
.read-status.ok { color: #236a4c; }
.read-status.error { color: #9b443d; }
.read-status.skipped { color: #9a6a1f; }
.row-error { max-width: 280px; margin: 4px 0 0; color: #8a5149; font-size: 12px; line-height: 1.4; white-space: normal; }
.spinning { animation: spin 0.9s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

/* 2026 Folio refresh: display-only treatment.  Balance reads and all API
   interactions above intentionally keep their existing behavior. */
.steam-balance-page--folio-refresh { gap: 16px; }

.steam-balance-page--folio-refresh .page-header {
  min-height: 176px;
  padding: 28px 30px;
  border: 1px solid rgba(255, 255, 255, .1);
  border-radius: 22px;
  color: #fff;
  background:
    radial-gradient(circle at 83% 8%, rgba(174, 223, 186, .2), transparent 28%),
    linear-gradient(132deg, #123b2d 0%, #184d39 64%, #2b7253 100%);
  box-shadow: 0 22px 54px rgba(20, 59, 46, .16);
}

.steam-balance-page--folio-refresh .page-header h1 { color: #fff; font-size: clamp(28px, 3vw, 36px); }
.steam-balance-page--folio-refresh .page-header .eyebrow { color: #c7e7cf; letter-spacing: .12em; }
.steam-balance-page--folio-refresh .read-button {
  min-height: 44px;
  padding-inline: 17px;
  border-color: rgba(255, 255, 255, .9);
  color: var(--folio-green-dark);
  background: #fff;
  box-shadow: 0 10px 22px rgba(8, 34, 24, .2);
}
.steam-balance-page--folio-refresh .read-button:hover:not(:disabled) {
  color: #fff;
  background: var(--folio-green-deep);
  transform: translateY(-1px);
}

.steam-balance-page--folio-refresh .balance-message {
  min-height: 42px;
  margin: -3px 2px 0;
  padding: 10px 12px;
  border: 1px solid transparent;
  border-radius: 11px;
  background: var(--folio-surface-soft);
}
.steam-balance-page--folio-refresh .success-message { border-color: #cee2d4; color: var(--folio-green); background: var(--folio-green-soft); }
.steam-balance-page--folio-refresh .error-message { border-color: #ebceca; color: var(--folio-red); background: var(--folio-red-soft); }

.steam-balance-page--folio-refresh .metrics-grid { gap: 12px; }
.steam-balance-page--folio-refresh .metric-card {
  min-height: 126px;
  padding: 17px;
  border-color: var(--folio-line);
  border-radius: 16px;
  background: linear-gradient(145deg, #fff, #f8faf8);
  box-shadow: var(--folio-shadow);
}
.steam-balance-page--folio-refresh .metric-card:first-child { background: linear-gradient(145deg, #f2f8f4, #fff); }
.steam-balance-page--folio-refresh .metric-card span { color: var(--folio-muted); }
.steam-balance-page--folio-refresh .metric-card strong { font-size: 25px; }
.steam-balance-page--folio-refresh .currency-totals { gap: 5px; }
.steam-balance-page--folio-refresh .currency-totals strong { font-size: 18px; letter-spacing: -.025em; }

.steam-balance-page--folio-refresh .panel {
  padding: 8px;
  border-radius: 18px;
  border-color: var(--folio-line);
  box-shadow: var(--folio-shadow);
}
.steam-balance-page--folio-refresh .balance-empty { min-height: 290px; padding: 30px; }
.steam-balance-page--folio-refresh .balance-empty > svg {
  width: 42px;
  height: 42px;
  padding: 10px;
  border-radius: 14px;
  color: var(--folio-green);
  background: var(--folio-green-soft);
}
.steam-balance-page--folio-refresh .balance-empty span { max-width: 440px; line-height: 1.7; }
.steam-balance-page--folio-refresh .table-wrap { border-radius: 13px; }
.steam-balance-page--folio-refresh .data-table th {
  padding-top: 12px;
  padding-bottom: 12px;
  background: #f5f8f5;
}
.steam-balance-page--folio-refresh .data-table td { padding-top: 12px; padding-bottom: 12px; }
.steam-balance-page--folio-refresh .data-table tbody tr:hover { background: #f7fbf8; }
.steam-balance-page--folio-refresh .data-table tfoot td { background: #f1f7f3; }
.steam-balance-page--folio-refresh .account-name { color: var(--folio-green-deep); }
.steam-balance-page--folio-refresh .read-status { padding: 5px 8px; border-radius: 999px; }
.steam-balance-page--folio-refresh .read-status.ok { background: var(--folio-green-soft); }
.steam-balance-page--folio-refresh .read-status.error { background: var(--folio-red-soft); }
.steam-balance-page--folio-refresh .read-status.skipped { background: var(--folio-amber-soft); }

@media (max-width: 720px) {
  .steam-balance-page--folio-refresh .page-header { min-height: 0; padding: 22px 18px; border-radius: 18px; }
  .steam-balance-page--folio-refresh .read-button { width: 100%; justify-content: center; }
  .steam-balance-page--folio-refresh .metric-card { min-height: 110px; }
  .steam-balance-page--folio-refresh .panel { padding: 5px; border-radius: 15px; }
}

/* Preview-aligned green operations chrome plus cropped Krill wallet artwork. */
.steam-balance-page--minimal-v2 .page-header {
  min-height: 148px;
  padding: 20px 24px;
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 20px;
  overflow: hidden;
  color: var(--minimal-ink);
  border-color: #d7e7dc;
  border-radius: var(--ops-radius-lg);
  background:
    radial-gradient(circle at 88% 8%, rgba(92, 180, 116, .12), transparent 26%),
    linear-gradient(135deg, #f7fbf8, #ffffff 58%, #edf7f0);
  box-shadow: var(--ops-shadow-medium);
}
.steam-balance-page--minimal-v2 .page-header h1 { color: var(--minimal-ink); }
.steam-balance-page--minimal-v2 .page-header .eyebrow { color: var(--minimal-success); }
.steam-balance-page--minimal-v2 .page-title-cluster {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 16px;
}
.steam-balance-page--minimal-v2 .page-title-cluster > .operation-visual-atom {
  border-radius: var(--ops-radius-lg);
  box-shadow: var(--ops-shadow-shallow);
}
.steam-balance-page--minimal-v2 .page-title-cluster span {
  display: block;
  margin-top: 6px;
  color: var(--minimal-muted);
  font-size: 12px;
}
.steam-balance-page--minimal-v2 .read-button {
  min-height: 43px;
  border-radius: var(--ops-radius-sm);
  color: #fff;
  border-color: var(--ops-primary);
  background: var(--ops-primary);
  box-shadow: var(--ops-shadow-shallow);
}
.steam-balance-page--minimal-v2 .read-button:hover:not(:disabled) { color: #fff; background: var(--ops-primary-hover); }
.steam-balance-page--minimal-v2 .balance-message {
  border-color: var(--minimal-line);
  border-radius: var(--ops-radius-md);
  background: var(--minimal-surface);
  box-shadow: var(--ops-shadow-shallow);
}
.steam-balance-page--minimal-v2 .success-message { color: var(--minimal-success); border-color: #d2e9dc; background: var(--minimal-success-soft); }
.steam-balance-page--minimal-v2 .error-message { color: var(--minimal-danger); border-color: #edcfcd; background: var(--minimal-danger-soft); }
.steam-balance-page--minimal-v2 .metric-card {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  align-content: center;
  align-items: center;
  gap: 13px;
  border-radius: var(--ops-radius-md);
  border-color: var(--minimal-line);
  background: linear-gradient(145deg, #fff, #fbfdfb);
  box-shadow: var(--ops-shadow-shallow);
}
.steam-balance-page--minimal-v2 .metric-copy { min-width: 0; display: grid; gap: 5px; }
.steam-balance-page--minimal-v2 .metric-card:first-child { background: linear-gradient(145deg, #f1f8f3, #fff); }
.steam-balance-page--minimal-v2 .metric-card:hover {
  border-color: #b8d7c2;
  box-shadow: var(--ops-shadow-medium);
  transform: translateY(-1px);
}
.steam-balance-page--minimal-v2 .metric-card span { color: var(--minimal-muted); }
.steam-balance-page--minimal-v2 .metric-card strong { color: var(--minimal-ink); }
.steam-balance-page--minimal-v2 .panel {
  border-radius: var(--ops-radius-lg);
  border-color: var(--minimal-line);
  background: var(--minimal-surface);
  box-shadow: var(--ops-shadow-medium);
}
.steam-balance-page--minimal-v2 .balance-empty { color: var(--minimal-muted); }
.steam-balance-page--minimal-v2 .balance-empty strong { color: var(--minimal-ink); }
.steam-balance-page--minimal-v2 .balance-empty > svg {
  border-radius: var(--ops-radius-lg);
  color: var(--minimal-success);
  background: var(--minimal-success-soft);
  box-shadow: var(--ops-shadow-shallow);
}
.steam-balance-page--minimal-v2 .data-table th { color: #506057; background: var(--minimal-surface-soft); }
.steam-balance-page--minimal-v2 .data-table th,
.steam-balance-page--minimal-v2 .data-table td { border-color: #eceeef; }
.steam-balance-page--minimal-v2 .data-table tbody tr:hover { background: #f5faf6; }
.steam-balance-page--minimal-v2 .data-table tfoot td { background: #edf6ef; }
.steam-balance-page--minimal-v2 .account-name { color: #145f38; }
.steam-balance-page--minimal-v2 .read-status {
  border: 1px solid transparent;
  border-radius: 999px;
}
.steam-balance-page--minimal-v2 .read-status.ok {
  color: var(--ops-success);
  border-color: #cce6d4;
  background: var(--ops-success-soft);
}
.steam-balance-page--minimal-v2 .read-status.error {
  color: var(--ops-error);
  border-color: #efcfcc;
  background: var(--ops-error-soft);
}
.steam-balance-page--minimal-v2 .read-status.skipped {
  color: var(--ops-warning);
  border-color: #f0dbad;
  background: var(--ops-warning-soft);
}

@media (max-width: 900px) {
  .steam-balance-page--minimal-v2 .page-header { grid-template-columns: minmax(0, 1fr) auto; }
}
@media (max-width: 720px) {
  .steam-balance-page--minimal-v2 .page-header { padding: 20px 16px; grid-template-columns: 1fr; }
  .steam-balance-page--minimal-v2 .page-title-cluster > .operation-visual-atom { width: 56px; height: 56px; }
}
</style>
