<script setup lang="ts">
import { computed, ref } from "vue";
import AccountProfitPage from "./pages/account_profit.vue";
import SteamBalancesPage from "./pages/steam_balances.vue";
import GuadaoReportPage from "./pages/guadao_report.vue";
import CaseRatioMonitorPage from "./pages/case_ratio_monitor.vue";
import ProfitTradePage from "./pages/profit_trade.vue";

const pages = [
  { key: "account", label: "挂刀余额核对", component: AccountProfitPage },
  { key: "steam", label: "Steam余额统计", component: SteamBalancesPage },
  { key: "guadao", label: "挂刀报表", component: GuadaoReportPage },
  { key: "case-ratio", label: "箱子挂刀比", component: CaseRatioMonitorPage },
  { key: "profit-trade", label: "搬砖做T", component: ProfitTradePage },
] as const;

type PageKey = (typeof pages)[number]["key"];

const activePage = ref<PageKey>("profit-trade");
const activeComponent = computed(
  () => pages.find((page) => page.key === activePage.value)?.component ?? AccountProfitPage,
);
</script>

<template>
  <div class="app-shell">
    <nav class="top-nav" aria-label="Primary">
      <button
        v-for="page in pages"
        :key="page.key"
        type="button"
        class="nav-tab"
        :class="{ active: activePage === page.key }"
        @click="activePage = page.key"
      >
        {{ page.label }}
      </button>
    </nav>

    <component :is="activeComponent" />
  </div>
</template>
