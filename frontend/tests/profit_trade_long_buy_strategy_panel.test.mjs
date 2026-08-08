import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { createSSRApp, h } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createServer } from "vite";

const frontendRoot = resolve(import.meta.dirname, "..");
let vite;
let LongBuyStrategyPanel;
let requiresLongBuyConfigConfirmation;
let usesProfitTradeRuntimeToggle;

before(async () => {
  vite = await createServer({
    root: frontendRoot,
    appType: "custom",
    logLevel: "error",
    server: { middlewareMode: true },
  });
  ({ default: LongBuyStrategyPanel } = await vite.ssrLoadModule(
    "/src/components/ProfitTradeLongBuyStrategyPanel.vue",
  ));
  ({ requiresLongBuyConfigConfirmation, usesProfitTradeRuntimeToggle } = await vite.ssrLoadModule(
    "/src/components/profit_trade_long_buy_strategy.ts",
  ));
});

after(async () => {
  await vite?.close();
});

async function renderPanel(config, activeOrderCount = 0) {
  return renderToString(createSSRApp({
    render: () => h(LongBuyStrategyPanel, { config, activeOrderCount }),
  }));
}

const observingConfig = {
  enabled: true,
  allowRealExecution: true,
  longBuyEnabled: true,
  longBuyAllowRealExecution: false,
  minItemValue: 5,
};

test("长期求购策略区展示可切换的四层开关，并将当前组合解释为观察模式", async () => {
  const html = await renderPanel(observingConfig);

  for (const testId of [
    "long-buy-setting-enabled",
    "long-buy-setting-real-execution",
    "long-buy-setting-feature",
    "long-buy-setting-steam-write",
  ]) {
    assert.match(html, new RegExp(`data-testid=\\"${testId}\\"`));
    assert.match(html, new RegExp(`data-testid=\\"${testId.replace("setting", "toggle")}\\"`));
  }
  assert.match(html, /前三项直接切换/);
  assert.doesNotMatch(html, /只读/);
  assert.equal((html.match(/需确认/g) ?? []).length, 1);
  assert.match(html, /长期求购：观察模式/);
  assert.match(html, /不会创建、撤销或改价 Steam 求购/);
  assert.match(html, /当前没有程序管理的长期求购订单/);
  assert.doesNotMatch(html, /真实写入已开放/);
});

test("只有长期 Steam 写入开关需要二次确认", () => {
  assert.equal(requiresLongBuyConfigConfirmation("enabled"), false);
  assert.equal(requiresLongBuyConfigConfirmation("allowRealExecution"), false);
  assert.equal(requiresLongBuyConfigConfirmation("longBuyEnabled"), false);
  assert.equal(requiresLongBuyConfigConfirmation("longBuyAllowRealExecution"), true);
});

test("总功能开关沿用运行时调度入口，其余三项只更新对应配置", () => {
  assert.equal(usesProfitTradeRuntimeToggle("enabled"), true);
  assert.equal(usesProfitTradeRuntimeToggle("allowRealExecution"), false);
  assert.equal(usesProfitTradeRuntimeToggle("longBuyEnabled"), false);
  assert.equal(usesProfitTradeRuntimeToggle("longBuyAllowRealExecution"), false);
});

test("顶部工作台只展示真实执行状态，不再保留重复的紧急关闭按钮", async () => {
  const source = await readFile(
    resolve(frontendRoot, "src/pages/profit_trade_layout.vue"),
    "utf8",
  );

  assert.match(source, /真实执行 \{\{ realExecutionAllowed \? "开放" : "关闭" \}\}/);
  assert.doesNotMatch(source, /紧急关闭真实执行/);
  assert.doesNotMatch(source, /emergencyDisable/);
});

test("四层任一开关不满足时，都不能将长期求购显示为真实写入", async () => {
  const cases = [
    [{ ...observingConfig, enabled: false }, /Profit Trade 总功能已关闭/],
    [{ ...observingConfig, allowRealExecution: false }, /普通真实执行未开放/],
    [{ ...observingConfig, longBuyEnabled: false }, /不产生新方案或 Steam 写入/],
    [{ ...observingConfig, longBuyAllowRealExecution: false }, /长期求购只观察，不写 Steam/],
  ];

  for (const [config, expected] of cases) {
    const html = await renderPanel(config);
    assert.match(html, expected);
    assert.doesNotMatch(html, /真实写入已开放/);
  }
});

test("四层开关全开时才显示长期求购真实写入许可", async () => {
  const html = await renderPanel({ ...observingConfig, longBuyAllowRealExecution: true }, 3);

  assert.match(html, /长期求购：真实写入已开放/);
  assert.match(html, /data-testid="allowed-long-buy-steam-write"/);
  assert.match(html, /四层许可已全部满足/);
  assert.match(html, /当前有 3 笔程序管理的长期求购订单/);
});

test("普通真实执行关闭时，长期求购只保留成交证据，不暗示会直接 C5 上架", async () => {
  const html = await renderPanel({ ...observingConfig, allowRealExecution: false }, 1);

  assert.match(html, /data-testid="blocked-ordinary-purchase"/);
  assert.match(html, /data-testid="blocked-long-buy-c5-followup"/);
  assert.match(html, /普通真实执行已关闭/);
  assert.match(html, /保留成交，等待 C5 执行许可/);
});

test("总功能和普通真实执行切换时，具体动作在允许与禁止两栏之间移动", async () => {
  const enabledHtml = await renderPanel(observingConfig);
  assert.match(enabledHtml, /data-testid="allowed-profit-cycle"/);
  assert.match(enabledHtml, /data-testid="allowed-ordinary-purchase"/);
  assert.match(enabledHtml, /data-testid="allowed-long-buy-scan"/);
  assert.match(enabledHtml, /data-testid="allowed-long-buy-reconcile"/);
  assert.match(enabledHtml, /data-testid="allowed-long-buy-c5-followup"/);
  assert.match(enabledHtml, /data-testid="blocked-long-buy-steam-write"/);

  const realExecutionOffHtml = await renderPanel({
    ...observingConfig,
    allowRealExecution: false,
  });
  assert.match(realExecutionOffHtml, /data-testid="allowed-profit-cycle"/);
  assert.match(realExecutionOffHtml, /data-testid="blocked-ordinary-purchase"/);
  assert.match(realExecutionOffHtml, /data-testid="allowed-long-buy-scan"/);
  assert.match(realExecutionOffHtml, /data-testid="allowed-long-buy-reconcile"/);
  assert.match(realExecutionOffHtml, /data-testid="blocked-long-buy-c5-followup"/);

  const totalOffHtml = await renderPanel({ ...observingConfig, enabled: false });
  for (const actionId of [
    "profit-cycle",
    "ordinary-purchase",
    "long-buy-scan",
    "long-buy-reconcile",
    "long-buy-c5-followup",
    "long-buy-steam-write",
  ]) {
    assert.match(totalOffHtml, new RegExp(`data-testid="blocked-${actionId}"`));
    assert.doesNotMatch(totalOffHtml, new RegExp(`data-testid="allowed-${actionId}"`));
  }
  assert.match(totalOffHtml, /当前没有允许执行的 Profit Trade 动作/);
});

test("关闭新长期求购策略后，已有订单仍展示安全成交核对的例外", async () => {
  const html = await renderPanel({ ...observingConfig, longBuyEnabled: false }, 2);

  assert.match(html, /当前有 2 笔程序管理的长期求购订单/);
  assert.match(html, /data-testid="allowed-long-buy-reconcile"/);
  assert.match(html, /已有订单继续读取官方成交证据/);
});

test("交叉盘口安全护栏按预览图只保留三个职责清晰的分支", async () => {
  const html = await renderPanel(observingConfig);

  assert.match(html, /CrossedBookSafetyRule/);
  assert.match(html, /交叉盘口安全护栏/);
  assert.match(html, /data-testid="crossed-book-existing-unmatched"/);
  assert.match(html, /有旧长期单 \+ 已成交/);
  assert.match(html, /锁可交易老库存 A/);
  assert.match(html, /data-testid="crossed-book-existing-matched"/);
  assert.match(html, /有旧长期单 \+ 未成交/);
  assert.match(html, /不撤、不改、不新建、不直购/);
  assert.match(html, /data-testid="crossed-book-no-existing"/);
  assert.match(html, />无旧长期单</);
  assert.match(html, />原直购链路</);
  assert.doesNotMatch(html, /无旧长期单 · ROI 达标/);
  assert.doesNotMatch(html, /无旧长期单 · ROI 不足/);
  assert.doesNotMatch(html, /只观察，不操作/);
});

test("成交后闭环按预览图展示四步时间线与独立空状态区", async () => {
  const html = await renderPanel(observingConfig);

  assert.match(html, /LongBuyLifecyclePreview/);
  assert.match(html, /Steam 官方成交确认/);
  assert.match(html, /锁可交易老库存 A/);
  assert.match(html, /记录实际 paidTotal/);
  assert.match(html, /C5 上架 A/);
  assert.match(html, /lifecycle-timeline/);
  assert.match(html, /lifecycle-empty-state/);
  assert.match(html, /当前没有程序管理的长期求购订单。/);
});
