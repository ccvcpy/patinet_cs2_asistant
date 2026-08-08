import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";
import ts from "typescript";

const componentsDirectory = resolve(import.meta.dirname, "../src/components");

async function loadTypeScriptFormatter() {
  const source = await readFile(resolve(componentsDirectory, "profit_trade_roi_format.ts"), "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  return import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);
}

const expectedRatioCases = [
  [0.042825, "4.28%"],
  [0.69, "69.00%"],
  [4.2825, "428.25%"],
  [0.0195195195, "1.95%"],
];

for (const [label, loadFormatter] of [
  ["TypeScript source", loadTypeScriptFormatter],
  ["JavaScript mirror", () => import(pathToFileURL(resolve(componentsDirectory, "profit_trade_roi_format.js")).href)],
]) {
  test(`${label} formats Profit Trade raw ratios without guessing units`, async () => {
    const { formatProfitTradeRatio } = await loadFormatter();
    for (const [input, expected] of expectedRatioCases) {
      assert.equal(formatProfitTradeRatio(input), expected);
    }
    assert.equal(formatProfitTradeRatio(null), "—");
    assert.equal(formatProfitTradeRatio(Number.POSITIVE_INFINITY), "—");
  });

  test(`${label} leaves explicit Pct fields in percentage points`, async () => {
    const { formatProfitTradePercentagePoints } = await loadFormatter();
    assert.equal(formatProfitTradePercentagePoints(428.25), "428.25%");
    assert.equal(formatProfitTradePercentagePoints(0.5), "0.50%");
    assert.equal(formatProfitTradePercentagePoints(undefined), "—");
  });
}
