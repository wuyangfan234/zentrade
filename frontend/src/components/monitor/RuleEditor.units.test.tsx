// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { MonitorRule } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { RuleEditor } from './RuleEditor'

const saveRule = vi.fn(async (_rule: unknown) => ({ ok: true }))
const ruleOptions = {
  types: [
    { key: 'signal', label: '信号' }, { key: 'price', label: '价格/涨跌' },
    { key: 'volume_delta', label: '轮询放量' },
  ],
  scopes: [{ key: 'all', label: '全市场' }], severities: [],
  threshold_fields: [
    { key: 'close', label: '收盘价' }, { key: 'volume', label: '成交量 (手)' },
    { key: 'amount', label: '成交额 (元)' }, { key: 'rsi_14', label: 'RSI14' },
    { key: 'consecutive_limit_ups', label: '连板数' },
    { key: 'consecutive_limit_downs', label: '跌停连板' },
    { key: 'limit_up_price', label: '涨停价' }, { key: 'limit_down_price', label: '跌停价' },
    { key: 'deviate_3d', label: '3日偏离' },
    ...['turnover_rate', 'total_shares', 'float_shares', 'market_cap', 'float_market_cap', 'deviate_10d', 'deviate_30d'].map(key => ({ key, label: key })),
    { key: 'ext_custom_limit_ups', label: '自定义指标' },
  ],
  operators: ['>', '>=', '<', '<=', '==', '!='], logics: [],
  builtin_signals: [{ key: 'signal_limit_up', label: '涨停' }],
}
vi.mock('@/lib/api', () => ({ api: {
  monitorRuleOptions: async () => ruleOptions,
  screenerStrategies: async () => ({ presets: [] }),
  watchlistList: async () => ({ items: [] }), watchlistGroups: async () => ({ groups: [] }),
  customSignalsList: async () => ({ signals: [] }),
  monitorRuleSave: (r: unknown) => saveRule(r),
}, genRuleId: () => 'test-volume-rule' }))
vi.mock('@/lib/useSharedQueries', () => ({ usePreferences: () => ({ data: {} }), useQuoteStatus: () => ({ data: { interval_s: 60 } }) }))
let root: Root
let host: HTMLDivElement
let qc: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div'); document.body.appendChild(host)
  root = createRoot(host)
  qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  qc.setQueryData(QK.monitorRuleOptions, ruleOptions)
  qc.setQueryData(QK.customSignals, { signals: [] })
  saveRule.mockClear()
})
afterEach(() => { act(() => root.unmount()); host.remove(); qc.clear() })
async function render(preset: Partial<MonitorRule>, simple = false, rule: MonitorRule | null = null) {
  await act(async () => {
    root.render(<QueryClientProvider client={qc}><RuleEditor rule={rule} preset={{ type: 'volume_delta', scope: 'all', metric: 'volume', threshold_amount: 1000000, ...preset }} simple={simple} onClose={() => {}} /></QueryClientProvider>)
  })
}
function inputFor(label: string) {
  const input = [...host.querySelectorAll('label')].find(x => x.textContent?.includes(label))?.querySelector('input')
  expect(input, `应存在输入框：${label}`).toBeTruthy()
  return input!
}
function threshold() { return inputFor('单轮放量阈值') }
async function change(input: HTMLInputElement, value: string) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value)
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
}
async function click(text: string) {
  const button = [...host.querySelectorAll('button')].find(x => x.textContent?.trim() === text)
  expect(button, `应存在按钮：${text}`).toBeTruthy()
  await act(async () => { button!.click() })
}
async function save() { await click('保存') }

it('按原始股数编辑并保存，默认值与后端一致，不再显示手数或集合竞价过滤', async () => {
  await render({ metric: 'volume' })
  expect(host.textContent).toContain('按股数')
  expect(host.textContent).not.toMatch(/手数|集合竞价|ST \/ 风险警示/)
  expect(threshold().value).toBe('500000')
  await change(threshold(), '12001')
  await save()
  expect(saveRule).toHaveBeenCalledWith(expect.objectContaining({ threshold_volume: 12001 }))
})
it('美元阈值按原值编辑保存，不作万元缩放或取整', async () => {
  await render({ metric: 'amount', threshold_amount: 1000000.25 })
  expect(host.textContent).toContain('USD')
  expect(host.textContent).not.toContain('万元')
  expect(threshold().value).toBe('1000000.25')
  await change(threshold(), '125001.75')
  await save()
  expect(saveRule).toHaveBeenCalledWith(expect.objectContaining({ threshold_amount: 125001.75 }))
})
it('切换股数和金额口径保留各自原始阈值', async () => {
  await render({ threshold_volume: 12001, threshold_amount: 125001.75 })
  await click('按金额')
  expect(threshold().value).toBe('125001.75')
  await click('按股数')
  expect(threshold().value).toBe('12001')
  await save()
  expect(saveRule).toHaveBeenCalledWith(expect.objectContaining({ metric: 'volume', threshold_volume: 12001, threshold_amount: 125001.75 }))
})
it('基础过滤保留美元价格成交额，清除隐藏旧市值过滤以免保存后继续生效', async () => {
  await render({ basic_filter: { price_min: 5.25, market_cap_min: 123456789, amount_min: 23456789, float_cap_min: 34567890, float_cap_max: 987654321, exclude_st: false } })
  expect(inputFor('股价区间 (USD)').value).toBe('5.25')
  expect(inputFor('当日成交额下限 (USD)').value).toBe('23456789')
  expect(host.textContent).not.toMatch(/总市值下限|流通市值[上下]限/)
  await change(inputFor('当日成交额下限'), '23456789.5')
  await save()
  expect(saveRule).toHaveBeenCalledWith(expect.objectContaining({ basic_filter: { price_min: 5.25, amount_min: 23456789.5, exclude_st: false } }))
})
it.each([
  ['volume', '股数'], ['amount', 'USD'],
] as const)('%s 非法阈值被阻止并使用美股单位提示', async (metric, unit) => {
  await render({ metric })
  await change(threshold(), '0')
  await save()
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  expect(saveRule).not.toHaveBeenCalled()
  expect(host.textContent).toContain('必须是 ≥1')
  expect(host.textContent).toContain(unit)
  expect(host.textContent).not.toMatch(/手数|万元/)
})
it.each([false, true])('完整/极简模式 simple=%s 不提供连板涨跌停字段，但保留真实指标和自定义字段', async simple => {
  await render({ type: 'price', conditions: [{ field: 'volume', op: '>', value: 100 }] }, simple)
  const labels = [...host.querySelectorAll('select option')].map(x => x.textContent)
  for (const label of ['连板数', '跌停连板', '涨停价', '跌停价', '3日偏离']) expect(labels).not.toContain(label)
  for (const key of ['turnover_rate', 'total_shares', 'float_shares', 'market_cap', 'float_market_cap', 'deviate_10d', 'deviate_30d']) expect(labels).not.toContain(key)
  expect(labels).toEqual(expect.arrayContaining(['成交量 (股)', '成交额 (USD)', 'RSI14', '自定义指标']))
  expect(host.textContent).not.toMatch(/连板梯队|涨停|跌停|异动监控|板块监控/)
})
it.each(['consecutive_limit_ups', 'turnover_rate', 'total_shares', 'float_shares', 'market_cap', 'float_market_cap', 'deviate_3d', 'deviate_10d', 'deviate_30d'])('编辑旧 %s 条件保留原值并提示不可用', async field => {
  const conditions = [{ field, op: '>=', value: 2 }]
  const rule: MonitorRule = {
    id: 'legacy-rule', name: '旧规则', type: 'price', asset_type: 'stock', enabled: true,
    scope: 'all', symbols: [], conditions, logic: 'and', cooldown_seconds: 300,
    severity: 'info', message: '', direction: 'entry',
  }
  await render({}, false, rule)
  expect(host.textContent).toContain('旧条件（美股不可用）')
  const selected = host.querySelector<HTMLOptionElement>(`option[value="${field}"]`)!
  expect(selected.selected).toBe(true)
  expect(selected.disabled).toBe(true)
  await save()
  expect(saveRule).toHaveBeenCalledWith(expect.objectContaining({ id: 'legacy-rule', conditions }))
})
it('编辑已有放量规则不覆盖用户阈值和兼容过滤字段', async () => {
  const rule: MonitorRule = {
    id: 'saved-volume', name: '自定义放量', type: 'volume_delta', enabled: true,
    scope: 'all', symbols: [], conditions: [], logic: 'or', cooldown_seconds: 600,
    severity: 'warn', message: '原备注', direction: 'entry', metric: 'volume', threshold_volume: 9000,
    threshold_amount: 125001.75, basic_filter: { exclude_st: true, amount_min: null },
  }
  await render({}, false, rule)
  expect(threshold().value).toBe('9000')
  await save()
  expect(saveRule).toHaveBeenCalledWith(expect.objectContaining(rule))
})

it.each(['signal_limit_up', 'signal_deviate_3d', 'signal_deviate_10d', 'signal_deviate_30d'])('旧信号 %s 有明确不可用提示，仅在用户移除时删除', async field => {
  await render({ type: 'signal', conditions: [
    { field, op: 'truth' },
    { field: 'signal_volume_surge', op: 'truth' },
  ] })
  expect(host.textContent).toContain('旧条件（美股不可用）')
  expect(host.textContent).not.toContain('涨停')
  await click('移除旧条件')
  await save()
  expect(saveRule).toHaveBeenCalledWith(expect.objectContaining({ conditions: [{ field: 'signal_volume_surge', op: 'truth' }] }))
})
