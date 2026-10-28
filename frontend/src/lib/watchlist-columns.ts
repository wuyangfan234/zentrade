/**
 * 自选列表自定义列配置。
 *
 * 自选页只保留业务内置列、分组和偏好持久化；通用列模型/合并/扩展列参数
 * 来自 list-columns，策略页等其它股票列表可复用同一底座。
 */

import { storage } from '@/lib/storage'
import {
  buildExtColumnsParam as buildExtColumnsParamBase,
  createExtColumn as createExtColumnBase,
  mergeColumns as mergeColumnsBase,
  serializeColumns as serializeColumnsBase,
  type ColumnConfig,
  type ColumnGroup,
  type ColumnSource,
  type ExtColumnDisplayConfig,
  type CandleColumnConfig,
} from '@/lib/list-columns'

export type { ColumnConfig, ColumnGroup, ColumnSource, ExtColumnDisplayConfig, CandleColumnConfig }

// ===== 内置列注册表（与当前硬编码一一对应） =====

export const BUILTIN_COLUMNS: ColumnConfig[] = [
  // 固定列
  { id: 'builtin:symbol', source: { type: 'builtin', key: 'symbol' }, label: '代码/名称', visible: true, pinned: true, align: 'left' },
  // 价格
  { id: 'builtin:price', source: { type: 'builtin', key: 'price' }, label: '现价', visible: true, align: 'center' },
  { id: 'builtin:pct', source: { type: 'builtin', key: 'pct' }, label: '涨跌幅', visible: true, align: 'center' },
  { id: 'builtin:change_amount', source: { type: 'builtin', key: 'change_amount' }, label: '涨跌额', visible: false, align: 'center' },
  { id: 'builtin:amplitude', source: { type: 'builtin', key: 'amplitude' }, label: '振幅', visible: false, align: 'center' },
  { id: 'builtin:open', source: { type: 'builtin', key: 'open' }, label: '今开', visible: false, align: 'center' },
  { id: 'builtin:high', source: { type: 'builtin', key: 'high' }, label: '最高', visible: false, align: 'center' },
  { id: 'builtin:low', source: { type: 'builtin', key: 'low' }, label: '最低', visible: false, align: 'center' },
  { id: 'builtin:prev_close', source: { type: 'builtin', key: 'prev_close' }, label: '昨收', visible: false, align: 'center' },
  // 成交
  { id: 'builtin:volume', source: { type: 'builtin', key: 'volume' }, label: '成交量 (股)', visible: false, align: 'center' },
  { id: 'builtin:amount', source: { type: 'builtin', key: 'amount' }, label: '成交额 (USD)', visible: false, align: 'center' },
  { id: 'builtin:vol_ratio', source: { type: 'builtin', key: 'vol_ratio' }, label: '量比', visible: true, align: 'center' },
  { id: 'builtin:annual_vol', source: { type: 'builtin', key: 'annual_vol' }, label: '年化波动', visible: false, align: 'center' },
  // 均线
  { id: 'builtin:ma5', source: { type: 'builtin', key: 'ma5' }, label: 'MA5', visible: false, align: 'center' },
  { id: 'builtin:ma10', source: { type: 'builtin', key: 'ma10' }, label: 'MA10', visible: false, align: 'center' },
  { id: 'builtin:ma20', source: { type: 'builtin', key: 'ma20' }, label: 'MA20', visible: false, align: 'center' },
  { id: 'builtin:ma60', source: { type: 'builtin', key: 'ma60' }, label: 'MA60', visible: false, align: 'center' },
  // 区间
  { id: 'builtin:high_60d', source: { type: 'builtin', key: 'high_60d' }, label: '60日高', visible: false, align: 'center' },
  { id: 'builtin:low_60d', source: { type: 'builtin', key: 'low_60d' }, label: '60日低', visible: false, align: 'center' },
  // 技术指标
  { id: 'builtin:rsi6', source: { type: 'builtin', key: 'rsi6' }, label: 'RSI6', visible: false, align: 'center' },
  { id: 'builtin:rsi14', source: { type: 'builtin', key: 'rsi14' }, label: 'RSI14', visible: true, align: 'center' },
  { id: 'builtin:rsi24', source: { type: 'builtin', key: 'rsi24' }, label: 'RSI24', visible: false, align: 'center' },
  { id: 'builtin:macd_dif', source: { type: 'builtin', key: 'macd_dif' }, label: 'MACD-DIF', visible: false, align: 'center' },
  { id: 'builtin:macd_dea', source: { type: 'builtin', key: 'macd_dea' }, label: 'MACD-DEA', visible: false, align: 'center' },
  { id: 'builtin:macd_hist', source: { type: 'builtin', key: 'macd_hist' }, label: 'MACD柱', visible: false, align: 'center' },
  { id: 'builtin:kdj_k', source: { type: 'builtin', key: 'kdj_k' }, label: 'KDJ-K', visible: false, align: 'center' },
  { id: 'builtin:kdj_d', source: { type: 'builtin', key: 'kdj_d' }, label: 'KDJ-D', visible: false, align: 'center' },
  { id: 'builtin:kdj_j', source: { type: 'builtin', key: 'kdj_j' }, label: 'KDJ-J', visible: false, align: 'center' },
  { id: 'builtin:boll_upper', source: { type: 'builtin', key: 'boll_upper' }, label: '布林上轨', visible: false, align: 'center' },
  { id: 'builtin:boll_lower', source: { type: 'builtin', key: 'boll_lower' }, label: '布林下轨', visible: false, align: 'center' },
  { id: 'builtin:atr14', source: { type: 'builtin', key: 'atr14' }, label: 'ATR14', visible: false, align: 'center' },
  { id: 'builtin:vol_ma5', source: { type: 'builtin', key: 'vol_ma5' }, label: '量MA5', visible: false, align: 'center' },
  { id: 'builtin:vol_ma10', source: { type: 'builtin', key: 'vol_ma10' }, label: '量MA10', visible: false, align: 'center' },
  // 动量
  { id: 'builtin:momentum_5d', source: { type: 'builtin', key: 'momentum_5d' }, label: '5D 动量', visible: false, align: 'center' },
  { id: 'builtin:momentum_10d', source: { type: 'builtin', key: 'momentum_10d' }, label: '10D 动量', visible: false, align: 'center' },
  { id: 'builtin:momentum_20d', source: { type: 'builtin', key: 'momentum_20d' }, label: '20D 动量', visible: false, align: 'center' },
  { id: 'builtin:momentum_30d', source: { type: 'builtin', key: 'momentum_30d' }, label: '30D 动量', visible: false, align: 'center' },
  { id: 'builtin:momentum_60d', source: { type: 'builtin', key: 'momentum_60d' }, label: '60日动量', visible: true, align: 'center' },
  // 信号 & 图表
  { id: 'builtin:signals', source: { type: 'builtin', key: 'signals' }, label: '信号', visible: true, align: 'center' },
  { id: 'builtin:candle', source: { type: 'builtin', key: 'candle' }, label: '日k', visible: false, align: 'center' },
  { id: 'builtin:intraday', source: { type: 'builtin', key: 'intraday' }, label: '分时', visible: false, align: 'center' },
  // 自选加入信息 (后端读时计算, 不落盘; 默认隐藏)
  { id: 'builtin:added_at', source: { type: 'builtin', key: 'added_at' }, label: '加入日期', visible: false, align: 'center' },
  { id: 'builtin:pct_since_added', source: { type: 'builtin', key: 'pct_since_added' }, label: '加入以来', visible: false, align: 'center' },
]

export const COLUMN_GROUPS: ColumnGroup[] = [
  { id: 'price', label: '价格', icon: '💰', keys: ['price', 'pct', 'change_amount', 'amplitude', 'open', 'high', 'low', 'prev_close'] },
  { id: 'added', label: '自选', icon: '⭐', keys: ['added_at', 'pct_since_added'] },
  { id: 'volume', label: '成交', icon: '📊', keys: ['volume', 'amount', 'vol_ratio', 'annual_vol'] },
  { id: 'ma', label: '均线', icon: '📈', keys: ['ma5', 'ma10', 'ma20', 'ma60'] },
  { id: 'range', label: '区间', icon: '📏', keys: ['high_60d', 'low_60d'] },
  { id: 'tech', label: '技术指标', icon: '🔬', keys: ['rsi6', 'rsi14', 'rsi24', 'macd_dif', 'macd_dea', 'macd_hist', 'kdj_k', 'kdj_d', 'kdj_j', 'boll_upper', 'boll_lower', 'atr14', 'vol_ma5', 'vol_ma10'] },
  { id: 'momentum', label: '动量', icon: '🚀', keys: ['momentum_5d', 'momentum_10d', 'momentum_20d', 'momentum_30d', 'momentum_60d'] },
  { id: 'signal', label: '信号', icon: '📡', keys: ['signals', 'candle', 'intraday'] },
]

// 操作列（始终显示，不参与自定义）
export const ACTION_COLUMN_ID = 'builtin:action'

// ===== localStorage 持久化 =====

/** 序列化列配置（只保存用户可自定义的列，排除 pinned 和 action） */
export function serializeColumns(columns: ColumnConfig[]): ColumnConfig[] {
  return serializeColumnsBase(columns, ACTION_COLUMN_ID)
}

/** 序列化并保存到后端 + localStorage */
export async function saveColumnConfig(columns: ColumnConfig[]): Promise<void> {
  const saveable = serializeColumns(columns)
  // 同时写 localStorage（即时）和后端（持久化）
  storage.watchlistColumns.set(saveable)
  try {
    const { api } = await import('@/lib/api')
    await api.updateWatchlistColumns(saveable)
  } catch {
    // 后端不可用时 localStorage 仍有效
  }
}

/** 加载列配置：优先后端，回退 localStorage，最终用默认值 */
export async function loadColumnConfig(): Promise<ColumnConfig[]> {
  // 1. 尝试从后端加载
  try {
    const { api } = await import('@/lib/api')
    const res = await api.watchlistColumns()
    if (res.columns && res.columns.length > 0) {
      const merged = mergeColumns(res.columns, BUILTIN_COLUMNS)
      // 同步到 localStorage
      storage.watchlistColumns.set(serializeColumns(merged))
      return merged
    }
  } catch {
    // 后端不可用，继续尝试 localStorage
  }

  // 2. 尝试从 localStorage 加载
  const saved = storage.watchlistColumns.get([]) as ColumnConfig[]
  if (saved.length > 0) {
    return mergeColumns(saved, BUILTIN_COLUMNS)
  }

  // 3. 默认值
  return [...BUILTIN_COLUMNS]
}

/** 合并用户保存的列与默认列 */
function mergeColumns(saved: ColumnConfig[], defaults: ColumnConfig[]): ColumnConfig[] {
  return mergeColumnsBase(saved, defaults, { actionColumnId: ACTION_COLUMN_ID })
}

/** 从列配置中提取 ext 列参数，用于后端 enriched 接口 */
export function buildExtColumnsParam(columns: ColumnConfig[]): string {
  return buildExtColumnsParamBase(columns)
}

/** 根据 ext schema 数据创建 ext 列配置 */
export function createExtColumn(
  configId: string,
  configLabel: string,
  fieldName: string,
  fieldLabel?: string,
): ColumnConfig {
  return createExtColumnBase(configId, configLabel, fieldName, fieldLabel)
}
