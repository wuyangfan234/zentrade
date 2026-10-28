/**
 * 个股日K信息条（StockInfoBar Row 2）的指标自定义配置。
 *
 * 与自选列表列配置同源：复用 list-columns 的通用列模型与合并/序列化底座，
 * 仅做纯 localStorage 同步持久化（无后端双写）。个股预览弹窗与回测成交K线
 * Modal 共用同一份配置。
 */

import { storage } from '@/lib/storage'
import {
  buildExtColumnsParam as buildExtColumnsParamBase,
  mergeColumns as mergeColumnsBase,
  serializeColumns as serializeColumnsBase,
  type ColumnConfig,
  type ColumnGroup,
} from '@/lib/list-columns'

export type { ColumnConfig, ColumnGroup }

// ===== 内置指标注册表 =====

export const BUILTIN_INFO_FIELDS: ColumnConfig[] = [
  // 成交
  { id: 'builtin:volume', source: { type: 'builtin', key: 'volume' }, label: '成交量', visible: false, align: 'left' },
  { id: 'builtin:amplitude', source: { type: 'builtin', key: 'amplitude' }, label: '振幅', visible: false, align: 'left' },
  // 行情
  { id: 'builtin:open', source: { type: 'builtin', key: 'open' }, label: '开盘', visible: false, align: 'left' },
  { id: 'builtin:high', source: { type: 'builtin', key: 'high' }, label: '最高', visible: false, align: 'left' },
  { id: 'builtin:low', source: { type: 'builtin', key: 'low' }, label: '最低', visible: false, align: 'left' },
]

export const INFO_GROUPS: ColumnGroup[] = [
  { id: 'volume', label: '成交', icon: '📊', keys: ['volume', 'amplitude'] },
  { id: 'quote', label: '行情', icon: '📈', keys: ['open', 'high', 'low'] },
]

// ===== localStorage 持久化 =====

/** 加载信息条指标配置：localStorage → 默认值，自动补齐新增默认项。 */
export function loadInfoFields(): ColumnConfig[] {
  const saved = storage.stockInfoBarFields.get([]) as ColumnConfig[]
  if (saved.length === 0) return [...BUILTIN_INFO_FIELDS]
  return mergeFields(saved, BUILTIN_INFO_FIELDS)
}

/** 保存信息条指标配置到 localStorage。 */
export function saveInfoFields(columns: ColumnConfig[]): void {
  storage.stockInfoBarFields.set(serializeFields(columns))
}

/** 序列化（此处无 pinned/action 列，直接用底座默认实现）。 */
function serializeFields(columns: ColumnConfig[]): ColumnConfig[] {
  return serializeColumnsBase(columns)
}

/** 从信息条字段配置中提取 ext 列参数（逗号分隔 config_id.field_name），用于 klineDaily 接口。 */
export function buildInfoExtColumnsParam(columns: ColumnConfig[]): string {
  return buildExtColumnsParamBase(columns)
}

/** 合并用户保存的配置与默认配置。 */
function mergeFields(saved: ColumnConfig[], defaults: ColumnConfig[]): ColumnConfig[] {
  // 无固定列，传入空的 pinnedFirstIds 跳过「代码置顶」逻辑
  return mergeColumnsBase(saved, defaults, { pinnedFirstIds: [] })
}
