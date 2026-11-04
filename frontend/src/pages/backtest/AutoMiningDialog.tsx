import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Sparkles, X } from 'lucide-react'
import { Modal } from '@/components/Modal'
import { toast } from '@/components/Toast'
import { api, type MiningAvailability, type MiningBudgetProfile } from '@/lib/api'
import { startAutoMining } from '@/lib/miningTask'
import { QK } from '@/lib/queryKeys'

const INPUT_CLS = 'h-8 w-full rounded-input border border-border bg-surface px-2.5 text-xs text-foreground focus:border-accent focus:outline-none'

function isoDaysAgo(days: number) {
  const value = new Date()
  value.setDate(value.getDate() - days)
  return value.toISOString().slice(0, 10)
}

const PROFILE_ORDER: MiningBudgetProfile[] = ['exploratory', 'balanced', 'strict']
const PROFILE_LABELS: Record<MiningBudgetProfile, string> = {
  exploratory: '探索',
  balanced: '均衡',
  strict: '严格',
}
const PROFILE_HINTS: Record<MiningBudgetProfile, string> = {
  exploratory: '门槛最低 · 只能存为待定候选',
  balanced: '门槛适中 · 推荐',
  strict: '门槛最高 · 样本要求最严',
}

/** 自动挖掘入口弹窗: L1 全量统计筛选 → 达标因子池 → 嵌套样本外组合挖掘。 */
/** 与后端 auto_mining.SCREEN_GATES 同值: 弹窗按所选档位显示门槛 (旧文案写死均衡档) */
const PROFILE_GATE_TEXT: Record<string, string> = {
  exploratory: '|t| ≥ 1.5 · q ≤ 0.2',
  balanced: '|t| ≥ 2 · q ≤ 0.1',
  strict: '|t| ≥ 2.5 · q ≤ 0.05',
}

export function AutoMiningDialog({
  onClose,
  defaultAssetType = 'stock',
}: {
  onClose: () => void
  defaultAssetType?: 'stock' | 'etf'
}) {
  const navigate = useNavigate()
  const [assetType, setAssetType] = useState<'stock' | 'etf'>(defaultAssetType)
  const [profile, setProfile] = useState<MiningBudgetProfile>('balanced')
  const [profileTouched, setProfileTouched] = useState(false)
  const [start, setStart] = useState(isoDaysAgo(365 * 2))
  const [end, setEnd] = useState(new Date().toISOString().slice(0, 10))

  // 三档可用性: 数据交易日 vs 各档所需; 默认档数据不够时自动落到可用的最高档
  const availabilityArgs = {
    assetType,
    start: start || undefined,
    end: end || undefined,
  } as const
  const exploratoryQuery = useQuery({
    queryKey: QK.miningAvailability(assetType, 'exploratory', start, end),
    queryFn: () => api.miningAvailability({ ...availabilityArgs, budgetProfile: 'exploratory' }),
    staleTime: 30_000,
  })
  const balancedQuery = useQuery({
    queryKey: QK.miningAvailability(assetType, 'balanced', start, end),
    queryFn: () => api.miningAvailability({ ...availabilityArgs, budgetProfile: 'balanced' }),
    staleTime: 30_000,
  })
  const strictQuery = useQuery({
    queryKey: QK.miningAvailability(assetType, 'strict', start, end),
    queryFn: () => api.miningAvailability({ ...availabilityArgs, budgetProfile: 'strict' }),
    staleTime: 30_000,
  })
  const availability: Partial<Record<MiningBudgetProfile, MiningAvailability>> = {
    exploratory: exploratoryQuery.data,
    balanced: balancedQuery.data,
    strict: strictQuery.data,
  }
  const bars = exploratoryQuery.data?.trading_bars ?? balancedQuery.data?.trading_bars ?? strictQuery.data?.trading_bars ?? null
  const currentAvailable = availability[profile]?.eligible ?? null

  useEffect(() => {
    if (profileTouched) return
    // 默认档 (均衡) 数据不足且存在可用档 → 自动降级一次 (用户手动改过则不再干预)。
    // 三档可用性是独立请求: 依赖须覆盖全部三档, 否则 balanced 先到、可用档后到时会错过降级。
    if (availability.balanced && !availability.balanced.eligible) {
      const fallback = PROFILE_ORDER.filter(p => availability[p]?.eligible).pop()
      if (fallback && fallback !== profile) {
        setProfile(fallback)
        toast(`当前数据 ${availability.balanced.trading_bars} 个交易日不够「均衡」档 (需 ${availability.balanced.required_bars})，已自动切到「${PROFILE_LABELS[fallback]}」档`)
      }
    }
  }, [availability.exploratory, availability.balanced, availability.strict, profile, profileTouched])

  const submitAutoMining = () => {
    // L1 全量筛选在后端要跑几分钟: 模块级任务提交后本弹窗立刻关闭,
    // 进度/结果由挖掘页的 RunStatus + SSE 呈现 (含 0 达标原因分布)。提示等真实结果再给。
    void startAutoMining({
      asset_type: assetType,
      budget_profile: profile,
      start: start || null,
      end: end || null,
    }).then(outcome => {
      if (outcome === 'started') toast('自动挖掘已开始 · 进度见「挖掘」页', 'success')
      else if (outcome === 'busy') toast('已有挖掘在跑 · 等它结束再开新的', 'error')
    })
    onClose()
    navigate('/factors?tab=mining')
  }
  const gateText = PROFILE_GATE_TEXT[profile] ?? PROFILE_GATE_TEXT.balanced

  return (
    <Modal
      onClose={onClose}
      labelledBy="auto-mining-title"
      panelClassName="flex max-h-[86vh] w-[92vw] max-w-md flex-col overflow-hidden border border-border bg-surface shadow-xl rounded-card"
    >
      <div className="flex items-start justify-between gap-3 border-b border-border px-4 py-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Sparkles className="h-4 w-4 shrink-0 text-accent" />
            <span id="auto-mining-title" className="text-sm font-semibold text-foreground">自动挖掘达标组合</span>
          </div>
          <p className="mt-1 text-[11px] leading-relaxed text-muted">
            全量筛选 ({gateText}) → 相关性去重 → 组合搜索 → 嵌套样本外验证
          </p>
        </div>
        <button type="button" onClick={onClose} aria-label="关闭" className="inline-flex h-8 w-8 shrink-0 items-center justify-center text-muted transition-colors hover:text-foreground">
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
        <div className="space-y-3">
          <div>
            <div className="mb-1 text-[10px] text-muted">资产</div>
            <div className="inline-flex rounded-btn border border-border bg-surface/80 p-0.5">
              {(['stock', 'etf'] as const).map(value => (
                <button
                  key={value}
                  type="button"
                  onClick={() => setAssetType(value)}
                  className={`h-7 rounded-[5px] px-3 text-[11px] font-medium transition-colors ${assetType === value ? 'bg-accent text-white' : 'text-secondary hover:text-foreground'}`}
                >
                  {value === 'stock' ? '股票' : 'ETF'}
                </button>
              ))}
            </div>
          </div>
          <label className="block">
            <span className="mb-1 block text-[10px] text-muted">验证档位（决定筛选门槛与样本要求）</span>
            <select
              value={profile}
              onChange={event => { setProfileTouched(true); setProfile(event.target.value as MiningBudgetProfile) }}
              className={INPUT_CLS}
            >
              {PROFILE_ORDER.map(value => {
                const info = availability[value]
                const mark = info == null ? '' : info.eligible ? ' ✓' : ` · 需 ${info.required_bars} 日`
                return (
                  <option key={value} value={value}>
                    {PROFILE_LABELS[value]} · {PROFILE_HINTS[value]}{mark}
                  </option>
                )
              })}
            </select>
          </label>
          <div className="grid grid-cols-2 gap-2">
            <label className="block">
              <span className="mb-1 block text-[10px] text-muted">开始</span>
              <input type="date" value={start} onChange={event => setStart(event.target.value)} className={INPUT_CLS} />
            </label>
            <label className="block">
              <span className="mb-1 block text-[10px] text-muted">结束</span>
              <input type="date" value={end} onChange={event => setEnd(event.target.value)} className={INPUT_CLS} />
            </label>
          </div>
          {currentAvailable === false && availability[profile] && (
            <div className="rounded-btn border border-danger/40 bg-danger/5 px-3 py-2 text-[11px] leading-relaxed text-danger">
              交易日不足 · {bars} / {availability[profile]!.required_bars} 日 · 「{PROFILE_LABELS[profile]}」档
              {availability.exploratory?.eligible ? ' · 可改「探索」档' : ' · 需补历史行情'}
            </div>
          )}
          <p className="text-[10px] leading-relaxed text-muted">
            后台运行 · 筛选约 1–3 分钟 · 结果见「挖掘 · 最近运行」
          </p>
        </div>
      </div>

      <div className="flex items-center justify-end gap-2 border-t border-border px-4 py-3">
        <button
          type="button"
          onClick={submitAutoMining}
          disabled={!start || !end || start > end || currentAvailable === false}
          className="inline-flex items-center gap-1.5 rounded-btn bg-accent px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <Sparkles className="h-3.5 w-3.5" />
          开始自动挖掘
        </button>
      </div>
    </Modal>
  )
}
