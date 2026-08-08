import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import test from "node:test";
import ts from "typescript";

const sourcePath = resolve(import.meta.dirname, "../src/pages/guadao_audit_shared.ts");
const pagePath = resolve(import.meta.dirname, "../src/pages/account_profit.vue");
const source = await readFile(sourcePath, "utf8");
const pageSource = await readFile(pagePath, "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ESNext,
    target: ts.ScriptTarget.ES2022,
  },
}).outputText;
const shared = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);

test("default form preserves the fixed July baseline and Beijing end time", () => {
  const form = shared.defaultAuditForm(new Date("2026-08-04T04:05:00.000Z"));
  assert.equal(form.dateFrom, "2026-07-19T15:20");
  assert.equal(form.dateTo, "2026-08-04T12:05");
  assert.equal(form.openingWallet, "2502.92");
  assert.equal(form.openingRealValue, "1755.474");
});

test("preset normalization keeps exact decimals and five stable accounts", () => {
  const preset = shared.normalizeAuditPreset({
    preset: {
      startAt: "2026-07-19T15:20:00+08:00",
      endAt: "2026-07-28T23:50:00+08:00",
      openingWalletCny: "2502.92",
      openingRealValueCny: "1755.474",
      accounts: Array.from({ length: 5 }, (_, index) => ({
        accountId: `account-${index + 1}`,
        accountName: `账号 ${index + 1}`,
        steamId64: `7656119${index}`,
      })),
    },
  });
  assert.equal(preset.dateFrom, "2026-07-19T15:20");
  assert.equal(preset.dateTo, "2026-07-28T23:50");
  assert.equal(preset.openingRealValue, "1755.474");
  assert.equal(preset.accounts.length, 5);
  assert.equal(preset.accounts[0].label, "账号 1");
});

test("form validation rejects reversed windows and run request is explicitly read-only", () => {
  const invalid = {
    dateFrom: "2026-07-20T00:00",
    dateTo: "2026-07-19T23:59",
    openingWallet: "2502.92",
    openingRealValue: "1755.474",
    accountIds: ["account-1"],
  };
  assert.deepEqual(shared.validateAuditForm(invalid), ["结束时间必须晚于开始时间"]);

  const request = shared.buildAuditRunRequest({ ...invalid, dateTo: "2026-07-21T00:00" });
  assert.equal(request.dateFrom, "2026-07-20T00:00:00+08:00");
  assert.equal(request.dateTo, "2026-07-21T00:00:00+08:00");
  assert.equal(request.startAt, request.dateFrom);
  assert.equal(request.initialBalance, "2502.92");
  assert.equal(request.initialRealValue, "1755.474");
  assert.deepEqual(request.steamAccountIds, ["account-1"]);
  assert.deepEqual(request.accountIds, ["account-1"]);
  assert.equal(request.expectedAccountCount, 1);
  assert.equal(request.mode, "strict_official");
  assert.equal(request.readOnly, true);
});

test("queued response stays active and inconclusive terminal never becomes passed", () => {
  const queued = shared.normalizeAuditRun({ requestId: "gda-1", status: "queued" });
  assert.equal(queued.status, "queued");
  assert.equal(queued.verdict, null);
  assert.equal(shared.isActiveAuditStatus(queued.status), true);

  const terminal = shared.normalizeAuditRun({
    requestId: "gda-1",
    status: "inconclusive",
    stage: "summary",
    progress: { done: 4, total: 5 },
    gaps: [{ source: "steam", code: "coverage_gap", message: "第 5 个账号历史未覆盖" }],
  });
  assert.equal(terminal.status, "completed");
  assert.equal(terminal.verdict, "inconclusive");
  assert.equal(terminal.progress.percent, 100);
  assert.equal(terminal.evidenceGaps[0].source, "steam");
  assert.equal(shared.verdictCopy(terminal.verdict).label, "INCONCLUSIVE");

  const failed = shared.normalizeAuditRun({
    requestId: "gda-2",
    status: "failed",
    error: { source: "c5", code: "remote_unavailable", message: "订单详情不可读", retryable: true, coverageComplete: false },
  });
  assert.equal(failed.status, "failed");
  assert.equal(failed.verdict, null);
  assert.deepEqual(failed.error, {
    source: "c5",
    code: "remote_unavailable",
    message: "订单详情不可读",
    retryable: true,
    coverageComplete: false,
  });
});

test("backend terminal status and decimal row shapes normalize without inventing cents", () => {
  const passed = shared.normalizeAuditRun({
    requestId: "GDA-1",
    status: "passed",
    stage: "finished",
    summary: { evidenceComplete: true, programSalesEqualOfficial: true },
  });
  assert.equal(passed.status, "completed");
  assert.equal(passed.verdict, "passed");
  assert.equal(passed.stage, "summary");
  assert.equal(passed.progress.percent, 100);

  const mismatch = shared.normalizeAuditRun({
    requestId: "GDA-2",
    status: "failed",
    stage: "finished",
    summary: { failures: ["wallet mismatch"] },
    error: null,
  });
  assert.equal(mismatch.status, "completed");
  assert.equal(mismatch.verdict, "failed");

  const steamTable = shared.AUDIT_TABLES.find((table) => table.dataset === "steamSales");
  const officialNet = steamTable.columns.find((column) => column.key === "officialNet");
  assert.match(shared.formatAuditCell({ officialNet: "8.69" }, officialNet), /8\.69/);
  assert.doesNotMatch(shared.formatAuditCell({ officialNet: "8.69" }, officialNet), /0\.08/);

  const completedWithGaps = shared.normalizeAuditRun({
    requestId: "GDA-3",
    status: "completed_with_gaps",
    summary: { evidenceComplete: false, evidenceGaps: ["Steam range incomplete"] },
  });
  assert.equal(completedWithGaps.status, "completed");
  assert.equal(completedWithGaps.verdict, "inconclusive");
  assert.equal(completedWithGaps.evidenceGaps[0].message, "Steam range incomplete");
});

test("five-stage state marks only completed and current stages", () => {
  const run = shared.normalizeAuditRun({ requestId: "gda-2", status: "running", stage: "evidence_matching" });
  assert.deepEqual(
    shared.AUDIT_STAGES.map(({ key }) => shared.stageState(key, run)),
    ["completed", "completed", "completed", "current", "pending"],
  );
});

test("all four datasets normalize independent pagination", () => {
  assert.deepEqual(
    shared.AUDIT_TABLES.map((table) => table.dataset),
    ["steamSales", "rebuyChains", "itemConservation", "wallet"],
  );
  const page = shared.normalizeAuditRows(
    { data: { items: [{ listingId: "100" }, { listingId: "101" }], page: 2, pageSize: 2, totalCount: 5 } },
    "steamSales",
    1,
    50,
  );
  assert.equal(page.rows.length, 2);
  assert.equal(page.page, 2);
  assert.equal(page.total, 5);
  assert.equal(page.hasMore, true);
});

test("evidence gaps remain visible and official amounts use minor units", () => {
  const row = {
    evidenceState: "unverified",
    evidenceMessage: "缺少 Steam receipt",
    officialNetFen: 9033,
  };
  assert.equal(shared.extractEvidenceGaps(row)[0].message, "缺少 Steam receipt");
  assert.equal(shared.evidenceLabel(row.evidenceState), "证据不完整");
  assert.match(shared.formatMoneyFen(row.officialNetFen), /90\.33/);
  assert.equal(shared.extractEvidenceGaps({ verdict: "inconclusive", reason: "history coverage gap" })[0].message, "history coverage gap");
});

test("server export and row URLs encode immutable request ids", () => {
  assert.equal(
    shared.buildAuditExportUrl("gda/one", "markdown"),
    "/api/guadao-audit/runs/gda%2Fone/export?format=markdown",
  );
  assert.equal(
    shared.buildAuditRowsUrl("gda/one", "wallet", 3, 25),
    "/api/guadao-audit/runs/gda%2Fone/rows?section=wallet_discount&page=3&pageSize=25",
  );
});

test("account page keeps the global shell and exposes no transaction action buttons", () => {
  assert.doesNotMatch(pageSource, /<nav\b/i);
  const buttonBlocks = pageSource.match(/<button\b[\s\S]*?<\/button>/gi) || [];
  for (const button of buttonBlocks) {
    assert.doesNotMatch(button, /买入|购买|上架|补仓|撤单|确认交易/);
  }
  assert.match(pageSource, /\/api\/guadao-audit\/presets/);
  assert.match(pageSource, /\/api\/guadao-audit\/runs/);
  assert.doesNotMatch(pageSource, /\.xlsx|format\s*[:=]\s*["'](?:xlsx|excel)|exportUrl\(["'](?:xlsx|excel)/i);
});
