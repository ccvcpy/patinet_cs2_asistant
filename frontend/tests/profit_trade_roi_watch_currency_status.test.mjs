import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(
  new URL("../src/components/ProfitTradeRoiWatchCard.vue", import.meta.url),
  "utf8",
);

test("selection watch card consumes the API selectionStatus field", () => {
  assert.match(source, /row\.selectionStatus/);
});

test("currency mismatch exposes the actual and required Steam currency ids", () => {
  assert.match(source, /实际币种/);
  assert.match(source, /CNY（23）/);
});

test("failed refresh card labels retained prices and ROI basis as an old snapshot", () => {
  assert.match(source, /本轮刷新失败/);
  assert.match(source, /旧行情和旧 ROI 参数/);
  assert.match(source, /本轮目标 ROI 基底/);
});
