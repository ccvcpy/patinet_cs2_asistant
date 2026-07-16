<script setup lang="ts">
import { RouterLink, RouterView } from "vue-router";
import FolioIcon, { type FolioIconName } from "./components/FolioIcon.vue";

const pages: ReadonlyArray<{
  to: string;
  match?: string;
  label: string;
  icon: FolioIconName;
}> = [
  { to: "/account", label: "挂刀余额核对", icon: "account" as FolioIconName },
  { to: "/steam", label: "Steam余额统计", icon: "wallet" as FolioIconName },
  { to: "/guadao/overview", match: "/guadao", label: "挂刀运营", icon: "report" as FolioIconName },
  { to: "/case-ratio", label: "箱子挂刀比", icon: "case" as FolioIconName },
  { to: "/profit-trade/overview", match: "/profit-trade", label: "搬砖做T", icon: "scan" as FolioIconName },
  { to: "/c5-sweeper", label: "C5扫货", icon: "price" as FolioIconName },
];
</script>

<template>
  <div class="app-shell">
    <nav class="top-nav" aria-label="主导航">
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
