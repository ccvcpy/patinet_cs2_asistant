<script setup lang="ts">
import FolioIcon from "../FolioIcon.vue";

withDefaults(defineProps<{
  modelValue: string;
  placeholder?: string;
}>(), {
  placeholder: "搜索中文名或英文名",
});

const emit = defineEmits<{
  "update:modelValue": [value: string];
}>();
</script>

<template>
  <label class="cm-search">
    <FolioIcon class="cm-search__icon" name="search" :size="16" />
    <input
      type="search"
      :value="modelValue"
      :placeholder="placeholder"
      @input="emit('update:modelValue', ($event.target as HTMLInputElement).value)"
    />
    <button
      v-if="modelValue"
      class="cm-search__clear"
      type="button"
      aria-label="清空搜索"
      @click="emit('update:modelValue', '')"
    >
      <FolioIcon name="x" :size="13" />
    </button>
  </label>
</template>
