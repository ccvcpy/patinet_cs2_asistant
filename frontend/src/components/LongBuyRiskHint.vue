<script setup lang="ts">
import FolioIcon from "./FolioIcon.vue";
import type {
  ProfitTradeLongBuyStrategyConfig,
  ProfitTradeLongBuyStrategyState,
} from "./profit_trade_long_buy_strategy";

const props = defineProps<{
  config: ProfitTradeLongBuyStrategyConfig;
  state: ProfitTradeLongBuyStrategyState;
}>();

function minimumItemValue(): string {
  const value = props.config.minItemValue;
  return typeof value === "number" && Number.isFinite(value) ? `¥${value.toFixed(2)}` : "minItemValue";
}
</script>

<template>
  <aside class="long-buy-risk-hint" aria-label="长期求购风险提示">
    <div>
      <FolioIcon name="warning" :size="17" />
      <span>
        C5 跌破 {{ minimumItemValue() }}：已有长期单仍持续监控；
        <template v-if="state.canWriteSteam">仅在非交叉盘口且低于激进底线时，才安全撤旧重建。</template>
        <template v-else>当前不撤单、不改价。</template>
      </span>
    </div>
    <div>
      <FolioIcon name="shield" :size="17" />
      <span>交叉盘口 + 旧长期单未成交：优先冻结旧单与直购，等待官方成交证据。</span>
    </div>
  </aside>
</template>

<style scoped>
.long-buy-risk-hint{display:flex;flex-wrap:wrap;gap:9px;padding:12px 14px;border:1px solid #eadfbd;border-radius:14px;background:#fffdf7}.long-buy-risk-hint>div{display:flex;flex:1 1 330px;gap:7px;align-items:flex-start;padding:7px 9px;border-radius:9px;color:#76571f;background:#fff5dc;font-size:11px;line-height:1.45}.long-buy-risk-hint svg{flex:0 0 auto;margin-top:1px;color:#a47720}
</style>
