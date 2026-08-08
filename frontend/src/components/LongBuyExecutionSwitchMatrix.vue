<script setup lang="ts">
import { computed } from "vue";
import {
  requiresLongBuyConfigConfirmation,
  type ProfitTradeLongBuyConfigKey,
  type ProfitTradeLongBuyStrategyConfig,
  type ProfitTradeLongBuyStrategyState,
} from "./profit_trade_long_buy_strategy";

const props = defineProps<{
  config: ProfitTradeLongBuyStrategyConfig;
  state: ProfitTradeLongBuyStrategyState;
  updatingKey?: ProfitTradeLongBuyConfigKey | null;
}>();

const emit = defineEmits<{
  toggle: [key: ProfitTradeLongBuyConfigKey, nextEnabled: boolean];
}>();

const switches = computed(() => [
  {
    id: "enabled",
    configKey: "enabled" as const,
    key: "profitTrade.enabled",
    label: "Profit Trade 总功能",
    detail: "控制 Profit Trade 周期是否运行。",
    enabled: props.config.enabled,
  },
  {
    id: "real-execution",
    configKey: "allowRealExecution" as const,
    key: "profitTrade.allowRealExecution",
    label: "普通真实执行",
    detail: "控制原有直购、C5 上架与改价的真实动作。",
    enabled: props.config.allowRealExecution,
  },
  {
    id: "feature",
    configKey: "longBuyEnabled" as const,
    key: "profitTrade.longBuyEnabled",
    label: "长期求购功能",
    detail: "控制新长期求购的算价与观察；已有订单仍做安全成交核对。",
    enabled: props.config.longBuyEnabled,
  },
  {
    id: "steam-write",
    configKey: "longBuyAllowRealExecution" as const,
    key: "profitTrade.longBuyAllowRealExecution",
    label: "长期求购 Steam 写入",
    detail: "单独控制长期求购的新建、撤销与安全重建。",
    enabled: props.config.longBuyAllowRealExecution,
  },
].map((item) => ({
  ...item,
  needsConfirmation: requiresLongBuyConfigConfirmation(item.configKey),
})));

function requestToggle(key: ProfitTradeLongBuyConfigKey, enabled: boolean): void {
  if (props.updatingKey) return;
  emit("toggle", key, !enabled);
}
</script>

<template>
  <article class="long-buy-switch-matrix" aria-label="长期求购开关状态">
    <header>
      <div>
        <span class="component-kicker">四层开关矩阵</span>
        <h3>长期求购权限</h3>
      </div>
      <span class="switch-hint">前三项直接切换</span>
    </header>

    <div class="switch-list">
      <article
        v-for="item in switches"
        :key="item.id"
        :data-testid="`long-buy-setting-${item.id}`"
        class="switch-row"
      >
        <div class="switch-copy">
          <code>{{ item.key }}</code>
          <strong>{{ item.label }}</strong>
          <small>{{ item.detail }}</small>
        </div>
        <button
          :data-testid="`long-buy-toggle-${item.id}`"
          :aria-label="`${item.label}：${item.enabled ? '开启' : '关闭'}`"
          :aria-pressed="item.enabled"
          class="switch-control"
          type="button"
          :disabled="Boolean(updatingKey)"
          @click="requestToggle(item.configKey, item.enabled)"
        >
          <span :class="['static-switch', { on: item.enabled }]" aria-hidden="true"><i></i></span>
          <b :class="{ on: item.enabled }">{{ item.enabled ? "开启" : "关闭" }}</b>
          <em v-if="item.needsConfirmation">需确认</em>
        </button>
      </article>
    </div>

    <p :class="['effective-result', `is-${state.mode}`]" data-testid="long-buy-effective-result">
      <span>当前结果</span>
      <strong>长期求购{{ state.mode === "live" ? "可写 Steam" : state.canObserve ? "只观察，不写 Steam" : "未运行" }}</strong>
    </p>
  </article>
</template>

<style scoped>
.long-buy-switch-matrix{display:grid;gap:11px;min-height:196px;padding:16px;border:1px solid var(--folio-line,#dce5df);border-radius:16px;background:#fff;box-shadow:var(--folio-shadow,0 8px 24px rgba(35,55,42,.06))}.long-buy-switch-matrix header{display:flex;align-items:start;justify-content:space-between;gap:10px}.component-kicker{display:block;color:var(--folio-muted,#718078);font-size:10px;font-weight:750;letter-spacing:.08em}.long-buy-switch-matrix h3{margin:3px 0 0;color:var(--folio-ink,#18231d);font-size:16px}.switch-hint{padding:3px 7px;border:1px solid var(--folio-line,#dce5df);border-radius:999px;color:var(--folio-muted,#718078);background:var(--folio-surface-soft,#f4f7f4);font-size:10px;font-weight:700}.switch-list{display:grid;gap:2px}.switch-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:9px;align-items:center;padding:6px 0;border-bottom:1px solid #edf0ed}.switch-row:last-child{border-bottom:0}.switch-copy{display:grid;gap:1px;min-width:0}.switch-copy code{color:#6b786f;font-size:10px;overflow-wrap:anywhere}.switch-copy strong{color:var(--folio-ink,#18231d);font-size:12px}.switch-copy small{color:var(--folio-muted,#718078);font-size:10px;line-height:1.35}.switch-control{display:flex;align-items:center;justify-content:flex-end;gap:6px;min-width:89px;padding:3px 0;border:0;color:inherit;background:transparent;cursor:pointer}.switch-control:focus-visible{outline:2px solid var(--folio-green,#236a4c);outline-offset:3px;border-radius:8px}.switch-control:disabled{cursor:wait;opacity:.58}.static-switch{position:relative;display:block;width:30px;height:18px;border-radius:999px;background:#adb8b1;box-shadow:inset 0 0 0 1px rgba(20,38,29,.08)}.static-switch i{position:absolute;top:3px;left:3px;width:12px;height:12px;border-radius:50%;background:#fff;box-shadow:0 1px 3px rgba(25,38,30,.22);transition:transform .16s ease}.static-switch.on{background:var(--folio-green,#236a4c)}.static-switch.on i{transform:translateX(12px)}.switch-control b{padding:3px 7px;border-radius:999px;color:#7b564f;background:#f8e9e7;font-size:10px;white-space:nowrap}.switch-control b.on{color:#236a4c;background:#e3f1e7}.switch-control em{font-style:normal;color:#875f17;font-size:9px;white-space:nowrap}.effective-result{display:flex;gap:7px;align-items:baseline;margin:0;padding:8px 10px;border-radius:9px;color:#875f17;background:#fff3d8;font-size:11px}.effective-result span{font-weight:700}.effective-result strong{font-size:12px}.effective-result.is-live{color:#236a4c;background:#e7f3eb}.effective-result.is-disabled{color:#87514b;background:#f9e8e6}
</style>
