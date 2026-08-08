<script setup lang="ts">
import { computed } from "vue";

const props = withDefaults(defineProps<{
  status: string;
  label?: string;
}>(), {
  label: "",
});

const tone = computed(() => {
  const status = props.status.toLowerCase();
  if (["running", "idle", "enabled", "completed"].includes(status)) return "running";
  if (["collecting", "queued"].includes(status)) return "collecting";
  if (["reporting"].includes(status)) return "reporting";
  if (["error", "failed", "offline", "interrupted"].includes(status)) return "error";
  return "paused";
});

const resolvedLabel = computed(() => {
  if (props.label) return props.label;
  const labels: Record<string, string> = {
    running: "监控运行中",
    idle: "监控运行中",
    enabled: "监控运行中",
    paused: "已暂停",
    stopped: "已暂停",
    collecting: "正在采集",
    queued: "等待执行",
    reporting: "正在生成报告",
    completed: "已完成",
    error: "采集失败",
    failed: "采集失败",
    offline: "后端离线",
    interrupted: "任务已中断",
  };
  return labels[props.status.toLowerCase()] || props.status;
});
</script>

<template>
  <span class="cm-status-chip" :class="`cm-status-chip--${tone}`">
    {{ resolvedLabel }}
  </span>
</template>
