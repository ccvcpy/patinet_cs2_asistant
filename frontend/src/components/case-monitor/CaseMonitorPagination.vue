<script setup lang="ts">
import { computed } from "vue";
import FolioIcon from "../FolioIcon.vue";

const props = withDefaults(defineProps<{
  modelValue: number;
  totalItems: number;
  pageSize: number;
  pageSizeOptions?: number[];
  disabled?: boolean;
  compact?: boolean;
}>(), {
  pageSizeOptions: () => [10, 20, 50],
  disabled: false,
  compact: false,
});

const emit = defineEmits<{
  "update:modelValue": [value: number];
  "update:pageSize": [value: number];
}>();

const totalPages = computed(() => Math.max(1, Math.ceil(props.totalItems / props.pageSize)));

const middlePages = computed(() => {
  const visiblePageCount = props.compact ? 3 : 5;
  if (totalPages.value <= visiblePageCount + 1) {
    return Array.from({ length: totalPages.value }, (_, index) => index + 1);
  }
  const start = Math.max(
    1,
    Math.min(
      props.modelValue - Math.floor(visiblePageCount / 2),
      totalPages.value - visiblePageCount + 1,
    ),
  );
  return Array.from({ length: visiblePageCount }, (_, index) => start + index);
});

function choose(page: number): void {
  const bounded = Math.max(1, Math.min(totalPages.value, page));
  emit("update:modelValue", bounded);
}
</script>

<template>
  <footer class="cm-pagination" aria-label="推荐排行分页">
    <span>共 {{ totalItems }} 条，第 {{ modelValue }} / {{ totalPages }} 页</span>
    <div class="cm-pagination__actions">
      <button
        class="cm-page-button"
        type="button"
        aria-label="上一页"
        :disabled="disabled || modelValue <= 1"
        @click="choose(modelValue - 1)"
      >
        <FolioIcon name="chevron-left" :size="15" />
      </button>

      <template v-if="middlePages[0] > 1">
        <button class="cm-page-button" type="button" @click="choose(1)">1</button>
        <span class="cm-pagination__ellipsis">…</span>
      </template>

      <button
        v-for="page in middlePages"
        :key="page"
        class="cm-page-button"
        :class="{ 'is-active': page === modelValue }"
        type="button"
        :disabled="disabled"
        :aria-current="page === modelValue ? 'page' : undefined"
        @click="choose(page)"
      >
        {{ page }}
      </button>

      <template v-if="middlePages[middlePages.length - 1] < totalPages">
        <span class="cm-pagination__ellipsis">…</span>
        <button class="cm-page-button" type="button" @click="choose(totalPages)">
          {{ totalPages }}
        </button>
      </template>

      <button
        class="cm-page-button"
        type="button"
        aria-label="下一页"
        :disabled="disabled || modelValue >= totalPages"
        @click="choose(modelValue + 1)"
      >
        <FolioIcon name="chevron-right" :size="15" />
      </button>

      <select
        class="cm-page-size"
        :value="pageSize"
        aria-label="每页条数"
        :disabled="disabled"
        @change="emit('update:pageSize', Number(($event.target as HTMLSelectElement).value))"
      >
        <option v-for="size in pageSizeOptions" :key="size" :value="size">
          每页 {{ size }} 条
        </option>
      </select>
    </div>
  </footer>
</template>
