// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { describe, expect, it, vi } from 'vitest'
import { EvidenceDesk as Opportunities, modeLabel } from './EvidenceDesk'

vi.mock('@tanstack/react-query', () => ({ useQuery: () => ({isLoading:false,data:{as_of:'2026-09-29',gate:'green',cards:[{id:'expired-hzo',symbol:'HZO',state:'expired',setup_type:'breakout',entry_zone:[52,53],invalidation:50,target_zone:[70,72],evidence:{},reward_risk:15,max_holding_days:7,state_reason:'低ATR失效'}],watch:[]}}) }))
vi.mock('@/components/PageHeader',()=>({PageHeader:()=>null}))
vi.mock('@/components/EmptyState',()=>({EmptyState:({title}:{title:string})=><div>{title}</div>}))

it('does not promote expired proposals to the active area',async()=>{
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT',true)
  const host=document.createElement('div'); const root=createRoot(host)
  try {
    await act(async()=>root.render(<Opportunities/>))
    const history=host.querySelector('details[data-terminal-history]') as HTMLDetailsElement
    expect(history).not.toBeNull()
    expect(history.open).toBe(false)
    expect(history.textContent).toContain('HZO')
    expect(host.textContent).toContain('暂无可入场机会')
  } finally {await act(async()=>root.unmount());vi.unstubAllGlobals()}
})

describe('modeLabel', () => {
  it('names v4 as the watchlist rule-set mode, never as legacy v1', () => {
    expect(modeLabel('v4')).toContain('自选 v4')
    expect(modeLabel('v3')).toBe('新尺子 v3.1')
    expect(modeLabel(undefined)).toBe('旧算法 v1')
  })
})
