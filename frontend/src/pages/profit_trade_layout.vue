<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
import { RouterLink, RouterView } from "vue-router";
import FolioIcon from "../components/FolioIcon.vue";

type SharedDashboard = {
  config?: { allowRealExecution?: boolean };
};

const apiOnline = ref<boolean | null>(null);
const realExecutionAllowed = ref(false);
const busy = ref(false);
const error = ref("");
let statusTimer: ReturnType<typeof setInterval> | null = null;

const autoRunStorageKey = "profitTrade.autoRun.v1";
function readAutoRunState(): boolean {
  try {
    return Boolean(JSON.parse(window.localStorage.getItem(autoRunStorageKey) || "null")?.enabled);
  } catch {
    return false;
  }
}
const autoRunActive = ref(readAutoRunState());

function handleRuntimeState(event: Event): void {
  const detail = (event as CustomEvent<{ active?: boolean }>).detail;
  autoRunActive.value = typeof detail?.active === "boolean" ? detail.active : readAutoRunState();
}

function handleDashboardStatus(event: Event): void {
  const detail = (event as CustomEvent<{ allowRealExecution?: boolean }>).detail;
  if (typeof detail?.allowRealExecution === "boolean") {
    realExecutionAllowed.value = detail.allowRealExecution;
    apiOnline.value = true;
  }
}

async function readError(response: Response): Promise<string> {
  try {
    const body = await response.json() as { error?: string; detail?: string };
    return body.error || body.detail || response.statusText;
  } catch {
    return response.statusText;
  }
}

async function refreshSharedStatus(): Promise<void> {
  try {
    const response = await fetch("/api/profit-trade/dashboard", { cache: "no-store" });
    if (!response.ok) throw new Error(await readError(response));
    const payload = await response.json() as SharedDashboard;
    apiOnline.value = true;
    realExecutionAllowed.value = Boolean(payload.config?.allowRealExecution);
    error.value = "";
  } catch (reason) {
    apiOnline.value = false;
    error.value = reason instanceof Error ? reason.message : String(reason);
  }
}

async function emergencyDisable(): Promise<void> {
  if (!realExecutionAllowed.value || busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    const response = await fetch("/api/profit-trade/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ allowRealExecution: false }),
    });
    if (!response.ok) throw new Error(await readError(response));
    realExecutionAllowed.value = false;
    apiOnline.value = true;
    window.dispatchEvent(new CustomEvent("profit-trade:config-changed"));
  } catch (reason) {
    error.value = `紧急关闭失败：${reason instanceof Error ? reason.message : String(reason)}`;
  } finally {
    busy.value = false;
  }
}

onMounted(() => {
  void refreshSharedStatus();
  statusTimer = setInterval(() => void refreshSharedStatus(), 30_000);
  window.addEventListener("profit-trade:runtime-state", handleRuntimeState);
  window.addEventListener("profit-trade:dashboard-status", handleDashboardStatus);
});

onUnmounted(() => {
  if (statusTimer !== null) clearInterval(statusTimer);
  window.removeEventListener("profit-trade:runtime-state", handleRuntimeState);
  window.removeEventListener("profit-trade:dashboard-status", handleDashboardStatus);
});
</script>

<template>
  <div class="profit-trade-workspace">
    <header class="profit-workspace-bar">
      <div class="profit-workspace-brand">
        <span class="profit-workspace-mark"><FolioIcon name="scan" :size="18" /></span>
        <div>
          <strong>Profit Trade</strong>
          <small>搬砖做 T 运营台</small>
        </div>
      </div>

      <nav class="profit-subnav" aria-label="Profit Trade 页面">
        <RouterLink to="/profit-trade/overview">总览</RouterLink>
        <RouterLink to="/profit-trade/interruptions">中断追踪</RouterLink>
        <RouterLink to="/profit-trade/logs">实时日志</RouterLink>
      </nav>

      <div class="profit-runtime-strip" aria-label="Profit Trade 运行状态">
        <span :class="['runtime-dot', apiOnline === true ? 'online' : apiOnline === false ? 'offline' : 'unknown']">
          API {{ apiOnline === true ? "在线" : apiOnline === false ? "离线" : "检查中" }}
        </span>
        <span :class="['runtime-dot', realExecutionAllowed ? 'danger' : 'safe']">
          真实执行 {{ realExecutionAllowed ? "开放" : "关闭" }}
        </span>
        <span :class="['runtime-dot', autoRunActive ? 'online' : 'unknown']">
          浏览器循环 {{ autoRunActive ? "运行中" : "未运行" }}
        </span>
        <button
          class="emergency-stop"
          type="button"
          :disabled="busy || !realExecutionAllowed"
          @click="emergencyDisable"
        >
          <FolioIcon name="shield" :size="15" />
          {{ busy ? "正在关闭" : "紧急关闭真实执行" }}
        </button>
      </div>
    </header>

    <p v-if="error" class="profit-layout-error">状态检查失败：{{ error }}</p>

    <RouterView v-slot="{ Component }">
      <KeepAlive>
        <component :is="Component" />
      </KeepAlive>
    </RouterView>
  </div>
</template>

<style scoped>
.profit-trade-workspace{min-width:1120px;background:#f4f6f2;min-height:calc(100vh - 57px)}
.profit-workspace-bar{position:sticky;top:57px;z-index:9;display:grid;grid-template-columns:auto auto 1fr;gap:24px;align-items:center;padding:11px max(20px,calc((100vw - 1280px)/2));border-bottom:1px solid #dde3dc;background:rgba(250,251,248,.97);box-shadow:0 5px 18px rgba(28,57,43,.045);backdrop-filter:blur(10px)}
.profit-workspace-brand{display:flex;gap:10px;align-items:center;color:#17201c;white-space:nowrap}.profit-workspace-brand>div{display:grid}.profit-workspace-brand small{color:#6f7872;font-size:11px}.profit-workspace-mark{display:grid;place-items:center;width:34px;height:34px;border-radius:8px;color:#fff;background:#236a4c}
.profit-subnav{display:flex;gap:4px;padding:3px;border-radius:9px;background:#edf1ec}.profit-subnav a{padding:7px 13px;border-radius:7px;color:#627068;text-decoration:none;font-size:13px;font-weight:650}.profit-subnav a.router-link-active{color:#174a36;background:#fff;box-shadow:0 1px 5px rgba(20,59,46,.09)}
.profit-runtime-strip{display:flex;justify-content:flex-end;align-items:center;gap:8px;min-width:0}.runtime-dot{display:inline-flex;align-items:center;gap:6px;padding:5px 8px;border:1px solid #dfe5df;border-radius:999px;color:#66716b;background:#fff;font-size:11px;white-space:nowrap}.runtime-dot::before{content:"";width:6px;height:6px;border-radius:50%;background:#a3aaa6}.runtime-dot.online::before{background:#2f805b}.runtime-dot.offline::before,.runtime-dot.danger::before{background:#b64c42}.runtime-dot.safe::before{background:#2f805b}
.emergency-stop{display:inline-flex;align-items:center;gap:6px;min-height:31px;padding:5px 10px;border:1px solid #d7a8a2;border-radius:7px;color:#8d332c;background:#fff7f5;font-size:12px;font-weight:650}.emergency-stop:disabled{color:#8a918d;border-color:#dfe3df;background:#f5f6f4}.profit-layout-error{width:min(1280px,calc(100vw - 40px));margin:10px auto 0;padding:8px 11px;border:1px solid #e4b4ae;border-radius:7px;color:#8c382f;background:#fff7f5;font-size:12px}
</style>
