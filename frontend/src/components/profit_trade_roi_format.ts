/**
 * Profit Trade 的 ROI、余额折扣和 C5 承接比例在 API 中都是原始比例：
 * 0.69 表示 69%，4.2825 表示 428.25%。
 *
 * 不要依据数值大小猜测单位；高 ROI 正是需要明确展示和人工风控的信号。
 */
export function formatProfitTradeRatio(value?: number | null): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(2)}%`;
}

/**
 * API 中名称以 Pct 结尾的字段已经是百分数值，例如 428.25 表示 428.25%。
 */
export function formatProfitTradePercentagePoints(value?: number | null): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `${value.toFixed(2)}%`;
}
