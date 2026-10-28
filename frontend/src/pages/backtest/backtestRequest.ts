import { strategyBacktestQuery, type StrategyBacktestParams } from '@/lib/api'
import { tryReconnect } from '@/lib/backtestTask'

/**
 * Keep the existing SSE singleton (progress/cancel/retry), but publish the full
 * request through its reconnect contract instead of its legacy field whitelist.
 * Both a new run and a reload use the exact same serialized query.
 */
export function startBacktest(params: StrategyBacktestParams): void {
  localStorage.setItem('backtest_reconnect', strategyBacktestQuery(params))
  tryReconnect()
}
