<script setup lang="ts">
import FolioIcon, { type FolioIconName } from "../FolioIcon.vue";

withDefaults(defineProps<{
  tone?: "primary" | "success" | "quiet";
  icon?: FolioIconName;
  loading?: boolean;
  disabled?: boolean;
  type?: "button" | "submit";
}>(), {
  tone: "quiet",
  icon: undefined,
  loading: false,
  disabled: false,
  type: "button",
});
</script>

<template>
  <button
    class="cm-button"
    :class="`cm-button--${tone}`"
    :type="type"
    :disabled="disabled || loading"
  >
    <FolioIcon
      v-if="loading || icon"
      :class="{ 'cm-button__spinner': loading }"
      :name="loading ? 'refresh' : icon!"
      :size="17"
      :stroke-width="1.8"
    />
    <slot />
  </button>
</template>
