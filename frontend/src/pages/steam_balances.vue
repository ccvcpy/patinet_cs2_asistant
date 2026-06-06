<script setup lang="ts">
import { computed, reactive } from "vue";

type SteamBalanceRow = {
  id: number;
  account: string;
  steamId: string;
  realBalance: number;
  pendingBalance: number;
};

const rows = reactive<SteamBalanceRow[]>([
  { id: 1, account: "xiaodigu11", steamId: "76561198279977505", realBalance: 0, pendingBalance: 0 },
  { id: 2, account: "vnuzl692", steamId: "76561199119018953", realBalance: 0, pendingBalance: 0 },
  { id: 3, account: "ropzx55x", steamId: "-", realBalance: 0, pendingBalance: 0 },
  { id: 4, account: "x6l1cg3cy5o", steamId: "-", realBalance: 0, pendingBalance: 0 },
]);

const accountCount = computed(() => rows.length);
const totalRealBalance = computed(() => rows.reduce((sum, row) => sum + Number(row.realBalance || 0), 0));
const totalPendingBalance = computed(() => rows.reduce((sum, row) => sum + Number(row.pendingBalance || 0), 0));
const totalSteamBalance = computed(() => totalRealBalance.value + totalPendingBalance.value);

function accountTotal(row: SteamBalanceRow): number {
  return Number(row.realBalance || 0) + Number(row.pendingBalance || 0);
}

function formatMoney(value: number): string {
  return value.toFixed(2);
}

function addRow(): void {
  rows.push({
    id: Date.now(),
    account: "",
    steamId: "",
    realBalance: 0,
    pendingBalance: 0,
  });
}

function removeRow(id: number): void {
  const index = rows.findIndex((row) => row.id === id);
  if (index >= 0) rows.splice(index, 1);
}
</script>

<template>
  <main class="page">
    <header class="page-header">
      <div>
        <p class="eyebrow">Steam Balances</p>
        <h1>Steam 余额统计</h1>
      </div>
      <button class="secondary-button" type="button" @click="addRow">新增账号</button>
    </header>

    <section class="metrics-grid">
      <article class="metric-card">
        <span>绑定 Steam 账号数</span>
        <strong>{{ accountCount }}</strong>
      </article>
      <article class="metric-card">
        <span>Steam 真实余额</span>
        <strong>CNY {{ formatMoney(totalRealBalance) }}</strong>
      </article>
      <article class="metric-card">
        <span>尚未处理 Steam 余额</span>
        <strong>CNY {{ formatMoney(totalPendingBalance) }}</strong>
      </article>
      <article class="metric-card">
        <span>Steam 总余额</span>
        <strong>CNY {{ formatMoney(totalSteamBalance) }}</strong>
      </article>
    </section>

    <section class="panel">
      <div class="table-wrap">
        <table class="data-table">
          <thead>
            <tr>
              <th>账号</th>
              <th>Steam ID</th>
              <th>Steam 真实余额</th>
              <th>尚未处理 Steam 余额</th>
              <th>Steam 总余额</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in rows" :key="row.id">
              <td>
                <input v-model="row.account" class="table-input" type="text" />
              </td>
              <td>
                <input v-model="row.steamId" class="table-input mono" type="text" />
              </td>
              <td>
                <input
                  v-model.number="row.realBalance"
                  class="table-input number"
                  type="number"
                  step="0.01"
                  inputmode="decimal"
                />
              </td>
              <td>
                <input
                  v-model.number="row.pendingBalance"
                  class="table-input number"
                  type="number"
                  step="0.01"
                  inputmode="decimal"
                />
              </td>
              <td class="amount">CNY {{ formatMoney(accountTotal(row)) }}</td>
              <td class="row-action">
                <button class="icon-button" type="button" @click="removeRow(row.id)">×</button>
              </td>
            </tr>
          </tbody>
          <tfoot>
            <tr>
              <td colspan="2">合计</td>
              <td>CNY {{ formatMoney(totalRealBalance) }}</td>
              <td>CNY {{ formatMoney(totalPendingBalance) }}</td>
              <td>CNY {{ formatMoney(totalSteamBalance) }}</td>
              <td></td>
            </tr>
          </tfoot>
        </table>
      </div>
    </section>
  </main>
</template>
