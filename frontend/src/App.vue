<script setup lang="ts">
import { computed } from "vue";
import { RouterLink, RouterView, useRoute } from "vue-router";
import FolioIcon, { type FolioIconName } from "./components/FolioIcon.vue";

const route = useRoute();
const isCaseMonitorAtoms = computed(() => route.path === "/case-ratio/components");
const operationsTheme = computed(
  () => !route.path.startsWith("/guadao") && !route.path.startsWith("/profit-trade"),
);

const pages: ReadonlyArray<{
  to: string;
  match?: string;
  label: string;
  icon: FolioIconName;
}> = [
  { to: "/account", label: "挂刀执行-测试工具", icon: "account" as FolioIconName },
  { to: "/steam", label: "Steam余额统计", icon: "wallet" as FolioIconName },
  { to: "/guadao/overview", match: "/guadao", label: "挂刀运营", icon: "report" as FolioIconName },
  { to: "/case-ratio", label: "箱子挂刀比", icon: "case" as FolioIconName },
  { to: "/profit-trade/overview", match: "/profit-trade", label: "搬砖做T", icon: "scan" as FolioIconName },
  { to: "/c5-t-monitor", label: "C5 扫描 & 库存运营", icon: "scan" as FolioIconName },
  { to: "/c5-sweeper", label: "C5扫货", icon: "price" as FolioIconName },
];
</script>

<template>
  <div
    class="app-shell"
    :class="{ 'app-shell--minimal-v2': operationsTheme }"
  >
    <nav
      v-if="!isCaseMonitorAtoms"
      class="top-nav top-nav--unified"
      aria-label="主导航"
    >
      <span class="nav-brand">
        <span class="nav-brand-mark"><FolioIcon name="scan" :size="15" /></span>
        <strong>CS2 交易运营中心</strong>
      </span>
      <RouterLink
        v-for="page in pages"
        :key="page.to"
        :to="page.to"
        class="nav-tab"
        :class="{ active: page.match ? $route.path.startsWith(page.match) : $route.path === page.to }"
      >
        <FolioIcon :name="page.icon" :size="16" />
        {{ page.label }}
      </RouterLink>
    </nav>

    <RouterView />
  </div>
</template>

<style scoped>
.top-nav.top-nav--unified {
  min-height: 58px;
  align-items: center;
  gap: 4px;
  overflow-x: auto;
  padding: 8px clamp(14px, 3vw, 36px);
  border-bottom: 1px solid #dfe6e1;
  background: rgba(255, 255, 255, 0.96);
  box-shadow: 0 2px 8px rgba(24, 50, 34, 0.05);
  backdrop-filter: blur(16px);
  font-family: var(--ops-font);
  scrollbar-width: none;
}

.top-nav.top-nav--unified::-webkit-scrollbar {
  display: none;
}

.top-nav--unified .nav-brand {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 9px;
  margin-right: clamp(8px, 2vw, 24px);
  color: #17211b;
  font-size: 13px;
  white-space: nowrap;
}

.top-nav--unified .nav-brand-mark {
  width: 32px;
  height: 32px;
  display: grid;
  flex: 0 0 auto;
  place-items: center;
  border: 1px solid #08743b;
  border-radius: 6px;
  color: #ffffff;
  background: linear-gradient(145deg, #08743b, #075f32);
  box-shadow: 0 2px 8px rgba(24, 50, 34, 0.05);
}

.top-nav--unified .nav-tab {
  position: relative;
  min-height: 40px;
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 7px;
  border: 1px solid transparent;
  border-radius: 5px;
  color: #66736b;
  background: transparent;
  font-size: 13px;
  font-weight: 600;
  white-space: nowrap;
}

.top-nav--unified .nav-tab:hover {
  color: #075f32;
  border-color: #e1e6e1;
  background: #f7f9f7;
}

.top-nav--unified .nav-tab.active {
  color: #075f32;
  border-color: #cce3d3;
  background: #eaf5ed;
  box-shadow: inset 0 -2px 0 #08743b;
}

.top-nav--unified .nav-tab.active svg {
  color: #08743b;
}

@media (max-width: 760px) {
  .top-nav--unified .nav-brand strong {
    display: none;
  }

  .top-nav--unified .nav-brand {
    margin-right: 2px;
  }
}
</style>
