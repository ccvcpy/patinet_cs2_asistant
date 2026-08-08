export type RatioBucket = {
  bucket: string;
  lower: number;
  upper: number;
  durationMinutes: number;
  durationLabel: string;
  coveragePct: number;
};

export type RatioThreshold = {
  key: string;
  label: string;
  ratio: number;
  durationLabel: string;
  coveragePct: number;
};

export type TimelineSegment = {
  startedAt: string;
  endedAt: string;
  ratio: number;
  bucket: string;
  durationLabel: string;
  leftPct: number;
  widthPct: number;
};

export type CaseRatioItem = {
  marketHashName: string;
  name: string;
  crateType: string;
  crateTypeLabel: string;
  sampleCount: number;
  okSampleCount: number;
  latestRatio: number;
  latestC5SellPrice: number | null;
  latestSteamListPrice: number | null;
  latestSteamAfterTaxPrice: number | null;
  minRatio: number;
  minRatioDurationLabel: string;
  maxRatio: number;
  maxRatioDurationLabel: string;
  avgRatio: number;
  p50Ratio: number | null;
  p75Ratio: number | null;
  p90Ratio: number | null;
  conservativeMaxListingRatio: number;
  recommendedMaxListingRatio: number;
  aggressiveMaxListingRatio: number;
  effectiveRecommendedMaxListingRatio: number | null;
  selectedReferenceRatio: number | null;
  steamReferenceSource: string | null;
  steamReferenceSourceLabel: string | null;
  steamReferencePrice: number | null;
  sellerFloorPrice: number | null;
  sellerWallListPrice: number | null;
  buyerMaxPrice: number | null;
  steamVolume24h: number | null;
  steamVolume7d: number | null;
  steamAvgDailyVolume7d: number | null;
  liquidityLabel: string | null;
  stddevRatio: number;
  coveragePct: number;
  recommendationScore: number;
  legacySteamMinorUnitCorrectedCount: number;
  buckets: RatioBucket[];
  dominantBuckets: RatioBucket[];
  ratioThresholds: RatioThreshold[];
  timelineSegments: TimelineSegment[];
};

export type CaseRatioReport = {
  generatedAt: string;
  startUtc: string;
  endUtc: string;
  rangeHours: number;
  snapshotCount: number;
  itemCount: number;
  statusCounts: Record<string, number>;
  crateTypeCounts: Record<string, number>;
  crateTypeLabels: Record<string, string>;
  recommendationCrateType: string;
  legacySteamMinorUnitCorrectedCount: number;
  steamLiquidityStatus: string;
  steamLiquidityRefreshedAt: string | null;
  recommendations: CaseRatioItem[];
  items: CaseRatioItem[];
};

export type CaseMonitorJob = {
  jobId: string;
  jobType: "collect" | "report";
  triggerSource: string;
  status: "queued" | "running" | "completed" | "failed" | "interrupted";
  progressCurrent: number;
  progressTotal: number;
  message: string | null;
  parameters: Record<string, unknown>;
  result: Record<string, unknown>;
  error: string | null;
  requestedAt: string;
  startedAt: string | null;
  finishedAt: string | null;
  updatedAt: string;
};

export type CaseMonitorStatus = {
  ok: boolean;
  backend: {
    online: boolean;
    workerAlive: boolean;
    lastError: string | null;
  };
  runtime: {
    enabled: boolean;
    status: string;
    intervalMinutes: number;
    busy: boolean;
    nextRunAt: string | null;
    lastCollectionAt: string | null;
    lastReportAt: string | null;
    lastError: string | null;
    message: string | null;
    startedAt: string | null;
    runningSeconds: number;
    restartRequiresManualResume: boolean;
    lastCollectionResult: {
      savedCount?: number;
      targetCount?: number;
      okCount?: number;
      missingC5Count?: number;
      missingSteamCount?: number;
      statusCounts?: Record<string, number>;
    };
  };
  currentJob: CaseMonitorJob | null;
  latestJob: CaseMonitorJob | null;
  latestReport: {
    available: boolean;
    reportId: string | null;
  };
  generatedAt: string;
};

export type CaseCategoryOption = {
  key: string;
  label: string;
  count: number;
};

export type SelectOption<T extends string | number = string> = {
  value: T;
  label: string;
};
