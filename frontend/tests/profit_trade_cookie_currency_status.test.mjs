import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(
  new URL("../src/pages/profit_trade_layout.vue", import.meta.url),
  "utf8",
);

test("cookie health cards expose the probed Steam currency id", () => {
  assert.match(source, /account\.currencyId/);
  assert.match(source, /account\.currencyStatus/);
  assert.match(source, /currencyCheckedAt/);
});

test("cookie health cards distinguish CNY, non-CNY, and unknown currency", () => {
  assert.match(source, /currencyStatus === "cny"/);
  assert.match(source, /currencyStatus === "non_cny"/);
  assert.match(source, /currency-unknown/);
});
