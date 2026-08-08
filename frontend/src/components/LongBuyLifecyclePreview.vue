<script setup lang="ts">
import { computed } from "vue";
import FolioIcon from "./FolioIcon.vue";
import type { ProfitTradeLongBuyStrategyState } from "./profit_trade_long_buy_strategy";

const props = defineProps<{
  state: ProfitTradeLongBuyStrategyState;
  activeOrderCount?: number;
}>();

const steps = computed(() => [
  { icon: "success", label: "Steam 官方成交确认", status: "ready" },
  { icon: "lock", label: "锁可交易老库存 A", status: "ready" },
  { icon: "report", label: "记录实际 paidTotal", status: "ready" },
  {
    icon: "case",
    label: "C5 上架 A",
    status: props.state.canExecuteC5Followup ? "ready" : "waiting",
  },
] as const);

const emptyStateText = computed(() => {
  const count = props.activeOrderCount ?? 0;
  if (count > 0) return `当前有 ${count} 笔程序管理的长期求购订单。`;
  return "当前没有程序管理的长期求购订单。";
});
</script>

<template>
  <div class="lifecycle-shell">
    <span class="atomic-component-label">LongBuyLifecyclePreview</span>
    <article class="long-buy-lifecycle" aria-label="长期求购成交后生命周期">
      <section class="lifecycle-timeline">
        <ol>
          <li
            v-for="step in steps"
            :key="step.label"
            :class="{ waiting: step.status === 'waiting' }"
          >
            <span class="step-marker"><FolioIcon :name="step.icon" :size="15" /></span>
            <div>
              <strong>{{ step.label }}</strong>
              <small v-if="step.status === 'waiting'">保留成交，等待 C5 执行许可</small>
            </div>
          </li>
        </ol>
      </section>

      <section class="lifecycle-empty-state">
        <span class="empty-icon"><FolioIcon name="document" :size="26" /></span>
        <p>{{ emptyStateText }}</p>
      </section>
    </article>
  </div>
</template>

<style scoped>
.lifecycle-shell {
  display: grid;
  grid-template-rows: auto 1fr;
  gap: 6px;
  min-width: 0;
}

.atomic-component-label {
  color: #89958e;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 9px;
  letter-spacing: .04em;
}

.long-buy-lifecycle {
  display: grid;
  grid-template-columns: minmax(220px, .88fr) minmax(240px, 1.12fr);
  height: 100%;
  min-height: 158px;
  overflow: hidden;
  border: 1px solid #dfe6e1;
  border-radius: 11px;
  background: #fff;
  box-shadow: 0 5px 16px rgba(34, 54, 43, .045);
}

.lifecycle-timeline {
  display: grid;
  align-items: center;
  padding: 13px 16px;
  border-right: 1px solid #e7ece8;
}

.lifecycle-timeline ol {
  display: grid;
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.lifecycle-timeline li {
  position: relative;
  display: grid;
  grid-template-columns: 27px minmax(0, 1fr);
  gap: 10px;
  align-items: center;
  min-height: 27px;
  color: #34483e;
}

.lifecycle-timeline li:not(:last-child)::after {
  position: absolute;
  top: 27px;
  bottom: -8px;
  left: 13px;
  width: 1px;
  background: #d8e1db;
  content: "";
}

.step-marker {
  z-index: 1;
  display: grid;
  place-items: center;
  width: 27px;
  height: 27px;
  border: 1px solid #dce7df;
  border-radius: 50%;
  color: #236a4c;
  background: #f7fbf8;
}

.lifecycle-timeline li>div {
  display: grid;
  gap: 1px;
  min-width: 0;
}

.lifecycle-timeline strong {
  color: #34483e;
  font-size: 10px;
  font-weight: 650;
  line-height: 1.25;
}

.lifecycle-timeline small {
  color: #9b6b14;
  font-size: 8px;
}

.lifecycle-timeline li.waiting .step-marker {
  border-color: #eadfbd;
  color: #a47720;
  background: #fff9e9;
}

.lifecycle-empty-state {
  display: grid;
  place-content: center;
  justify-items: center;
  gap: 9px;
  min-width: 0;
  padding: 18px;
  text-align: center;
}

.empty-icon {
  display: grid;
  place-items: center;
  width: 50px;
  height: 50px;
  border: 1px solid #e1e7e3;
  border-radius: 50%;
  color: #b7c1bb;
  background: #fbfcfb;
}

.lifecycle-empty-state p {
  max-width: 170px;
  margin: 0;
  color: #7c8982;
  font-size: 10px;
  line-height: 1.45;
}

@media (max-width: 900px) {
  .long-buy-lifecycle {
    grid-template-columns: minmax(200px, .9fr) minmax(200px, 1.1fr);
  }
}

@media (max-width: 620px) {
  .long-buy-lifecycle {
    grid-template-columns: 1fr;
  }

  .lifecycle-timeline {
    border-right: 0;
    border-bottom: 1px solid #e7ece8;
  }
}
</style>
