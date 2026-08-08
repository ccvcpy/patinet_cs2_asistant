<script setup lang="ts">
import FolioIcon from "../FolioIcon.vue";

withDefaults(defineProps<{
  modelValue: number;
  disabled?: boolean;
  expanded?: boolean;
}>(), {
  disabled: false,
  expanded: false,
});

const emit = defineEmits<{
  "update:modelValue": [value: number];
}>();

const intervals = [5, 10, 15, 30] as const;
</script>

<template>
  <div v-if="expanded" class="cm-interval-options">
    <button
      v-for="interval in intervals"
      :key="interval"
      class="cm-interval-option"
      :class="{ 'is-active': modelValue === interval }"
      type="button"
      :disabled="disabled"
      @click="emit('update:modelValue', interval)"
    >
      <span>{{ interval }} 分钟</span>
      <FolioIcon name="chevron-down" :size="11" />
    </button>
  </div>
  <select
    v-else
    class="cm-interval-select"
    :value="modelValue"
    :disabled="disabled"
    aria-label="采集间隔"
    @change="emit('update:modelValue', Number(($event.target as HTMLSelectElement).value))"
  >
    <option v-for="interval in intervals" :key="interval" :value="interval">
      {{ interval }} 分钟
    </option>
  </select>
</template>
