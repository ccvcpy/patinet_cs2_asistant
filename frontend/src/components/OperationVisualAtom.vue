<script setup lang="ts">
import { computed } from "vue";

export type OperationVisualAtomName =
  | "accounts"
  | "steam-balance"
  | "pending-wallet"
  | "total-database"
  | "refresh"
  | "case-single"
  | "case-scanner"
  | "price-tag"
  | "budget-wallet"
  | "clock"
  | "delivery-success"
  | "progress-ring"
  | "trend-line"
  | "ratio-pie"
  | "search"
  | "shield-success"
  | "warning"
  | "error"
  | "pause"
  | "calendar";

const props = withDefaults(
  defineProps<{
    name: OperationVisualAtomName;
    size?: number;
    alt?: string;
  }>(),
  {
    size: 44,
    alt: "",
  },
);

const src = computed(() => `/images/operations/atoms/${props.name}.webp`);
</script>

<template>
  <img
    class="operation-visual-atom"
    :src="src"
    :alt="alt"
    :aria-hidden="alt ? undefined : 'true'"
    :width="size"
    :height="size"
    decoding="async"
  />
</template>

<style scoped>
.operation-visual-atom {
  display: block;
  flex: 0 0 auto;
  object-fit: contain;
  border-radius: var(--ops-radius-md, 8px);
}
</style>
