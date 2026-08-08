<script setup lang="ts">
import LongBuyActionBoundary from "./LongBuyActionBoundary.vue";
import LongBuyCrossedBookSafetyRule from "./LongBuyCrossedBookSafetyRule.vue";
import LongBuyExecutionStatusCard from "./LongBuyExecutionStatusCard.vue";
import LongBuyExecutionSwitchMatrix from "./LongBuyExecutionSwitchMatrix.vue";
import LongBuyLifecyclePreview from "./LongBuyLifecyclePreview.vue";
import LongBuyRiskHint from "./LongBuyRiskHint.vue";
import {
  resolveProfitTradeLongBuyStrategyState,
  type ProfitTradeLongBuyConfigKey,
  type ProfitTradeLongBuyStrategyConfig,
} from "./profit_trade_long_buy_strategy";
import { computed } from "vue";

const props = defineProps<{
  config: ProfitTradeLongBuyStrategyConfig;
  activeOrderCount?: number;
  updatingKey?: ProfitTradeLongBuyConfigKey | null;
}>();

const emit = defineEmits<{
  toggle: [key: ProfitTradeLongBuyConfigKey, nextEnabled: boolean];
}>();

const state = computed(() => resolveProfitTradeLongBuyStrategyState(props.config));
</script>

<template>
  <section class="long-buy-strategy-panel" aria-labelledby="long-buy-strategy-title">
    <header class="strategy-heading">
      <div>
        <p>STEAM LONG BUY STRATEGY</p>
        <h2 id="long-buy-strategy-title">长期 Steam 求购策略</h2>
        <span>先观察与核对成交；Steam 写入由独立许可控制。</span>
      </div>
      <span :class="['strategy-mode', `is-${state.mode}`]">{{ state.label }}</span>
    </header>

    <div class="strategy-top-grid">
      <LongBuyExecutionStatusCard :state="state" />
      <LongBuyExecutionSwitchMatrix
        :config="config"
        :state="state"
        :updating-key="updatingKey"
        @toggle="(key, nextEnabled) => emit('toggle', key, nextEnabled)"
      />
      <LongBuyActionBoundary :config="config" :state="state" />
    </div>

    <div class="strategy-bottom-grid">
      <LongBuyCrossedBookSafetyRule />
      <LongBuyLifecyclePreview :state="state" :active-order-count="activeOrderCount ?? 0" />
    </div>

    <LongBuyRiskHint :config="config" :state="state" />
  </section>
</template>

<style scoped>
.long-buy-strategy-panel{display:grid;gap:12px;padding:18px;border:1px solid var(--folio-line,#dce5df);border-radius:18px;background:linear-gradient(180deg,#fbfdfb 0%,#f7faf7 100%);box-shadow:var(--folio-shadow,0 8px 24px rgba(35,55,42,.06))}.strategy-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:14px}.strategy-heading p{margin:0;color:var(--folio-green,#236a4c);font-size:10px;font-weight:800;letter-spacing:.1em}.strategy-heading h2{margin:4px 0 3px;color:var(--folio-ink,#18231d);font-size:22px;line-height:1.2}.strategy-heading span{color:var(--folio-muted,#718078);font-size:12px}.strategy-heading .strategy-mode{flex:0 0 auto;margin-top:4px;padding:6px 10px;border:1px solid #e4d3a5;border-radius:999px;color:#875f17;background:#fff2d1;font-size:11px;font-weight:750}.strategy-heading .strategy-mode.is-live{border-color:#c8dfd0;color:#236a4c;background:#e7f3eb}.strategy-heading .strategy-mode.is-disabled{border-color:#ebceca;color:#9b493e;background:#f9e8e6}.strategy-top-grid{display:grid;grid-template-columns:minmax(220px,.9fr) minmax(330px,1.25fr) minmax(330px,1.15fr);gap:12px}.strategy-bottom-grid{display:grid;grid-template-columns:minmax(0,1.08fr) minmax(0,1fr);gap:12px;align-items:stretch}@media (max-width:1180px){.strategy-top-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.strategy-top-grid>:last-child{grid-column:1/-1}}@media (max-width:960px){.strategy-bottom-grid{grid-template-columns:1fr}}@media (max-width:720px){.long-buy-strategy-panel{padding:14px}.strategy-heading{display:grid}.strategy-heading .strategy-mode{justify-self:start}.strategy-top-grid{grid-template-columns:1fr}.strategy-top-grid>:last-child{grid-column:auto}.strategy-bottom-grid{grid-template-columns:1fr}}
</style>
