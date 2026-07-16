<script setup lang="ts">
import { RouterLink, RouterView } from "vue-router";
import FolioIcon, { type FolioIconName } from "../components/FolioIcon.vue";

const links: ReadonlyArray<{ to: string; label: string; icon: FolioIconName }> = [
  { to: "/guadao/overview", label: "运行总览", icon: "scan" },
  { to: "/guadao/operations", label: "流水状态", icon: "report" },
  { to: "/guadao/issues", label: "异常与待处理", icon: "warning" },
  { to: "/guadao/logs", label: "实时日志", icon: "clock" },
  { to: "/guadao/settings", label: "策略设置", icon: "settings" },
  { to: "/guadao/report", label: "挂刀报表", icon: "wallet" },
];
</script>

<template>
  <div class="guadao-workspace">
    <nav class="guadao-subnav" aria-label="挂刀运营页面">
      <RouterLink v-for="link in links" :key="link.to" :to="link.to">
        <FolioIcon :name="link.icon" :size="15" />
        {{ link.label }}
      </RouterLink>
    </nav>
    <RouterView v-slot="{ Component }">
      <KeepAlive>
        <component :is="Component" />
      </KeepAlive>
    </RouterView>
  </div>
</template>

<style scoped>
.guadao-workspace{min-width:1120px;min-height:calc(100vh - 57px);background:var(--folio-bg)}
.guadao-subnav{position:sticky;top:57px;z-index:8;display:flex;justify-content:center;gap:16px;padding:0 max(20px,calc((100vw - 1280px)/2));border-bottom:1px solid var(--folio-line);background:rgba(250,251,248,.96);box-shadow:0 5px 18px rgba(28,57,43,.035);backdrop-filter:blur(10px)}
.guadao-subnav a{position:relative;display:inline-flex;align-items:center;gap:7px;min-height:49px;padding:0 12px;color:#56635c;text-decoration:none;font-size:12px;font-weight:700;white-space:nowrap}
.guadao-subnav a::after{content:"";position:absolute;right:10px;bottom:0;left:10px;height:2px;border-radius:2px;background:transparent}
.guadao-subnav a:hover{color:var(--folio-green-dark)}
.guadao-subnav a.router-link-active{color:var(--folio-green)}
.guadao-subnav a.router-link-active::after{background:var(--folio-green)}
</style>
