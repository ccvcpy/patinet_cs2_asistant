import { createRouter, createWebHashHistory, type RouteRecordRaw } from "vue-router";

const legacyRoutes: Record<string, string> = {
  account: "/account",
  steam: "/steam",
  guadao: "/guadao",
  "case-ratio": "/case-ratio",
  "profit-trade": "/profit-trade/overview",
  "c5-sweeper": "/c5-sweeper",
};

/**
 * The previous shell wrote hashes such as `#profit-trade`. Normalize those
 * once before Vue Router reads the location, while leaving the new `#/...`
 * addresses and query strings untouched.
 */
export function normalizeLegacyHash(): void {
  const rawHash = window.location.hash.replace(/^#/, "");
  if (!rawHash || rawHash.startsWith("/")) return;

  const [legacyKey, query = ""] = rawHash.split("?", 2);
  const target = legacyRoutes[legacyKey];
  if (!target) return;
  window.history.replaceState(
    window.history.state,
    "",
    `${window.location.pathname}${window.location.search}#${target}${query ? `?${query}` : ""}`,
  );
}

normalizeLegacyHash();

const routes: RouteRecordRaw[] = [
  { path: "/", redirect: "/profit-trade/overview" },
  { path: "/account", component: () => import("./pages/account_profit.vue") },
  { path: "/steam", component: () => import("./pages/steam_balances.vue") },
  { path: "/guadao", component: () => import("./pages/guadao_report.vue") },
  { path: "/case-ratio", component: () => import("./pages/case_ratio_monitor.vue") },
  { path: "/c5-sweeper", component: () => import("./pages/c5_sweeper.vue") },
  {
    path: "/profit-trade",
    component: () => import("./pages/profit_trade_layout.vue"),
    children: [
      { path: "", redirect: { name: "profit-trade-overview" } },
      {
        path: "overview",
        name: "profit-trade-overview",
        component: () => import("./pages/profit_trade.vue"),
      },
      {
        path: "interruptions",
        name: "profit-trade-interruptions",
        component: () => import("./pages/profit_trade_interruptions.vue"),
      },
      {
        path: "logs",
        name: "profit-trade-logs",
        component: () => import("./pages/profit_trade_logs.vue"),
      },
    ],
  },
  { path: "/:pathMatch(.*)*", redirect: "/profit-trade/overview" },
];

const router = createRouter({
  history: createWebHashHistory(),
  routes,
  scrollBehavior(to, from, savedPosition) {
    if (savedPosition) return savedPosition;
    if (to.path !== from.path) return { top: 0 };
    return undefined;
  },
});

export default router;
