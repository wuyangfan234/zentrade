/**
 * 集中管理所有 React Query key。
 *
 * - 新增查询只需在此加一行，所有消费方自动引用。
 * - SSE invalidation 基于 SSE_INVALIDATE_PREFIXES 列表，新增 key 无需改 useQuoteStream。
 */

// ===== Query Key 工厂 =====

export const QK = {
  // 全局 / 共享 (Layout 预取)
  capabilities:   ['capabilities'] as const,
  settings:       ['settings'] as const,
  version:        ['version'] as const,
  preferences:    ['preferences'] as const,
  dataSources:    ['data-sources'] as const,
  capabilityMatrix: ['capability-matrix'] as const,
  setupChecklist: ['setup-checklist'] as const,
  quoteStatus:    ['quote-status'] as const,
  quoteInterval:  ['quote-interval'] as const,
  overviewMarket: (asOf?: string) => ['overview-market', asOf ?? 'latest'] as const,
  indexQuotes:    ['index-quotes'] as const,

  // Watchlist
  watchlist:            ['watchlist'] as const,
  watchlistGroups:      ['watchlist-groups'] as const,
  // 不用 watchlist- 前缀: 图标与行情无关, 不随 SSE quotes_updated 失效
  stockLogos:           ['stock-logos'] as const,
  watchlistQuotes:      ['watchlist-quotes'] as const,
  watchlistEnriched:    (ext?: string) => ['watchlist-enriched', ext] as const,
  // 不用 watchlist- 前缀: 日K历史盘中几乎不变, 若被 SSE quotes_updated 高频失效
  // (expert 1s) 会导致全自选日K每秒重拉, staleTime 形同虚设。
  // 刷新点: staleTime 过期 + Watchlist 增删自选/改蜡烛天数时的手动失效;
  // 当日最后一根蜡烛由 Watchlist 用 enriched 实时 OHLC 前端修补 (零额外请求)。
  watchlistKlineBatch:  (symbols: string) => ['kline-batch', symbols] as const,
  // 不用 watchlist- 前缀: 避免被 SSE quotes_updated 高频失效(expert 1s/pro 2s)
  // 导致每次都拉 TickFlow 触限流。分时图用固定 refetchInterval 刷新即可。
  minuteBatch:          (symbols: string) => ['minute-batch', symbols] as const,
  instrumentSearch:     (q: string, assetTypes?: string) => ['instrument-search', q, assetTypes ?? 'stock'] as const,

  // Screener
  screener:             ['screener'] as const,
  screenerStrategies:   (assetType: string = 'stock', timeframe: '1d' | '1m' | 'all' = '1d') => ['screener-strategies', assetType, timeframe] as const,
  screenerCachedSummary: ['screener-cached', 'summary'] as const,
  screenerCachedResult: (strategyId: string, asOf?: string, ext?: string) => ['screener-cached', 'strategy', strategyId, asOf ?? '', ext ?? ''] as const,
  screenerCached:       (asOf?: string, ext?: string) => ['screener-cached', 'all', asOf ?? '', ext ?? ''] as const,
  screenerKlineBatch:   (symbols: string) => ['screener-kline-batch', symbols] as const,
  marketSnapshot:       ['market-snapshot'] as const,

  // Backtest
  backtestStatus:       ['backtest-status'] as const,
  backtestCandidates:   ['backtest-candidates'] as const,
  factorColumns:        ['backtest-factor-columns'] as const,
  factorLibrary:        (assetType: string) => ['factors-library', assetType] as const,
  miningRuns:           ['backtest-mining-runs'] as const,
  miningAvailability:   (assetType: string, profile: string, start: string, end: string) =>
                          ['backtest-mining-availability', assetType, profile, start, end] as const,
  miningRun:            (id: string) => ['backtest-mining-run', id] as const,
  miningResult:         (id: string) => ['backtest-mining-result', id] as const,
  miningConfig:         ['backtest-mining-config'] as const,
  researchCandidates:  ['research-candidates'] as const,
  strategyLinkOptions: (assetType?: 'stock' | 'etf') => assetType
    ? ['strategy-link-options', assetType] as const
    : ['strategy-link-options'] as const,
  strategyDetail:       (id: string) => ['strategy-detail', id] as const,

  // Data / Pipeline
  dataStatus:           ['data-status'] as const,
  pipelineJobs:         ['pipeline-jobs'] as const,
  // 列表查询必须带 limit (侧栏 1 条 / 数据页 15 条各自缓存); 失效仍用上面的前缀键一次刷新全部
  pipelineJobsList:     (limit: number) => ['pipeline-jobs', limit] as const,
  pipelineJob:          (id: string) => ['pipeline-job', id] as const,
  extData:              ['ext-data'] as const,
  extDataRows:          (id: string, date?: string, limit?: number, columns?: string) => ['ext-data-rows', id, date, limit, columns] as const,
  dimensionMembers:     (id: string, field: string, value: string, date?: string) => ['dimension-members', id, field, value, date] as const,
  dimensionIntraday:    (id: string, field: string, value: string, date?: string) => ['dimension-intraday', id, field, value, date] as const,
  analysisMenus:        ['analysis-menus'] as const,
  // 0.40.0 盘面情报 (不用 watchlist- / quote 前缀: 自己按时段轮询, 不随 SSE 行情失效)
  intelToday:           ['intel-today'] as const,
  analysisMenu:         (id: string) => ['analysis-menu', id] as const,

  // Kline
  kline:                (symbol: string, start: string, end: string, extColumns?: string) =>
                           ['kline', symbol, start, end, extColumns ?? ''] as const,
  klineLatest:          (symbol: string) => ['kline-latest', symbol] as const,
  stockLevels:          (symbol: string, days?: number) => ['stock-levels', symbol, days ?? 120] as const,
  klineMinute:          (symbol: string, date: string) =>
                             ['kline-minute', symbol, date] as const,
  klineMinuteRange:     (symbol: string, days: number) =>
                             ['kline-minute-range', symbol, days] as const,
  stockNews:            (symbol: string) => ['stock-news', symbol] as const,
  newsSummary:          (symbols: string) => ['news-summary', symbols] as const,
  newsStatus:           ['news-status'] as const,
  aiTasks:              ['ai-tasks'] as const,
  aiModels:             ['ai-models'] as const,
  apiKeys:              ['api-keys'] as const,
  qverisUsage:          ['qveris-usage'] as const,
  qverisOptions:        (symbol: string) => ['qveris-options', symbol] as const,
  qverisEstimates:      (symbol: string) => ['qveris-estimates', symbol] as const,
  qverisTranscript:     (symbol: string, quarter: string) => ['qveris-transcript', symbol, quarter] as const,

  // Schema
  extDataSchemaAll:     ['ext-data-schema-all'] as const,
  tableSchema:          (table: string) => ['table-schema', table] as const,

  // Custom Signals
  customSignals:        ['custom-signals'] as const,
  customSignalsOptions: ['custom-signals-options'] as const,

  // Monitor (监控规则 + 触发记录)
  monitorRules:         ['monitor-rules'] as const,
  monitorRuleOptions:   ['monitor-rule-options'] as const,
  lots:                 ['lots'] as const,
  lotsKline:            (symbols: string) => ['lots-kline', symbols] as const,

  // 模拟盘 (多账户: 键按账户隔离; paperAll 作账户无关失效前缀)
  paperAll:             ['paper'] as const,
  paperAccounts:        ['paper', 'accounts'] as const,
  paperOverview:        (acc: string) => ['paper', 'overview', acc] as const,
  paperOrders:          (acc: string) => ['paper', 'orders', acc] as const,
  paperTrades:          (acc: string) => ['paper', 'trades', acc] as const,
  paperNav:             (acc: string) => ['paper', 'nav', acc] as const,
  paperStats:           (acc: string) => ['paper', 'stats', acc] as const,
  paperCompare:         ['paper', 'compare'] as const,
  paperAutoRules:       (acc: string) => ['paper', 'auto-rules', acc] as const,
  alerts:               (source?: string) => ['alerts', source ?? ''] as const,

  // 扩展表 schema 清单 (板块切换的资金流列选择器等)
  extSchemaAll:         ['ext-schema-all'] as const,
} as const

// ===== SSE 应该 invalidate 的 key 前缀列表 =====
// 新增需要 SSE 推送的查询，只需在此加一行
//
// 注意: 策略页 (screener-cached) 不在此列表 —— 行情刷新时策略结果不变
// (非监控策略读盘后静态缓存, 监控策略由独立的 strategy_results_updated 事件在
// 重算完成后刷新)。若加入 'screener', 会导致每个行情 tick 双重刷新策略页,
// 且在 monitor "重算" 窗口内读到空结果, 造成策略列表闪烁 (变 0 → 空失效 → 又出现)。

export const SSE_INVALIDATE_PREFIXES = [
  // 精确前缀: 只命中自选页的实时数据 (quotes/enriched)。不能用宽泛的 'watchlist' ——
  // 会误伤 ['watchlist'] (自选列表) 和 ['watchlist-groups'] (分组配置, 只随手动操作变化)。
  // 旧设置里的 'watchlist' 单开关由 useQuoteStream 兼容读取。
  'watchlist-quotes',
  'watchlist-enriched',
  'quote-status',
  'index-quotes',
  'overview-market',
  // 概念/行业分析的全市场快照 (#419): 后端读最新日内缓存, 行情更新后需重取,
  // 否则停留页面时板块聚合数值一直停留旧值。仅两页打开时才实际发请求。
  'market-snapshot',
] as const
