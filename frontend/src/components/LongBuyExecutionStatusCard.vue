<script setup lang="ts">
import { computed } from "vue";
import FolioIcon from "./FolioIcon.vue";
import type { ProfitTradeLongBuyStrategyState } from "./profit_trade_long_buy_strategy";

const props = defineProps<{
  state: ProfitTradeLongBuyStrategyState;
}>();

const actionRows = computed(() => [
  { label: "扫描与安全价计算", allowed: props.state.canObserve },
  { label: "核对已有 Steam 求购成交", allowed: props.state.canReconcileExistingOrders },
  { label: "新建、撤销或改价 Steam 求购", allowed: props.state.canWriteSteam },
]);

const statusIcon = computed(() => {
  if (props.state.mode === "live") return "success";
  if (props.state.mode === "observe") return "warning";
  return "error";
});
</script>

<template>
  <article
    :class="['long-buy-status-card', `is-${state.mode}`]"
    data-testid="long-buy-execution-status"
  >
    <span class="component-kicker">当前有效状态</span>
    <div class="status-heading">
      <span class="status-icon"><FolioIcon :name="statusIcon" :size="20" /></span>
      <div>
        <strong data-testid="long-buy-mode">长期求购：{{ state.label }}</strong>
        <small>{{ state.detail }}</small>
      </div>
    </div>

    <ul class="action-list">
      <li v-for="row in actionRows" :key="row.label" :class="{ allowed: row.allowed }">
        <FolioIcon :name="row.allowed ? 'success' : 'error'" :size="16" />
        <span>{{ row.label }}</span>
        <b>{{ row.allowed ? "允许" : "禁止" }}</b>
      </li>
    </ul>
  </article>
</template>

<style scoped>
.long-buy-status-card{display:grid;gap:13px;min-height:196px;padding:16px;border:1px solid var(--folio-line,#dce5df);border-radius:16px;background:#fff;box-shadow:var(--folio-shadow,0 8px 24px rgba(35,55,42,.06))}.long-buy-status-card.is-observe{border-color:#e7d9bb;background:linear-gradient(180deg,#fffdfa 0%,#fff8e8 100%)}.long-buy-status-card.is-live{border-color:#bfdac8;background:linear-gradient(180deg,#fbfefc 0%,#f0f8f3 100%)}.long-buy-status-card.is-disabled{border-color:#e4d6d3;background:#fff8f7}.component-kicker{color:var(--folio-muted,#718078);font-size:10px;font-weight:750;letter-spacing:.08em}.status-heading{display:flex;gap:10px;align-items:flex-start}.status-icon{display:grid;place-items:center;width:36px;height:36px;border-radius:50%;color:#976816;background:#fff1ce}.is-live .status-icon{color:var(--folio-green,#236a4c);background:#e1f0e6}.is-disabled .status-icon{color:var(--folio-red,#9b493e);background:#fae8e6}.status-heading>div{display:grid;gap:4px}.status-heading strong{color:var(--folio-ink,#18231d);font-size:17px;line-height:1.2}.status-heading small{color:var(--folio-muted,#718078);font-size:11px;line-height:1.5}.action-list{display:grid;gap:7px;margin:0;padding:0;list-style:none}.action-list li{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:8px;align-items:center;color:#9b493e;font-size:12px}.action-list li.allowed{color:var(--folio-green,#236a4c)}.action-list li>span{color:var(--folio-ink,#18231d)}.action-list b{padding:3px 7px;border-radius:999px;color:#9b493e;background:#fae7e4;font-size:10px}.action-list .allowed b{color:#226447;background:#e3f1e7}
</style>
