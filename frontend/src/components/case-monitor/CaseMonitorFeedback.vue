<script setup lang="ts">
import FolioIcon from "../FolioIcon.vue";

withDefaults(defineProps<{
  tone: "success" | "error";
  dismissible?: boolean;
}>(), {
  dismissible: true,
});

const emit = defineEmits<{
  close: [];
}>();
</script>

<template>
  <div class="cm-feedback" :class="`cm-feedback--${tone}`" role="status">
    <FolioIcon :name="tone === 'success' ? 'success' : 'error'" :size="17" />
    <span><slot /></span>
    <span v-if="$slots.actions" class="cm-feedback__actions"><slot name="actions" /></span>
    <button
      v-if="dismissible"
      class="cm-feedback__close"
      type="button"
      aria-label="关闭消息"
      @click="emit('close')"
    >
      <FolioIcon name="x" :size="14" />
    </button>
  </div>
</template>
