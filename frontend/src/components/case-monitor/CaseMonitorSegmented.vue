<script setup lang="ts" generic="T extends string | number">
import type { SelectOption } from "./types";

defineProps<{
  modelValue: T;
  options: SelectOption<T>[];
  disabled?: boolean;
}>();

const emit = defineEmits<{
  "update:modelValue": [value: T];
}>();
</script>

<template>
  <div class="cm-segmented">
    <button
      v-for="option in options"
      :key="String(option.value)"
      class="cm-segmented__option"
      :class="{ 'is-active': modelValue === option.value }"
      type="button"
      :disabled="disabled"
      @click="emit('update:modelValue', option.value)"
    >
      {{ option.label }}
    </button>
  </div>
</template>
