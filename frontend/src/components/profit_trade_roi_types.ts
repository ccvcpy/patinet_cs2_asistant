export type ProfitTradeOrderbookLevel = {
  price?: number | null;
  count?: number | null;
};

export type ProfitTradeSteamOrderbook = {
  observedAt?: string | null;
  currencyId?: number | null;
  sellerFloorPrice?: number | null;
  sellerFloorCount?: number | null;
  buyerMaxPrice?: number | null;
  buyerMaxCount?: number | null;
  spreadAmount?: number | null;
  spreadPct?: number | null;
  crossed?: boolean | null;
  sellOrderCountTotal?: number | null;
  buyOrderCountTotal?: number | null;
  sellLevels?: ProfitTradeOrderbookLevel[];
  buyLevels?: ProfitTradeOrderbookLevel[];
};

export type ProfitTradeCrossedListingProbe = {
  checkedAt?: string | null;
  attemptedAt?: string | null;
  status?:
    | "matched"
    | "floor_mismatch"
    | "empty"
    | "no_usable_cny_listing"
    | "rate_limited"
    | "circuit_open"
    | "currency_invalid"
    | "floor_unavailable"
    | "client_unavailable"
    | "unavailable"
    | "error"
    | string;
  expectedSellerFloorPrice?: number | null;
  currencyId?: number | null;
  listingId?: string | null;
  listingSubtotal?: number | null;
  listingFee?: number | null;
  listingTotal?: number | null;
  listingCurrencyId?: number | null;
  priceMatchesFloor?: boolean | null;
  candidateCount?: number | null;
  purchaseAttempted?: boolean;
  circuitCooldownUntil?: string | null;
  httpStatus?: number | null;
  message?: string | null;
};

export type ProfitTradeLinkedTrade = {
  tradeId: number;
  tradeNo?: string | null;
  status?: string | null;
  stepKey?: string | null;
  stepIndex?: number | null;
  progress?: number | null;
  steamBoughtAt?: string | null;
  completedAt?: string | null;
  steamBuyPrice?: number | null;
  c5ListingPrice?: number | null;
  c5SoldNetPrice?: number | null;
  expectedProfit?: number | null;
  realizedProfit?: number | null;
  expectedRoi?: number | null;
  realizedRoi?: number | null;
  error?: string | null;
  createdAt?: string | null;
  manuallyEdited?: boolean;
};

export type ProfitTradeLongBuyOrder = {
  id?: number | null;
  state?: string | null;
  accountId?: string | null;
  steamId?: string | null;
  buyOrderId?: string | null;
  bidPrice?: number | null;
  quantity?: number | null;
  filledQuantity?: number | null;
  remainingQuantity?: number | null;
  standardSafePrice?: number | null;
  aggressiveSafePrice?: number | null;
  worstCaseRoi?: number | null;
  createdAt?: string | null;
  lastCheckedAt?: string | null;
  lastFilledAt?: string | null;
  reason?: string | null;
  replacesOrderId?: number | null;
  replacedByOrderId?: number | null;
};

export type ProfitTradeLongBuyProposal = {
  c5PriceBatch?: number | null;
  c5ExpectedNetPrice?: number | null;
  balanceDiscount?: number | null;
  standardRoi?: number | null;
  aggressiveRoi?: number | null;
  standardSafePrice?: number | null;
  standardSafePriceCents?: number | null;
  aggressiveSafePrice?: number | null;
  aggressiveSafePriceCents?: number | null;
  competitorBuyPrice?: number | null;
  competitorBuyPriceCents?: number | null;
  competitorBuyCount?: number | null;
  competitorBuyStatus?: string | null;
  competitorBuyRoi?: number | null;
  competitorBuyProfit?: number | null;
  excludedOwnBuyPrices?: number[];
  targetPrice?: number | null;
  targetPriceCents?: number | null;
  worstCaseRoi?: number | null;
  quantity?: number | null;
  decision?: string | null;
  eligible?: boolean;
  executionAllowed?: boolean;
  blockedReason?: string | null;
  sourceScanId?: string | null;
  sellerExecutionStatus?: string | null;
  recommendedAction?: string | null;
};

export type ProfitTradeWatchPool = "inventory" | "selection";
export type ProfitTradeHistoryRange = "7d" | "30d" | "90d" | "all";

export type ProfitTradeWatchItem = {
  scanId?: string | null;
  marketHashName: string;
  name?: string | null;
  active?: boolean;
  status?: string | null;
  selectedAt?: string | null;
  nextScanAt?: string | null;
  lastError?: string | null;

  steamBuyPrice?: number | null;
  c5ListingPrice?: number | null;
  c5ExpectedNetPrice?: number | null;
  balanceDiscount?: number | null;
  roiBasis?: number | null;
  expectedProfit?: number | null;
  expectedRoi?: number | null;
  buyOrderReferenceRoi?: number | null;
  buyOrderReferenceProfit?: number | null;
  buyOrderReferenceStatus?: string | null;
  competitorBuyPrice?: number | null;
  competitorBuyRoi?: number | null;
  competitorBuyProfit?: number | null;
  competitorBuyStatus?: string | null;
  excludedOwnBuyPrices?: number[];
  longBuyOrder?: ProfitTradeLongBuyOrder | null;
  longBuyProposal?: ProfitTradeLongBuyProposal | null;
  minRoi?: number | null;
  manualReviewRoi?: number | null;

  inventoryCount?: number | null;
  tradableCount?: number | null;
  c5CurrentSellPrice?: number | null;
  c5OnSaleCount?: number | null;
  c5PurchaseMaxPrice?: number | null;
  c5PurchaseCount?: number | null;
  c5PurchaseSellRatio?: number | null;
  c5MinPurchaseSellRatio?: number | null;
  riskStatus?: string | null;
  riskReason?: string | null;
  executionStatus?: string | null;
  executionStatusCode?: string | null;
  executionReason?: string | null;
  manualExecutableQuantity?: number | null;
  firstSeenAt?: string | null;
  lastObservedAt?: string | null;
  exitedAt?: string | null;
  exitReason?: string | null;
  steamOrderbook?: ProfitTradeSteamOrderbook | null;
  crossedListingProbe?: ProfitTradeCrossedListingProbe | null;
  latestTrade?: ProfitTradeLinkedTrade | null;
};

export type ProfitTradeWatchHistoryItem = ProfitTradeWatchItem & {
  eventType?: string | null;
  observedAt?: string | null;
  scanId?: string | null;
  relatedTrade?: ProfitTradeLinkedTrade | null;
};

export type ProfitTradeWatchSummary = {
  activeItemCount?: number | null;
  tradableQuantity?: number | null;
  currentExpectedProfitTotal?: number | null;
  buyOrderReferenceProfitTotal?: number | null;
  buyOrderReferenceCoveredItems?: number | null;
  buyOrderReferenceEligibleItems?: number | null;
  longBuyActiveOrders?: number | null;
};

export type ProfitTradeListingsCircuit = {
  status?: "closed" | "open";
  isBlocking?: boolean;
  cooldownUntil?: string | null;
  triggerAccountName?: string | null;
  consecutive429Count?: number;
};

export type ProfitTradeWatchPage = {
  items?: ProfitTradeWatchItem[];
  total?: number;
  page?: number;
  pageSize?: number;
  summary?: ProfitTradeWatchSummary;
  listingsCircuit?: ProfitTradeListingsCircuit;
};

export type ProfitTradeHistoryStats = {
  highestRoi?: number | null;
  averageRoi?: number | null;
  roiBasis?: number | null;
  roiBasisMin?: number | null;
  roiBasisMax?: number | null;
  validObservationCount?: number | null;
};

export type ProfitTradeHistoryTrendPoint = {
  observedAt: string;
  expectedRoi: number;
  buyOrderReferenceRoi?: number | null;
  roiBasis?: number | null;
};

export type ProfitTradeHistoryTrend = {
  totalValidPoints: number;
  sampled: boolean;
  points: ProfitTradeHistoryTrendPoint[];
};

export type ProfitTradeHistoryPage = {
  items?: ProfitTradeWatchHistoryItem[];
  total?: number;
  page?: number;
  pageSize?: number;
  stats?: ProfitTradeHistoryStats | null;
  trend?: ProfitTradeHistoryTrend | null;
};

export type ProfitTradeItemSearchResult = {
  marketHashName: string;
  name: string;
};
