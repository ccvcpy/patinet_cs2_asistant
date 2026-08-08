<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { formatProfitTradeRatio } from "./profit_trade_roi_format";
import type { ProfitTradeWatchItem } from "./profit_trade_roi_types";

const props = withDefaults(defineProps<{
  row?: ProfitTradeWatchItem | null;
  maxQuantity?: number;
  submitting?: boolean;
  error?: string;
}>(), {
  row: null,
  maxQuantity: 0,
  submitting: false,
  error: "",
});

const emit = defineEmits<{
  close: [];
  confirm: [quantity: number];
}>();

const quantity = ref(1);
const safeMaxQuantity = computed(() => Math.max(0, Math.floor(props.maxQuantity || 0)));
const expectedTotalProfit = computed(() => (
  typeof props.row?.expectedProfit === "number"
    ? props.row.expectedProfit * quantity.value
    : null
));
const belowAutomaticThreshold = computed(() => {
  const roi = props.row?.expectedRoi;
  const minRoi = props.row?.minRoi;
  return typeof roi === "number" && typeof minRoi === "number" && roi < minRoi;
});

watch(
  () => props.row?.marketHashName,
  () => {
    // 每次重新打开确认框都从 1 件开始，避免沿用上一件饰品的批量数量。
    quantity.value = 1;
  },
  { immediate: true },
);

watch(safeMaxQuantity, (maxQuantity) => {
  quantity.value = Math.min(Math.max(1, quantity.value), Math.max(1, maxQuantity));
});

function money(value?: number | null): string {
  return typeof value === "number" && Number.isFinite(value) ? `¥${value.toFixed(2)}` : "—";
}

function pct(value?: number | null): string {
  return formatProfitTradeRatio(value);
}

function setQuantity(value: number): void {
  const normalizedValue = Number.isFinite(value) ? Math.floor(value) : 1;
  quantity.value = Math.min(Math.max(1, normalizedValue), Math.max(1, safeMaxQuantity.value));
}

function onQuantityInput(event: Event): void {
  const input = event.target as HTMLInputElement;
  setQuantity(input.valueAsNumber);
  // `:value` 不会在状态仍为 1 时主动覆盖用户清空后的 DOM 值，这里立即回显归一化结果。
  input.value = String(quantity.value);
}

function close(): void {
  if (!props.submitting) emit("close");
}
</script>

<template>
  <Teleport to="body">
    <div v-if="row" class="manual-execution-backdrop" @click.self="close">
      <section class="manual-execution-dialog" role="dialog" aria-modal="true" aria-labelledby="manual-execution-title">
        <header>
          <div>
            <h2 id="manual-execution-title">确认一键执行</h2>
            <p>{{ row.name || row.marketHashName }}<small v-if="row.name && row.name !== row.marketHashName">{{ row.marketHashName }}</small></p>
          </div>
          <button type="button" aria-label="关闭" :disabled="submitting" @click="close">×</button>
        </header>

        <div v-if="belowAutomaticThreshold" class="roi-warning">
          <strong>!</strong>
          <span>当前 ROI {{ pct(row.expectedRoi) }} 低于自动执行门槛 {{ pct(row.minRoi) }}</span>
        </div>
        <div v-else class="roi-ready">
          当前 ROI {{ pct(row.expectedRoi) }}，本次仍按人工指定数量执行。
        </div>
        <p class="approval-note">这是一次人工批准，只执行本次确认数量，不修改全局策略。</p>

        <section class="quantity-section" aria-labelledby="manual-execution-quantity">
          <strong id="manual-execution-quantity">执行数量</strong>
          <div class="quantity-row">
            <div class="quantity-stepper">
              <button type="button" :disabled="submitting || quantity <= 1" @click="setQuantity(quantity - 1)">−</button>
              <input
                :value="quantity"
                type="number"
                min="1"
                :max="safeMaxQuantity"
                :disabled="submitting"
                aria-label="执行数量"
                @input="onQuantityInput"
              >
              <button type="button" :disabled="submitting || quantity >= safeMaxQuantity" @click="setQuantity(quantity + 1)">＋</button>
            </div>
            <button class="all-button" type="button" :disabled="submitting || safeMaxQuantity <= 0" @click="setQuantity(safeMaxQuantity)">全部</button>
          </div>
          <small>最多可执行 {{ safeMaxQuantity }} 件 · 当前库存 {{ row.inventoryCount ?? "—" }} 件</small>
        </section>

        <section class="execution-summary" aria-label="预计执行结果">
          <div><small>当前 ROI</small><strong>{{ pct(row.expectedRoi) }}</strong></div>
          <div><small>预计单件收益</small><strong>{{ money(row.expectedProfit) }}</strong></div>
          <div><small>预计总收益</small><strong>{{ money(expectedTotalProfit) }}</strong></div>
        </section>

        <p class="safety-note">
          <span>i</span>
          确认后将创建 {{ quantity }} 笔真实做 T 流水，并按现有风控逐笔执行。执行前会重新校验实时价格；变差时安全取消，不沿用旧价格强买。
        </p>
        <p v-if="error" class="execution-error" role="alert">{{ error }}</p>

        <footer>
          <button class="secondary" type="button" :disabled="submitting" @click="close">取消</button>
          <button
            class="primary"
            type="button"
            :disabled="submitting || safeMaxQuantity <= 0"
            @click="emit('confirm', quantity)"
          >
            {{ submitting ? "提交中…" : `确认执行 ${quantity} 件` }}
          </button>
        </footer>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.manual-execution-backdrop{position:fixed;z-index:1300;inset:0;display:grid;place-items:center;padding:24px;background:rgba(20,34,27,.48);backdrop-filter:blur(4px)}.manual-execution-dialog{width:min(470px,calc(100vw - 32px));max-height:calc(100vh - 32px);overflow:auto;border:1px solid #d8ded8;border-radius:16px;padding:22px 24px;background:#fff;box-shadow:0 28px 80px rgba(18,45,31,.25)}header{display:flex;align-items:flex-start;justify-content:space-between;gap:16px}header h2{margin:0;color:#17201c;font-size:21px}header p{display:grid;gap:2px;margin:7px 0 0;color:#536159;font-size:12px}header p small{color:#7a847e;font-size:10px;overflow-wrap:anywhere}header>button{border:0;padding:0;color:#425047;background:transparent;font-size:26px;line-height:1}.roi-warning,.roi-ready{display:flex;align-items:center;gap:9px;margin-top:18px;border-radius:9px;padding:11px 13px;color:#6b5520;background:#fff4d8;font-size:12px}.roi-warning strong{display:grid;place-items:center;width:18px;height:18px;border-radius:50%;color:#fff;background:#e7a70d;font-size:12px}.roi-ready{color:#236a4c;background:#eef7f1}.approval-note{margin:15px 0 0;color:#4d5b53;font-size:12px;line-height:1.6}.quantity-section{display:grid;gap:9px;margin-top:20px}.quantity-section>strong{color:#27332c;font-size:13px}.quantity-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px}.quantity-stepper{display:grid;grid-template-columns:48px minmax(0,1fr) 48px;min-height:44px;border:1px solid #d7ded8;border-radius:8px;overflow:hidden}.quantity-stepper button,.quantity-stepper input,.all-button{border:0;color:#26342c;background:#fff}.quantity-stepper button{font-size:20px}.quantity-stepper button:disabled{color:#acb6af}.quantity-stepper input{min-width:0;border-right:1px solid #edf0ed;border-left:1px solid #edf0ed;text-align:center;font-size:17px;font-weight:750;appearance:textfield}.quantity-stepper input::-webkit-inner-spin-button{appearance:none}.all-button{border:1px solid #d9e5dc;border-radius:8px;padding:0 14px;color:#236a4c;background:#f1f7f3;font-weight:750}.quantity-section>small{color:#758078;font-size:10px}.execution-summary{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));margin-top:18px;border-radius:10px;background:#f3f8f4;overflow:hidden}.execution-summary>div{display:grid;gap:4px;padding:13px 9px;text-align:center}.execution-summary>div+div{border-left:1px solid #e1e9e3}.execution-summary small{color:#6f7a73;font-size:10px}.execution-summary strong{color:#176c46;font-size:18px}.safety-note{display:flex;gap:8px;margin:16px 0 0;border-radius:8px;padding:10px 11px;color:#59655e;background:#f5f7f5;font-size:10px;line-height:1.55}.safety-note span{display:grid;flex:0 0 auto;place-items:center;width:16px;height:16px;border:1px solid #849087;border-radius:50%;font-weight:800}.execution-error{margin:12px 0 0;border:1px solid #ebc4be;border-radius:8px;padding:10px 11px;color:#923f34;background:#fff5f3;font-size:11px;line-height:1.5}footer{display:grid;grid-template-columns:1fr 1.45fr;gap:12px;margin-top:18px}footer button{min-height:44px;border-radius:8px;font-size:12px;font-weight:750}.secondary{border:1px solid #d5dcd6;color:#3f4e45;background:#fff}.primary{border:1px solid #28714d;color:#fff;background:#28714d}.primary:disabled,.secondary:disabled{cursor:not-allowed;opacity:.58}@media(max-width:560px){.manual-execution-backdrop{padding:12px}.manual-execution-dialog{padding:18px}.execution-summary{grid-template-columns:1fr}.execution-summary>div+div{border-top:1px solid #e1e9e3;border-left:0}footer{grid-template-columns:1fr}}
</style>
