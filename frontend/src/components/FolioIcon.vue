<script setup lang="ts">
export type FolioIconName =
  | "account" | "wallet" | "report" | "case" | "scan" | "price"
  | "clock" | "refresh" | "play" | "pause" | "link" | "shield"
  | "warning" | "success" | "error" | "settings" | "edit"
  | "calendar" | "lock" | "rocket" | "document" | "circle-dashed"
  | "chevron-up" | "chevron-down" | "chevron-left" | "chevron-right"
  | "search" | "download" | "x" | "info" | "crown" | "bell"
  | "menu" | "grid" | "user" | "sparkles";

withDefaults(defineProps<{
  name: FolioIconName;
  size?: number;
  strokeWidth?: number;
  label?: string;
}>(), { size: 18, strokeWidth: 1.7, label: "" });
</script>

<template>
  <svg
    class="folio-icon"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-linecap="round"
    stroke-linejoin="round"
    :stroke-width="strokeWidth"
    :width="size"
    :height="size"
    :aria-hidden="label ? undefined : true"
    :aria-label="label || undefined"
    role="img"
  >
    <template v-if="name === 'account'">
      <circle cx="12" cy="8" r="3.5"/><path d="M5 20c.6-4 3-6 7-6s6.4 2 7 6"/>
    </template>
    <template v-else-if="name === 'wallet'">
      <path d="M4 7h14a2 2 0 0 1 2 2v10H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h12"/><path d="M16 12h6v5h-6a2.5 2.5 0 0 1 0-5Z"/>
    </template>
    <template v-else-if="name === 'report'">
      <path d="M5 3h14v18H5zM8 8h8M8 12h8M8 16h5"/>
    </template>
    <template v-else-if="name === 'case'">
      <path d="m4 7 8-4 8 4-8 4-8-4Zm0 0v10l8 4 8-4V7M12 11v10"/>
    </template>
    <template v-else-if="name === 'scan'">
      <path d="M4 8V4h4M16 4h4v4M20 16v4h-4M8 20H4v-4M8 12h8"/>
    </template>
    <template v-else-if="name === 'price'">
      <path d="M20 13 13 20 4 11V4h7l9 9Z"/><path d="M8 8h.01"/>
    </template>
    <template v-else-if="name === 'clock'">
      <circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>
    </template>
    <template v-else-if="name === 'refresh'">
      <path d="M20 7v5h-5M4 17v-5h5M6.2 8.5A7 7 0 0 1 18.8 7M5.2 17A7 7 0 0 0 17.8 15.5"/>
    </template>
    <template v-else-if="name === 'play'">
      <path d="m9 6 9 6-9 6V6Z"/>
    </template>
    <template v-else-if="name === 'pause'">
      <path d="M9 7v10M15 7v10"/>
    </template>
    <template v-else-if="name === 'link'">
      <path d="m10 14 4-4m-6.5 7.5-1 1a3.5 3.5 0 0 1-5-5l3-3a3.5 3.5 0 0 1 5 0m7-4 1-1a3.5 3.5 0 0 1 5 5l-3 3a3.5 3.5 0 0 1-5 0"/>
    </template>
    <template v-else-if="name === 'shield'">
      <path d="M12 3 20 6v5c0 5-3.2 8.3-8 10-4.8-1.7-8-5-8-10V6l8-3Z"/><path d="m9 12 2 2 4-4"/>
    </template>
    <template v-else-if="name === 'warning'">
      <path d="m12 3 10 18H2L12 3Z"/><path d="M12 9v5M12 17h.01"/>
    </template>
    <template v-else-if="name === 'success'">
      <circle cx="12" cy="12" r="9"/><path d="m8 12 2.6 2.6L16.5 9"/>
    </template>
    <template v-else-if="name === 'error'">
      <circle cx="12" cy="12" r="9"/><path d="m9 9 6 6m0-6-6 6"/>
    </template>
    <template v-else-if="name === 'edit'">
      <path d="M4 20h4l11-11a2.8 2.8 0 0 0-4-4L4 16v4Z"/><path d="m13.5 6.5 4 4M12 20h8"/>
    </template>
    <template v-else-if="name === 'calendar'">
      <rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v4M17 3v4M3 10h18M8 14h.01M12 14h.01M16 14h.01M8 18h.01M12 18h.01"/>
    </template>
    <template v-else-if="name === 'lock'">
      <rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3M12 14v3"/>
    </template>
    <template v-else-if="name === 'rocket'">
      <path d="M14 5c2.7-2.7 5.2-2 5.2-2s.7 2.5-2 5.2l-5.4 5.4-3.4-3.4L14 5Z"/><path d="m13 12 1 5-3 3-2-5M10 11l-5-1-3 3 5 1M15.5 6.5h.01M5 19c1.3-2.7 3.3-2.7 4-2"/>
    </template>
    <template v-else-if="name === 'document'">
      <path d="M6 3h8l4 4v14H6zM14 3v5h4M9 12h6M9 16h6"/>
    </template>
    <template v-else-if="name === 'circle-dashed'">
      <circle cx="12" cy="12" r="8.5" stroke-dasharray="2.2 2.4"/>
    </template>
    <template v-else-if="name === 'chevron-up'">
      <path d="m6 15 6-6 6 6"/>
    </template>
    <template v-else-if="name === 'chevron-down'">
      <path d="m6 9 6 6 6-6"/>
    </template>
    <template v-else-if="name === 'chevron-left'">
      <path d="m15 18-6-6 6-6"/>
    </template>
    <template v-else-if="name === 'chevron-right'">
      <path d="m9 18 6-6-6-6"/>
    </template>
    <template v-else-if="name === 'search'">
      <circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>
    </template>
    <template v-else-if="name === 'download'">
      <path d="M12 3v12m0 0 5-5m-5 5-5-5M4 19v2h16v-2"/>
    </template>
    <template v-else-if="name === 'x'">
      <path d="m6 6 12 12M18 6 6 18"/>
    </template>
    <template v-else-if="name === 'info'">
      <circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>
    </template>
    <template v-else-if="name === 'crown'">
      <path d="m3 7 4.5 4L12 5l4.5 6L21 7l-2 11H5L3 7Z"/><path d="M5 18h14"/>
    </template>
    <template v-else-if="name === 'bell'">
      <path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/>
    </template>
    <template v-else-if="name === 'menu'">
      <path d="M4 6h16M4 12h16M4 18h16"/>
    </template>
    <template v-else-if="name === 'grid'">
      <rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>
    </template>
    <template v-else-if="name === 'user'">
      <circle cx="12" cy="8" r="3.5"/><path d="M5 21a7 7 0 0 1 14 0"/>
    </template>
    <template v-else-if="name === 'sparkles'">
      <path d="m12 3 1.2 3.8L17 8l-3.8 1.2L12 13l-1.2-3.8L7 8l3.8-1.2L12 3ZM19 14l.7 2.3L22 17l-2.3.7L19 20l-.7-2.3L16 17l2.3-.7L19 14ZM5 13l.7 2.3L8 16l-2.3.7L5 19l-.7-2.3L2 16l2.3-.7L5 13Z"/>
    </template>
    <template v-else>
      <circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1a1.7 1.7 0 0 0 1.9.3A1.7 1.7 0 0 0 10 3V2.8h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z"/>
    </template>
  </svg>
</template>

<style scoped>
.folio-icon{display:block;flex:0 0 auto}
</style>
