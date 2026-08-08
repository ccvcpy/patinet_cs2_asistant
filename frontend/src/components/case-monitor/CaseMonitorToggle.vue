<script setup lang="ts">
defineProps<{
  modelValue: boolean;
  disabled?: boolean;
  onLabel?: string;
  offLabel?: string;
}>();

const emit = defineEmits<{
  "update:modelValue": [value: boolean];
}>();
</script>

<template>
  <button
    class="cm-toggle"
    :class="{ 'is-on': modelValue }"
    type="button"
    role="switch"
    :aria-checked="modelValue"
    :disabled="disabled"
    @click="emit('update:modelValue', !modelValue)"
  >
    <span class="cm-toggle__track" aria-hidden="true">
      <span class="cm-toggle__thumb" />
    </span>
    <span>{{ modelValue ? (onLabel || "开") : (offLabel || "关") }}</span>
  </button>
</template>
