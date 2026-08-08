<script setup lang="ts">
import type { CaseCategoryOption } from "./types";

defineProps<{
  modelValue: string;
  options: CaseCategoryOption[];
}>();

const emit = defineEmits<{
  "update:modelValue": [value: string];
}>();
</script>

<template>
  <div class="cm-category-tabs" role="tablist" aria-label="箱子类别">
    <button
      v-for="option in options"
      :key="option.key"
      class="cm-category-tab"
      :class="{ 'is-active': modelValue === option.key }"
      type="button"
      role="tab"
      :aria-selected="modelValue === option.key"
      @click="emit('update:modelValue', option.key)"
    >
      <span>{{ option.label }}</span>
      <strong>{{ option.count }}</strong>
    </button>
  </div>
</template>
