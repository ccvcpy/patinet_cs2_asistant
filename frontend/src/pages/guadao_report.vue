<script setup lang="ts">
import { computed, ref } from "vue";

type ReportRow = {
  name: string;
  count: number;
  steamGross: number;
  steamNet: number;
  c5Cash: number;
};

const dateFrom = ref("2026-06-01T00:00");
const dateTo = ref("2026-06-03T23:00");
const itemName = ref("");
const detail = ref(false);

const rows = ref<ReportRow[]>([
  { name: "Kilowatt Case", count: 201, steamGross: 477.1, steamNet: 382.6, c5Cash: 250.04 },
  { name: "Revolution Case", count: 66, steamGross: 198.4, steamNet: 172.18, c5Cash: 111.81 },
]);

const filteredRows = computed(() => {
  const nameFilter = itemName.value.trim().toLowerCase();
  if (!nameFilter) return rows.value;

  return rows.value.filter((row) => row.name.toLowerCase().includes(nameFilter));
});

const closedCount = computed(() => filteredRows.value.reduce((sum, row) => sum + row.count, 0));
const steamGrossTotal = computed(() => filteredRows.value.reduce((sum, row) => sum + row.steamGross, 0));
const steamNetTotal = computed(() => filteredRows.value.reduce((sum, row) => sum + row.steamNet, 0));
const c5CashTotal = computed(() => filteredRows.value.reduce((sum, row) => sum + row.c5Cash, 0));
const totalDiscount = computed(() => (steamNetTotal.value > 0 ? c5CashTotal.value / steamNetTotal.value : 0));
const faceDiscount = computed(() => (steamGrossTotal.value > 0 ? c5CashTotal.value / steamGrossTotal.value : 0));
const commandPreview = computed(() => {
  const parts = [
    "python main.py pool guadao-report",
    `--from ${dateFrom.value.replace("T", "T")}`,
    `--to ${dateTo.value.replace("T", "T")}`,
  ];
  if (itemName.value.trim()) parts.push(`--item "${itemName.value.trim()}"`);
  if (detail.value) parts.push("--detail");
  return parts.join(" ");
});

function formatMoney(value: number): string {
  return value.toFixed(2);
}

function formatPct(value: number): string {
  return `${(value * 100).toFixed(2)}%`;
}

function refreshPreview(): void {
  rows.value = [...rows.value].sort((a, b) => b.c5Cash - a.c5Cash);
}
</script>

<template>
  <main class="page">
    <header class="page-header">
      <div>
        <p class="eyebrow">Guadao Report</p>
        <h1>挂刀余额折扣报表</h1>
      </div>
      <button class="primary-button" type="button" @click="refreshPreview">生成预览</button>
    </header>

    <section class="panel">
      <form class="report-controls" @submit.prevent="refreshPreview">
        <label>
          From
          <input v-model="dateFrom" type="datetime-local" />
        </label>
        <label>
          To
          <input v-model="dateTo" type="datetime-local" />
        </label>
        <label>
          Item
          <input v-model="itemName" type="text" placeholder="market hash name" />
        </label>
        <label class="checkbox-row">
          <input v-model="detail" type="checkbox" />
          Detail
        </label>
      </form>
      <code class="command-preview">{{ commandPreview }}</code>
    </section>

    <section class="metrics-grid">
      <article class="metric-card">
        <span>闭环笔数</span>
        <strong>{{ closedCount }}</strong>
      </article>
      <article class="metric-card">
        <span>Steam 面值</span>
        <strong>CNY {{ formatMoney(steamGrossTotal) }}</strong>
      </article>
      <article class="metric-card">
        <span>Steam 到手</span>
        <strong>CNY {{ formatMoney(steamNetTotal) }}</strong>
      </article>
      <article class="metric-card">
        <span>C5 现金</span>
        <strong>CNY {{ formatMoney(c5CashTotal) }}</strong>
      </article>
      <article class="metric-card">
        <span>总折比</span>
        <strong>{{ formatPct(totalDiscount) }}</strong>
      </article>
      <article class="metric-card">
        <span>面值折比</span>
        <strong>{{ formatPct(faceDiscount) }}</strong>
      </article>
    </section>

    <section class="panel">
      <div class="table-wrap">
        <table class="data-table">
          <thead>
            <tr>
              <th>饰品</th>
              <th>笔数</th>
              <th>Steam 面值</th>
              <th>Steam 到手</th>
              <th>C5 现金</th>
              <th>总折比</th>
              <th>面值折比</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in filteredRows" :key="row.name">
              <td>{{ row.name }}</td>
              <td>{{ row.count }}</td>
              <td>CNY {{ formatMoney(row.steamGross) }}</td>
              <td>CNY {{ formatMoney(row.steamNet) }}</td>
              <td>CNY {{ formatMoney(row.c5Cash) }}</td>
              <td>{{ formatPct(row.c5Cash / row.steamNet) }}</td>
              <td>{{ formatPct(row.c5Cash / row.steamGross) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </main>
</template>
