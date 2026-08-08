<script setup lang="ts">
import { computed, ref } from "vue";
import { formatProfitTradeRatio } from "./profit_trade_roi_format";
import type {
  ProfitTradeSteamOrderbook,
  ProfitTradeWatchItem,
  ProfitTradeWatchPool,
} from "./profit_trade_roi_types";

const props = withDefaults(defineProps<{
  row: ProfitTradeWatchItem;
  pool?: ProfitTradeWatchPool;
  listingsCooling?: boolean;
  manualExecutionDisabledReason?: string;
}>(), {
  pool: "inventory",
  listingsCooling: false,
  manualExecutionDisabledReason: "",
});

const emit = defineEmits<{
  "open-history": [row: ProfitTradeWatchItem];
  "remove-selection": [row: ProfitTradeWatchItem];
  "manual-execute": [row: ProfitTradeWatchItem];
}>();

const depthOpen = ref(false);
const isSelection = computed(() => props.pool === "selection");
const hasSavedDepth = computed(() => {
  const snapshot = orderbook(props.row);
  return Boolean((snapshot.sellLevels?.length || 0) + (snapshot.buyLevels?.length || 0));
});
const longBuyOrder = computed(() => props.row.longBuyOrder || null);
const longBuyProposal = computed(() => props.row.longBuyProposal || null);
const excludedOwnBuyPrices = computed(() => {
  const values = props.row.excludedOwnBuyPrices
    || longBuyProposal.value?.excludedOwnBuyPrices
    || [];
  return values.filter((value) => typeof value === "number" && Number.isFinite(value));
});
const hasNonPositiveSellerRoi = computed(() => (
  typeof props.row.expectedRoi === "number" && props.row.expectedRoi <= 0
));
const retainedByLongBuyOrder = computed(() => (
  !isSelection.value
  && Boolean(longBuyOrder.value)
  && hasNonPositiveSellerRoi.value
));
const retainedByEligibleLongBuyProposal = computed(() => (
  !isSelection.value
  && !longBuyOrder.value
  && longBuyProposal.value?.eligible === true
  && hasNonPositiveSellerRoi.value
));
const longBuyRetentionReason = computed(() => {
  if (retainedByLongBuyOrder.value) {
    return "当前卖盘 ROI 已不大于 0，但因仍有程序管理的长期求购而保留在观察区。";
  }
  if (retainedByEligibleLongBuyProposal.value) {
    return "当前卖盘 ROI 已不大于 0，但本轮长期求购拟建方案仍合格，故保留在观察区持续观察。";
  }
  return null;
});

type C5PurchaseGapExplanation = {
  formula: string;
  detail: string;
  tooltip: string;
};

function money(value?: number | null): string {
  return typeof value === "number" && Number.isFinite(value) ? `¥${value.toFixed(2)}` : "—";
}

function pct(value?: number | null): string {
  return formatProfitTradeRatio(value);
}

function time(value?: string | null): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleString("zh-CN", { hour12: false });
}

function orderbook(row: ProfitTradeWatchItem): ProfitTradeSteamOrderbook {
  return row.steamOrderbook || {
    sellerFloorPrice: row.steamBuyPrice ?? null,
    sellLevels: [],
    buyLevels: [],
  };
}

function compactListingId(value?: string | null): string {
  const text = String(value || "");
  return text.length > 18 ? `${text.slice(0, 8)}…${text.slice(-6)}` : text;
}

function compactOrderId(value?: string | null): string {
  const text = String(value || "");
  if (!text) return "待同步";
  return text.length > 22 ? `${text.slice(0, 10)}…${text.slice(-7)}` : text;
}

function longBuyStateLabel(value?: string | null): string {
  const labels: Record<string, string> = {
    creating: "创建结果核对中",
    active: "长期求购中",
    partial: "已部分成交",
    cancel_pending: "撤单终态核对中",
    filled: "已全部成交",
    auto_cancelled: "Steam 自动撤单",
    cancelled: "已安全撤单",
    terminal_uncertain: "终态不确定",
    failed: "创建失败",
  };
  return labels[value || ""] || value || "状态待同步";
}

function longBuyStateClass(value?: string | null): string {
  if (["active", "partial"].includes(value || "")) return "active";
  if (["creating", "cancel_pending"].includes(value || "")) return "pending";
  if (value === "terminal_uncertain") return "uncertain";
  return "terminal";
}

function longBuyDecisionLabel(value?: string | null): string {
  const labels: Record<string, string> = {
    standard_no_competitor: "无有效外部求购，按标准安全价排队",
    standard_safe_price: "标准安全价已取得足够价格优势",
    aggressive_competitor_advantage: "在激进 ROI 硬底线内取得竞争优势",
    standard_low_queue: "无法安全超过最高求购，按标准价低位排队",
  };
  return labels[value || ""] || value || "等待完整行情";
}

function listingProbeText(row: ProfitTradeWatchItem): string {
  const probe = row.crossedListingProbe;
  if (!probe) return "本次观察尚未记录具体 listing 证据";
  const listingId = compactListingId(probe.listingId);
  if (probe.status === "matched") {
    return `找到 listing ${listingId} · 买家支付 ${money(probe.listingTotal)} · 与卖一一致`;
  }
  if (probe.status === "floor_mismatch") {
    return `找到 listing ${listingId} · 实际 ${money(probe.listingTotal)} · 与卖一不一致`;
  }
  if (probe.status === "empty") return "未找到公开 listing · 更像幽灵卖盘或尚未传播";
  if (probe.status === "no_usable_cny_listing") return "有 listing 返回，但没有可验证的人民币同物品卖单";
  if (probe.status === "rate_limited") return "查询具体 listing 返回 429 · 未重试，已进入冷却";
  if (probe.status === "circuit_open") return "listings 正在冷却 · 本次没有额外请求";
  return probe.message || "本次未取得可验证的 listing 证据";
}

function listingProbeClass(row: ProfitTradeWatchItem): string {
  const status = row.crossedListingProbe?.status || "";
  if (status === "matched") return "matched";
  if (status === "floor_mismatch") return "mismatch";
  return "unresolved";
}

function finiteNumber(value?: number | null): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function c5PurchaseSellRatio(row: ProfitTradeWatchItem): number | null {
  const savedRatio = finiteNumber(row.c5PurchaseSellRatio);
  if (savedRatio !== null) return savedRatio;
  const purchasePrice = finiteNumber(row.c5PurchaseMaxPrice);
  const sellPrice = finiteNumber(row.c5CurrentSellPrice);
  if (purchasePrice !== null && sellPrice !== null && sellPrice > 0) {
    return purchasePrice / sellPrice;
  }
  const rawReason = row.executionReason || row.riskReason || "";
  const match = rawReason.match(/purchase\/sell ratio\s+([\d.]+)/i);
  if (!match) return null;
  const parsed = Number(match[1]);
  return Number.isFinite(parsed) ? parsed : null;
}

function c5MinimumPurchaseSellRatio(row: ProfitTradeWatchItem): number | null {
  const savedRatio = finiteNumber(row.c5MinPurchaseSellRatio);
  if (savedRatio !== null) return savedRatio;
  const rawReason = row.executionReason || row.riskReason || "";
  const match = rawReason.match(/purchase\/sell ratio\s+[\d.]+\s*<\s*([\d.]+)/i);
  if (!match) return null;
  const parsed = Number(match[1]);
  return Number.isFinite(parsed) ? parsed : null;
}

function c5PurchaseGapExplanation(row: ProfitTradeWatchItem): C5PurchaseGapExplanation | null {
  const rawReason = row.executionReason || row.riskReason || "";
  if (
    row.riskStatus !== "blocked_c5_purchase_price_gap"
    && !/purchase\/sell ratio/i.test(rawReason)
  ) return null;

  const purchasePrice = finiteNumber(row.c5PurchaseMaxPrice);
  const sellPrice = finiteNumber(row.c5CurrentSellPrice);
  const actualRatio = c5PurchaseSellRatio(row);
  const minimumRatio = c5MinimumPurchaseSellRatio(row);
  const formula = purchasePrice !== null && sellPrice !== null && actualRatio !== null
    ? `最高求购 ${money(purchasePrice)} ÷ 当前在售价 ${money(sellPrice)} = ${pct(actualRatio)}`
    : actualRatio !== null
      ? `C5 最高求购 / 当前在售价 = ${pct(actualRatio)}`
      : "C5 最高求购价与当前在售价的比例未达到要求";
  const threshold = minimumRatio !== null ? pct(minimumRatio) : "系统最低要求";
  return {
    formula,
    detail: `低于最低要求 ${threshold}，买卖价差过大，当前求购承接不足。`,
    tooltip: `C5 求购承接不足：${formula}，低于最低要求 ${threshold}`,
  };
}

const c5RiskExplanation = computed(() => c5PurchaseGapExplanation(props.row));
const generalReason = computed(() => {
  if (c5RiskExplanation.value) return "";
  return props.row.executionReason || props.row.riskReason || props.row.exitReason || props.row.lastError || "";
});
const displayedManualExecutionDisabledReason = computed(() => {
  const disabledReason = props.manualExecutionDisabledReason;
  if (!c5RiskExplanation.value) return disabledReason;
  if (
    !disabledReason
    || disabledReason === props.row.executionReason
    || disabledReason === props.row.riskReason
    || /purchase\/sell ratio/i.test(disabledReason)
  ) return c5RiskExplanation.value.tooltip;
  return disabledReason;
});

function stateLabel(row: ProfitTradeWatchItem): string {
  if (isSelection.value) {
    const labels: Record<string, string> = {
      pending_first_scan: "等待首次观察",
      observed: "已观察",
      price_unavailable: "暂时无价格",
      scan_failed: "读取失败",
      paused: "已暂停",
      removed: "已移出",
    };
    return labels[row.status || ""] || "仅研究";
  }
  if (row.active === false) return "已退出观察池";
  if (
    props.listingsCooling
    && ["listings_cooldown", "listings_probe_ready", "executable", "eligible"].includes(row.executionStatus || "")
  ) return "达到门槛 · listings 冷却";
  const labels: Record<string, string> = {
    listings_cooldown: "达到执行门槛",
    listings_probe_ready: "达到执行门槛",
    executable: "达到执行门槛",
    eligible: "达到执行门槛",
    observe_only: "仅观察，不执行",
    blocked: "风控阻断",
    manual_review: "异常 ROI，需人工",
    below_min_roi: "低于自动门槛，可人工执行",
    below_min_item_value: "低于最低商品价值 · 仅维护已有长期求购",
    asset_unavailable: "暂无可执行资产",
    exited: "已退出观察池",
  };
  const status = row.executionStatusCode || row.executionStatus || "observe_only";
  return labels[status] || row.executionStatus || "仅观察，不执行";
}

function stateClass(row: ProfitTradeWatchItem): string {
  if (isSelection.value) {
    if (["scan_failed", "price_unavailable"].includes(row.status || "")) return "blocked";
    if (["paused", "removed"].includes(row.status || "")) return "exited";
    if (row.status === "pending_first_scan") return "observe";
    return "ready";
  }
  if (row.active === false) return "exited";
  if (
    props.listingsCooling
    && ["listings_cooldown", "listings_probe_ready", "executable", "eligible"].includes(row.executionStatus || "")
  ) return "cooldown";
  if (["listings_cooldown", "listings_probe_ready", "executable", "eligible"].includes(row.executionStatus || "")) return "ready";
  if (["blocked", "manual_review"].includes(row.executionStatus || "")) return "blocked";
  return "observe";
}

function referenceHint(row: ProfitTradeWatchItem): string | null {
  const labels: Record<string, string> = {
    missing_buy_book: "暂无有效最高求购参考",
    currency_invalid: "求购参考币种不可用",
    crossed_possible_stale: "求购参考盘口交叉，可能滞后",
    c5_price_unavailable: "C5 价格暂不可用",
    snapshot_expired: "求购参考快照已过期",
  };
  return labels[row.buyOrderReferenceStatus || ""] || null;
}

function tradeStateLabel(status?: string | null): string {
  const labels: Record<string, string> = {
    completed: "已完成",
    c5_listed: "已买入，C5 在售",
    listing_c5: "已买入，正在 C5 上架",
    steam_bought: "已买入，准备 C5 上架",
    buying: "正在买入",
    locked: "执行中，已锁定 A",
    audited: "已转化为流水，执行中",
    candidate: "已转化为流水，执行中",
    manual_required: "执行中断，需人工处理",
    failed: "执行失败",
    cancelled: "已取消",
  };
  return labels[status || ""] || status || "执行状态待同步";
}
</script>

<template>
  <article :class="['watch-card', { 'selection-card': isSelection }]">
    <div class="card-head">
      <div>
        <strong>{{ row.name || row.marketHashName }}</strong>
        <small v-if="row.name && row.name !== row.marketHashName">{{ row.marketHashName }}</small>
      </div>
      <span :class="['watch-state', stateClass(row)]">{{ stateLabel(row) }}</span>
    </div>

    <p v-if="isSelection" class="research-note">仅研究 · 不要求库存 · 不创建流水</p>
    <p
      v-else-if="listingsCooling && ['listings_cooldown', 'listings_probe_ready', 'executable', 'eligible'].includes(row.executionStatus || '')"
      class="card-cooldown"
    >
      当前行情仍符合执行门槛；Steam listings 处于冷却，真实执行前会重新校验并安全改走求购，不会沿用旧价格。
    </p>

    <div class="price-line">
      <dl>
        <dt>Steam 买入</dt>
        <dd>{{ money(orderbook(row).sellerFloorPrice ?? row.steamBuyPrice) }}</dd>
        <small class="steam-highest-bid">
          Steam 最高求购 {{ money(orderbook(row).buyerMaxPrice) }}
          <template v-if="orderbook(row).buyerMaxCount != null"> · {{ orderbook(row).buyerMaxCount }} 件</template>
        </small>
        <span v-if="orderbook(row).crossed" class="crossed-badge">盘口交叉 · 可能滞后</span>
        <div
          v-if="orderbook(row).crossed"
          :class="['listing-probe', listingProbeClass(row)]"
        >
          <strong>Listing 证据</strong>
          <span>{{ listingProbeText(row) }}</span>
        </div>
      </dl>
      <i>→</i>
      <dl>
        <dt>C5 竞争挂价</dt>
        <dd>{{ money(row.c5ListingPrice) }}</dd>
      </dl>
      <i>→</i>
      <dl>
        <dt>C5 预计到手</dt>
        <dd>{{ money(row.c5ExpectedNetPrice) }}</dd>
      </dl>
    </div>

    <div class="roi-summary">
      <div>
        <small>当前 ROI</small>
        <strong>{{ pct(row.expectedRoi) }}</strong>
        <span>预计收益 {{ money(row.expectedProfit) }}</span>
      </div>
      <div class="buy-reference">
        <small>求购参考 ROI</small>
        <strong>{{ pct(row.buyOrderReferenceRoi) }}</strong>
        <span>参考收益 {{ money(row.buyOrderReferenceProfit) }}</span>
      </div>
    </div>
    <p v-if="referenceHint(row)" class="reference-hint">{{ referenceHint(row) }}</p>
    <p v-if="longBuyRetentionReason" class="long-buy-retained">{{ longBuyRetentionReason }}</p>

    <section
      v-if="longBuyOrder && !isSelection"
      :class="['long-buy-panel', `is-${longBuyStateClass(longBuyOrder.state)}`]"
      aria-label="程序管理的 Steam 长期求购"
    >
      <header>
        <div>
          <small>程序管理的 Steam 长期求购</small>
          <strong>{{ longBuyStateLabel(longBuyOrder.state) }}</strong>
        </div>
        <span>{{ money(longBuyOrder.bidPrice) }} · {{ longBuyOrder.remainingQuantity ?? 0 }} 件待成交</span>
      </header>
      <div class="long-buy-grid">
        <dl>
          <dt>我方求购价</dt>
          <dd>{{ money(longBuyOrder.bidPrice) }}</dd>
          <small>最坏 ROI {{ pct(longBuyOrder.worstCaseRoi) }}</small>
        </dl>
        <dl>
          <dt>外部最高求购</dt>
          <dd>{{ money(row.competitorBuyPrice ?? longBuyProposal?.competitorBuyPrice) }}</dd>
          <small>排除自价后 ROI {{ pct(row.competitorBuyRoi ?? longBuyProposal?.competitorBuyRoi) }}</small>
        </dl>
        <dl>
          <dt>成交进度</dt>
          <dd>{{ longBuyOrder.filledQuantity ?? 0 }} / {{ longBuyOrder.quantity ?? "—" }}</dd>
          <small>剩余 {{ longBuyOrder.remainingQuantity ?? "—" }} 件</small>
        </dl>
        <dl>
          <dt>标准 / 激进安全价</dt>
          <dd>{{ money(longBuyOrder.standardSafePrice) }}</dd>
          <small>激进上限 {{ money(longBuyOrder.aggressiveSafePrice) }}</small>
        </dl>
      </div>
      <p class="long-buy-meta">
        <span>账号 {{ longBuyOrder.accountId || "—" }}</span>
        <span>订单 {{ compactOrderId(longBuyOrder.buyOrderId) }}</span>
        <span>创建 {{ time(longBuyOrder.createdAt) }}</span>
        <span>核对 {{ time(longBuyOrder.lastCheckedAt) }}</span>
      </p>
      <p v-if="excludedOwnBuyPrices.length" class="own-price-exclusion">
        已排除程序自价：{{ excludedOwnBuyPrices.map(money).join("、") }}
      </p>
      <p v-if="longBuyOrder.reason" class="long-buy-warning">{{ longBuyOrder.reason }}</p>
    </section>

    <section
      v-else-if="longBuyProposal && !isSelection"
      class="long-buy-panel is-proposal"
      aria-label="长期求购观察模式提案"
    >
      <header>
        <div>
          <small>长期求购观察模式</small>
          <strong>{{ longBuyProposal.eligible ? "本轮拟建方案" : "本轮不创建" }}</strong>
        </div>
        <span>{{ money(longBuyProposal.targetPrice) }} · {{ longBuyProposal.quantity ?? "—" }} 件</span>
      </header>
      <div class="long-buy-grid">
        <dl>
          <dt>拟定求购价</dt>
          <dd>{{ money(longBuyProposal.targetPrice) }}</dd>
          <small>最坏 ROI {{ pct(longBuyProposal.worstCaseRoi) }}</small>
        </dl>
        <dl>
          <dt>外部最高求购</dt>
          <dd>{{ money(longBuyProposal.competitorBuyPrice) }}</dd>
          <small>ROI {{ pct(longBuyProposal.competitorBuyRoi) }} · 收益 {{ money(longBuyProposal.competitorBuyProfit) }}</small>
        </dl>
        <dl>
          <dt>标准安全价</dt>
          <dd>{{ money(longBuyProposal.standardSafePrice) }}</dd>
          <small>目标 ROI {{ pct(longBuyProposal.standardRoi) }}</small>
        </dl>
        <dl>
          <dt>激进安全上限</dt>
          <dd>{{ money(longBuyProposal.aggressiveSafePrice) }}</dd>
          <small>硬底线 {{ pct(longBuyProposal.aggressiveRoi) }}</small>
        </dl>
      </div>
      <p class="long-buy-decision">{{ longBuyDecisionLabel(longBuyProposal.decision) }}</p>
      <p v-if="excludedOwnBuyPrices.length" class="own-price-exclusion">
        已排除程序自价：{{ excludedOwnBuyPrices.map(money).join("、") }}
      </p>
      <p v-if="longBuyProposal.blockedReason" class="long-buy-warning">
        {{ longBuyProposal.blockedReason }}
      </p>
      <p v-else-if="!longBuyProposal.executionAllowed" class="long-buy-observe-note">
        当前只观察；尚未开启长期求购真实执行。
      </p>
    </section>

    <div class="watch-metrics">
      <dl>
        <dt>ROI 基底</dt>
        <dd>{{ pct(row.roiBasis ?? row.balanceDiscount) }}</dd>
      </dl>
      <dl v-if="!isSelection">
        <dt>可交易 / 库存</dt>
        <dd>{{ row.tradableCount ?? "—" }} / {{ row.inventoryCount ?? "—" }}</dd>
      </dl>
      <dl v-else>
        <dt>选品状态</dt>
        <dd>{{ stateLabel(row) }}</dd>
      </dl>
      <dl>
        <dt>最后观察</dt>
        <dd>{{ time(row.lastObservedAt) }}</dd>
      </dl>
    </div>

    <section v-if="c5RiskExplanation" class="c5-risk-reason">
      <strong>C5 求购承接不足</strong>
      <span>{{ c5RiskExplanation.formula }}</span>
      <small>{{ c5RiskExplanation.detail }}一键执行已禁用。</small>
    </section>
    <p v-else-if="generalReason" class="reason">{{ generalReason }}</p>

    <div v-if="row.latestTrade && !isSelection" class="linked-trade">
      <span><small>最近关联流水</small><strong>{{ tradeStateLabel(row.latestTrade.status) }}</strong></span>
      <span><small>{{ row.latestTrade.tradeNo || "流水号同步中" }}</small><b>Steam {{ money(row.latestTrade.steamBuyPrice) }}</b></span>
    </div>

    <div class="card-actions">
      <button class="link-action" type="button" @click="depthOpen = !depthOpen">
        {{ depthOpen ? "收起买卖前 5 档" : "查看买卖前 5 档" }}
      </button>
      <button class="link-action" type="button" @click="emit('open-history', row)">查看价格与 ROI 历史</button>
      <button v-if="isSelection" class="link-action remove-action" type="button" @click="emit('remove-selection', row)">移出选品观察</button>
      <button
        v-else
        class="manual-execute-action"
        type="button"
        :disabled="Boolean(displayedManualExecutionDisabledReason)"
        :title="displayedManualExecutionDisabledReason || '选择数量并再次确认后执行'"
        @click="emit('manual-execute', row)"
      >
        一键执行
      </button>
    </div>

    <section v-if="depthOpen" class="depth-panel" aria-label="最新 Steam 买卖前五档">
      <p v-if="!hasSavedDepth" class="depth-empty">本次观察未记录买卖前 5 档；展开不会重新请求 Steam。</p>
      <template v-else>
        <div>
          <strong>卖盘前 5 档</strong>
          <p v-for="(level, levelIndex) in orderbook(row).sellLevels || []" :key="`sell-${levelIndex}`">
            <span>{{ levelIndex + 1 }}</span><b>{{ money(level.price) }}</b><small>× {{ level.count ?? "—" }}</small>
          </p>
        </div>
        <div>
          <strong>买盘前 5 档</strong>
          <p v-for="(level, levelIndex) in orderbook(row).buyLevels || []" :key="`buy-${levelIndex}`">
            <span>{{ levelIndex + 1 }}</span><b>{{ money(level.price) }}</b><small>× {{ level.count ?? "—" }}</small>
          </p>
        </div>
      </template>
    </section>
  </article>
</template>

<style scoped>
.watch-card{display:grid;gap:11px;padding:13px;border:1px solid #e0e5df;border-radius:9px;background:#fff}.selection-card{border-color:#d8e8dc;background:linear-gradient(180deg,#fff 0,#fbfdfb 100%)}.card-head{display:flex;justify-content:space-between;gap:12px}.card-head>div{display:grid;min-width:0}.card-head strong,.card-head small{overflow-wrap:anywhere}.card-head small{color:#7a837e}.watch-state{height:max-content;padding:4px 7px;border-radius:999px;font-size:10px;font-weight:700;white-space:nowrap}.watch-state.ready{color:#1d6748;background:#e6f4eb}.watch-state.observe{color:#6d5a25;background:#faf3d9}.watch-state.blocked{color:#913b31;background:#fbe9e6}.watch-state.exited{color:#68716c;background:#edf0ed}.watch-state.cooldown{color:#6d5720;background:#f8edc8}.research-note,.card-cooldown{margin:0;padding:7px 9px;border-left:3px solid #88b89a;color:#38664a;background:#eff8f1;font-size:10px}.card-cooldown{border-color:#cba545;color:#665731;background:#fff8e7}.price-line{display:grid;grid-template-columns:1fr auto 1fr auto 1fr;gap:7px;align-items:center;padding:9px;border-radius:7px;background:#f7f9f6}.price-line i{color:#98a19b;font-style:normal}.price-line dl,.watch-metrics dl{margin:0}.price-line dt,.watch-metrics dt{color:#77817b;font-size:10px}.price-line dd{margin:2px 0 0;font-size:13px;font-weight:700}.steam-highest-bid{display:block;margin-top:4px;color:#68736d;font-size:9px;font-weight:600}.crossed-badge{display:inline-block;margin-top:4px;padding:2px 5px;border-radius:999px;color:#765a18;background:#faefc9;font-size:8px;font-weight:700}.listing-probe{display:grid;gap:2px;margin-top:5px;padding:5px 7px;border-left:2px solid #c9a548;border-radius:4px;color:#65552c;background:#fff8e6;font-size:8px;line-height:1.35}.listing-probe strong{font-size:8px}.listing-probe.matched{border-left-color:#4f9870;color:#315f45;background:#edf7f0}.listing-probe.mismatch{border-left-color:#d1874d;color:#744f2e;background:#fff4e9}.roi-summary{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:8px}.roi-summary>div{display:grid;gap:2px;padding:9px 10px;border-radius:8px;background:#f0f7f3}.roi-summary small,.roi-summary span{color:#6f7f75;font-size:10px}.roi-summary strong{color:#1f704d;font-size:19px;line-height:1.05}.buy-reference{background:#f7f8f5 !important}.buy-reference strong{color:#496858;font-size:14px}.reference-hint{margin:-5px 0 0;color:#756127;font-size:10px}.long-buy-retained{margin:-4px 0 0;padding:6px 8px;border-left:3px solid #8d7350;color:#6c573c;background:#faf4e9;font-size:9px}.long-buy-panel{display:grid;gap:8px;padding:10px;border:1px solid #bcd8c7;border-radius:9px;background:#f1f8f3}.long-buy-panel.is-proposal{border-style:dashed;border-color:#b8cbbc;background:#f8fbf8}.long-buy-panel.is-pending{border-color:#d8c47c;background:#fff9e9}.long-buy-panel.is-uncertain{border-color:#e2a79e;background:#fff3f1}.long-buy-panel>header{display:flex;align-items:flex-start;justify-content:space-between;gap:10px}.long-buy-panel>header>div{display:grid;gap:2px}.long-buy-panel>header small{color:#6e7c73;font-size:8px}.long-buy-panel>header strong{color:#235e43;font-size:12px}.long-buy-panel>header>span{padding:3px 6px;border-radius:999px;color:#285f45;background:#dfeee4;font-size:9px;font-weight:750;white-space:nowrap}.long-buy-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px}.long-buy-grid dl{display:grid;gap:2px;margin:0;padding:7px;border:1px solid rgba(40,96,68,.12);border-radius:6px;background:rgba(255,255,255,.72)}.long-buy-grid dt,.long-buy-grid small{color:#718078;font-size:8px}.long-buy-grid dd{margin:0;color:#234c37;font-size:11px;font-weight:750}.long-buy-meta{display:flex;flex-wrap:wrap;gap:4px 10px;margin:0;color:#68766e;font-size:8px;overflow-wrap:anywhere}.own-price-exclusion,.long-buy-decision,.long-buy-warning,.long-buy-observe-note{margin:0;font-size:9px;line-height:1.45}.own-price-exclusion{color:#2d6749;font-weight:700}.long-buy-decision{color:#4a6253}.long-buy-warning{padding:5px 7px;border-left:2px solid #c78754;color:#704f31;background:#fff7ec}.long-buy-observe-note{color:#6b765f}.watch-metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:9px}.watch-metrics dd{margin:2px 0 0;font-size:11px;font-weight:650;overflow-wrap:anywhere}.reason{margin:0;padding:7px 9px;border-left:3px solid #d4b05b;color:#665731;background:#fbf7e9;font-size:11px}.c5-risk-reason{display:grid;gap:3px;margin:0;padding:8px 10px;border-left:3px solid #c96551;color:#784337;background:#fdf0ed;font-size:11px}.c5-risk-reason strong{color:#973f30;font-size:11px}.c5-risk-reason span{font-weight:700}.c5-risk-reason small{color:#76534b;font-size:10px}.linked-trade{border:1px solid #c9dfd1;border-radius:9px;padding:9px 10px;display:flex;align-items:center;justify-content:space-between;gap:12px;background:#f0f7f3}.linked-trade>span{display:grid;gap:2px}.linked-trade>span:last-child{text-align:right}.linked-trade small{color:#718078;font-size:9px}.linked-trade strong,.linked-trade b{color:#236a4c;font-size:11px}.linked-trade b{font-weight:700}.card-actions{display:flex;flex-wrap:wrap;align-items:center;gap:11px}.link-action{justify-self:start;border:0;padding:0;color:#236a4c;background:transparent;font-size:11px;font-weight:700;text-align:left}.remove-action{color:#7a5148}.manual-execute-action{margin-left:auto;border:1px solid #28714d;border-radius:6px;padding:6px 12px;color:#fff;background:#28714d;font-size:11px;font-weight:750;box-shadow:0 3px 8px rgba(35,106,76,.12)}.manual-execute-action:hover:not(:disabled){background:#1f6544}.manual-execute-action:disabled{cursor:not-allowed;border-color:#c7d2ca;color:#f5f7f5;background:#b7c5ba;box-shadow:none}.depth-panel{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;padding-top:2px}.depth-panel>div{padding:8px;border:1px solid #e1e6e1;border-radius:6px;background:#f8faf8}.depth-panel>div>strong{font-size:10px}.depth-panel p{display:grid;grid-template-columns:18px 1fr auto;gap:6px;margin:5px 0 0;color:#47534d;font-size:9px}.depth-panel p span,.depth-panel p small{color:#78827c}.depth-panel p b{text-align:right}.depth-empty{grid-column:1/-1;display:block !important;margin:0 !important;padding:8px;border:1px dashed #d8ded8;border-radius:6px;color:#748078 !important;font-size:10px !important}.depth-empty::first-line{color:#748078}@media (max-width:560px){.price-line{grid-template-columns:1fr;gap:6px}.price-line i{display:none}.watch-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.depth-panel{grid-template-columns:1fr}.roi-summary{grid-template-columns:1fr}.long-buy-grid{grid-template-columns:1fr}.long-buy-panel>header{display:grid}.long-buy-panel>header>span{width:max-content}.watch-state{white-space:normal;text-align:right}.manual-execute-action{width:100%;margin-left:0}}
</style>
