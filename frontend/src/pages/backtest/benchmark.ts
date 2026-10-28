import type { StrategyBacktestResult } from '@/lib/api'

type BenchmarkInput = Pick<StrategyBacktestResult, 'config' | 'equity_curve' | 'benchmark_curve'>

export function benchmarkView(result: BenchmarkInput) {
  const symbol = result.benchmark_curve?.find(row => row.symbol)?.symbol ?? result.config.benchmark_symbol ?? ''
  const byDate = new Map((result.benchmark_curve ?? []).map(row => [row.date.slice(0, 10), row.close ?? row.value]))
  const values = result.equity_curve.map(row => byDate.get(row.date.slice(0, 10)) ?? null)
  const valid = values.filter((value): value is number => value != null && Number.isFinite(value) && value > 0)
  return {
    symbol,
    label: symbol ? `同期 ${symbol}` : '同期基准（未标识）',
    values,
    hasData: valid.length > 0,
    totalReturn: values.length >= 2 && values[0] != null && values[values.length - 1] != null && values[0]! > 0
      ? values[values.length - 1]! / values[0]! - 1 : null,
  }
}
