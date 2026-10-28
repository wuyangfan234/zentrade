// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { expect, it, vi } from 'vitest'
import { EvidenceDesk as Opportunities } from './EvidenceDesk'

const short = {
  id: 'opp-short-xyz', symbol: 'XYZ', state: 'active', side: 'short', setup_type: 'strategy:phantom_4h_short',
  entry_zone: [99, 100], invalidation: 104, target_zone: [88, 89.76], evidence: {}, reward_risk: 2.3, max_holding_days: 7,
  state_reason: '次交易日开盘确认有效',
}
const long = { ...short, id: 'opp-long-aaa', symbol: 'AAA', side: undefined, setup_type: 'breakout', entry_zone: [52, 53],
  invalidation: 50, target_zone: [58, 59] }

const conflict = { ...short, id: 'conflict', symbol: 'CONFLICT', state: 'watch',
  evidence: { decision: { sources: ['phantom_4h', 'phantom_4h_short'], direction_conflict: true, resonance: 0, score: 0 } },
  state_reason: '多空来源冲突：只观察' }

vi.mock('@tanstack/react-query', () => ({
  useQuery: () => ({ isLoading: false, data: { as_of: '2026-10-02', gate: 'green', cards: [short, long], watch: [conflict] } }),
  useMutation: () => ({ mutate: () => {}, isPending: false }),
  useQueryClient: () => ({ invalidateQueries: () => {} }),
}))
vi.mock('@/components/PageHeader', () => ({ PageHeader: () => null }))
vi.mock('@/components/EmptyState', () => ({ EmptyState: ({ title }: { title: string }) => <div>{title}</div> }))

it('labels short cards with direction and short wording; old cards without side read as long', async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  const host = document.createElement('div')
  const root = createRoot(host)
  try {
    await act(async () => root.render(<Opportunities />))
    const s = host.querySelector('[data-card="XYZ"]') as HTMLElement
    const l = host.querySelector('[data-card="AAA"]') as HTMLElement
    expect(s.querySelector('[data-side]')?.textContent).toBe('做空')
    expect(s.textContent).toContain('卖空区')
    expect(s.textContent).toContain('收盘站上确认，下一交易日开盘回补')
    const disagreement = host.querySelector('[data-card="CONFLICT"]') as HTMLElement
    expect(disagreement.textContent).toContain('多空分歧')
    expect(disagreement.textContent).not.toContain('个来源共振')
    expect(l.querySelector('[data-side]')?.textContent).toBe('做多')
    expect(l.textContent).toContain('入场区')
  } finally {
    await act(async () => root.unmount())
    vi.unstubAllGlobals()
  }
})
