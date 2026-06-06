<script setup lang="ts">
import { computed, onMounted, ref } from "vue";

const STORAGE_KEY = "cs-account-check-previous-total";

const previousTotal = ref<number>(1878.31);
const currentRecordedBalance = ref<number>(0);
const realTotal = ref<number | null>(null);
const savedAt = ref<string>("");

onMounted(() => {
  const stored = window.localStorage.getItem(STORAGE_KEY);
  if (stored !== null) {
    const parsed = Number(stored);
    if (Number.isFinite(parsed)) {
      previousTotal.value = parsed;
    }
  }
});

const programTotal = computed(() => previousTotal.value + currentRecordedBalance.value);
const hasRealTotal = computed(() => realTotal.value !== null && Number.isFinite(realTotal.value));
const difference = computed(() => (hasRealTotal.value ? Number(realTotal.value) - programTotal.value : 0));
const isBalanced = computed(() => hasRealTotal.value && Math.abs(difference.value) < 0.005);
const resultText = computed(() => {
  if (!hasRealTotal.value) return "等待 Real Total";
  return isBalanced.value ? "相等，程序记录挂刀余额等于真实挂刀余额" : "不相等，需要复查挂刀余额";
});

function formatMoney(value: number): string {
  return value.toFixed(2);
}

function parseCardNumber(value: Event): number | null {
  const target = value.target as HTMLElement;
  const raw = target.textContent?.trim() ?? "";
  if (!raw) return null;
  const parsed = Number(raw.replace(/[^\d.-]/g, ""));
  return Number.isFinite(parsed) ? parsed : null;
}

function updatePreviousTotal(event: Event): void {
  previousTotal.value = parseCardNumber(event) ?? 0;
}

function updateCurrentRecordedBalance(event: Event): void {
  currentRecordedBalance.value = parseCardNumber(event) ?? 0;
}

function updateRealTotal(event: Event): void {
  realTotal.value = parseCardNumber(event);
}

function saveSnapshot(): void {
  if (!hasRealTotal.value) return;
  const nextPrevious = Number(realTotal.value);
  previousTotal.value = nextPrevious;
  currentRecordedBalance.value = 0;
  realTotal.value = null;
  savedAt.value = new Date().toLocaleString();
  window.localStorage.setItem(STORAGE_KEY, String(nextPrevious));
}
</script>

<template>
  <main class="page">
    <header class="page-header">
      <div>
        <p class="eyebrow">挂刀余额核对</p>
        <h1>挂刀余额核对</h1>
      </div>
      <button class="primary-button" type="button" :disabled="!hasRealTotal" @click="saveSnapshot">
        保存
      </button>
    </header>

    <section class="metrics-grid">
      <article class="metric-card">
        <span>Previous Total (manual)</span>
        <strong
          class="editable-total"
          contenteditable="true"
          inputmode="decimal"
          @input="updatePreviousTotal"
        >
          {{ formatMoney(previousTotal) }}
        </strong>
      </article>
      <article class="metric-card">
        <span>本次程序记录</span>
        <strong
          class="editable-total"
          contenteditable="true"
          inputmode="decimal"
          @input="updateCurrentRecordedBalance"
        >
          {{ formatMoney(currentRecordedBalance) }}
        </strong>
      </article>
      <article class="metric-card">
        <span>程序合计</span>
        <strong>{{ formatMoney(programTotal) }}</strong>
      </article>
      <article class="metric-card" :class="{ success: isBalanced, danger: hasRealTotal && !isBalanced }">
        <span>最终结果</span>
        <strong
          class="editable-total"
          contenteditable="true"
          inputmode="decimal"
          @input="updateRealTotal"
        >
          {{ hasRealTotal ? formatMoney(Number(realTotal)) : resultText }}
        </strong>
      </article>
    </section>

    <p v-if="savedAt" class="save-note">已保存 {{ savedAt }}，本次 Real Total 已成为 Previous Total (manual)。</p>
  </main>
</template>
