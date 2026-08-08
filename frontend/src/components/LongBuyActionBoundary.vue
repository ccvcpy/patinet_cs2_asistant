<script setup lang="ts">
import { computed } from "vue";
import FolioIcon from "./FolioIcon.vue";
import type {
  ProfitTradeLongBuyStrategyConfig,
  ProfitTradeLongBuyStrategyState,
} from "./profit_trade_long_buy_strategy";

const props = defineProps<{
  config: ProfitTradeLongBuyStrategyConfig;
  state: ProfitTradeLongBuyStrategyState;
}>();

type BoundaryAction = {
  id: string;
  label: string;
  detail: string;
  allowed: boolean;
};

const boundaryActions = computed<BoundaryAction[]>(() => [
  {
    id: "profit-cycle",
    label: "Profit Trade 周期与新机会扫描",
    detail: props.config.enabled ? "总功能已开启。" : "Profit Trade 总功能已关闭。",
    allowed: props.config.enabled,
  },
  {
    id: "ordinary-purchase",
    label: "原有直接购买链路",
    detail: props.config.enabled
      ? (props.config.allowRealExecution ? "允许买 B、上架 C5 与后续自动改价。" : "普通真实执行已关闭。")
      : "Profit Trade 总功能已关闭。",
    allowed: props.config.enabled && props.config.allowRealExecution,
  },
  {
    id: "long-buy-scan",
    label: "长期求购方案计算与观察",
    detail: props.config.enabled
      ? (props.config.longBuyEnabled ? "长期求购功能已开启。" : "长期求购功能已关闭。")
      : "Profit Trade 总功能已关闭。",
    allowed: props.state.canObserve,
  },
  {
    id: "long-buy-reconcile",
    label: "核对已有 Steam 长期求购成交",
    detail: props.state.canReconcileExistingOrders
      ? "已有订单继续读取官方成交证据。"
      : "Profit Trade 总功能关闭后暂停核对。",
    allowed: props.state.canReconcileExistingOrders,
  },
  {
    id: "long-buy-c5-followup",
    label: "长期求购成交后锁 A 并上架 C5",
    detail: props.state.canExecuteC5Followup
      ? "普通真实执行已开启，可以推进成交后的做 T 闭环。"
      : "需要总功能、普通真实执行和长期求购功能同时开启。",
    allowed: props.state.canExecuteC5Followup,
  },
  {
    id: "long-buy-steam-write",
    label: "新建、撤销或改价 Steam 长期求购",
    detail: props.state.canWriteSteam
      ? "四层许可已全部满足，仍需通过钱包、ROI 与盘口风控。"
      : "需要四个开关全部开启；长期 Steam 写入必须单独确认。",
    allowed: props.state.canWriteSteam,
  },
]);

const allowedActions = computed(() => boundaryActions.value.filter((item) => item.allowed));
const blockedActions = computed(() => [
  ...boundaryActions.value.filter((item) => !item.allowed),
  {
    id: "crossed-book-freeze",
    label: "交叉盘口旧单未成交时撤旧、改价或直购",
    detail: "始终禁止，保留旧求购等待官方成交证据。",
    allowed: false,
  },
  {
    id: "bypass-risk-gates",
    label: "绕过钱包、ROI 或老库存 A 风控",
    detail: "始终禁止。",
    allowed: false,
  },
]);
</script>

<template>
  <article class="long-buy-action-boundary" aria-label="长期求购动作边界">
    <header>
      <span class="component-kicker">随四个开关实时变化</span>
      <h3>当前允许与禁止的具体动作</h3>
    </header>
    <div class="boundary-grid">
      <section class="allowed">
        <h4><FolioIcon name="success" :size="18" />当前允许</h4>
        <p v-if="allowedActions.length === 0" class="empty-boundary">当前没有允许执行的 Profit Trade 动作。</p>
        <ul v-else>
          <li v-for="item in allowedActions" :key="item.id" :data-testid="`allowed-${item.id}`">
            <strong>{{ item.label }}</strong>
            <small>{{ item.detail }}</small>
          </li>
        </ul>
      </section>
      <section class="blocked">
        <h4><FolioIcon name="error" :size="18" />当前禁止</h4>
        <ul>
          <li v-for="item in blockedActions" :key="item.id" :data-testid="`blocked-${item.id}`">
            <strong>{{ item.label }}</strong>
            <small>{{ item.detail }}</small>
          </li>
        </ul>
      </section>
    </div>
  </article>
</template>

<style scoped>
.long-buy-action-boundary{display:grid;gap:11px;min-height:196px;padding:16px;border:1px solid var(--folio-line,#dce5df);border-radius:16px;background:#fff;box-shadow:var(--folio-shadow,0 8px 24px rgba(35,55,42,.06))}.component-kicker{display:block;color:var(--folio-muted,#718078);font-size:10px;font-weight:750;letter-spacing:.08em}.long-buy-action-boundary h3{margin:3px 0 0;color:var(--folio-ink,#18231d);font-size:16px}.boundary-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.boundary-grid section{padding:10px;border:1px solid #e7ece7;border-radius:10px;background:#fbfdfb}.boundary-grid section.blocked{border-color:#f0ddd9;background:#fffafa}.boundary-grid h4{display:flex;gap:6px;align-items:center;margin:0;color:var(--folio-green,#236a4c);font-size:13px}.boundary-grid .blocked h4{color:var(--folio-red,#9b493e)}.boundary-grid ul{display:grid;gap:9px;margin:10px 0 0;padding:0;list-style:none}.boundary-grid li{position:relative;display:grid;gap:2px;padding-left:11px;color:#405048;line-height:1.4}.boundary-grid li::before{position:absolute;left:0;color:var(--folio-green,#236a4c);content:"•"}.boundary-grid .blocked li::before{color:var(--folio-red,#9b493e)}.boundary-grid li strong{font-size:10px}.boundary-grid li small{color:var(--folio-muted,#718078);font-size:9px;line-height:1.4}.empty-boundary{margin:10px 0 0;color:var(--folio-muted,#718078);font-size:10px;line-height:1.45}@media (max-width:720px){.boundary-grid{grid-template-columns:1fr}}
</style>
