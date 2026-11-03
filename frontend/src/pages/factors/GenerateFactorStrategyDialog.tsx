import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Sparkles } from 'lucide-react'
import { Modal } from '@/components/Modal'
import { toast } from '@/components/Toast'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

const INPUT_CLS = 'h-8 w-full rounded-input border border-border bg-surface px-2.5 text-xs text-foreground focus:border-accent focus:outline-none'

/** 渲染与挖掘发布同构的单因子排名策略源码 (FactorRankResearchMatrixStrategy)。 */
function renderStrategyCode(strategyId: string, name: string, factorId: string, direction: 'high' | 'low', description: string): string {
  const assetTypes = '["stock"]'
  return `"""${name}: 因子库生成 · 单因子排名。"""
from app.strategy.builtin.factor_rank_research import FactorRankResearchMatrixStrategy

META = {
    "id": ${JSON.stringify(strategyId)},
    "name": ${JSON.stringify(name)},
    "description": ${JSON.stringify(description)},
    "tags": ["因子", "生成"],
    "asset_types": ${assetTypes},
    "timeframes": ["1d"],
    "params": [
        {"id": "entry_score", "label": "入场最低分", "type": "float", "default": 70.0, "min": 0.0, "max": 100.0, "step": 5.0},
        {"id": "exit_score", "label": "离场最高分", "type": "float", "default": 40.0, "min": 0.0, "max": 100.0, "step": 5.0},
        {"id": "top_rank", "label": "每日最多入选", "type": "int", "default": 20, "min": 1, "max": 100, "step": 1},
    ],
    "scoring": {},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_factor_rank_entry"]
EXIT_SIGNALS = ["signal_factor_rank_exit"]
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 30

SCORING = {${JSON.stringify(factorId)}: 1.0}
DIRECTIONS = {${JSON.stringify(factorId)}: ${JSON.stringify(direction)}}
MATRIX_STRATEGY = FactorRankResearchMatrixStrategy(SCORING, DIRECTIONS)
`
}

/** 因子库 → 生成完整可回测策略 (单因子排名, 参数化入场/离场评分与每日上限)。 */
export function GenerateFactorStrategyDialog({
  item, onClose,
}: {
  item: { id: string; label: string; group: string; direction?: 'high' | 'low' | 'none' }
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [name, setName] = useState(`${item.label}排名策略`)
  // 方向未知 (内置因子全部如此) 时不预选: 旧实现默认「值大」, 反转类因子直接点生成就得到反向策略
  const [direction, setDirection] = useState<'high' | 'low' | null>(
    item.direction === 'low' ? 'low' : item.direction === 'high' ? 'high' : null,
  )

  const generate = useMutation({
    mutationFn: async () => {
      if (!direction) throw new Error('未选方向')
      const baseId = `custom_factor_${item.id}`.replace(/[^a-z0-9_]/g, '').slice(0, 56)
      const description = `${item.label} · 因子 ${item.id}`
      let lastError: Error | null = null
      // ID 冲突时递增后缀直到空位 (旧实现只试 3 个, 同一因子第 4 次生成必失败);
      // 冲突请求静默, 只在最终结果处提示一次
      for (let attempt = 1; attempt <= 50; attempt += 1) {
        const strategyId = attempt === 1 ? baseId : `${baseId}_${attempt}`
        try {
          return await api.strategySaveCodeV2({
            strategy_id: strategyId,
            code: renderStrategyCode(strategyId, name.trim(), item.id, direction, description),
            target_source: 'custom',
            mode: 'create',
            name: name.trim(),
            description,
          }, { quiet: true })
        } catch (error) {
          lastError = error as Error
          if (!/已存在|already exists/i.test((error as Error).message)) throw error
        }
      }
      throw lastError ?? new Error('生成失败')
    },
    onSuccess: result => {
      toast(`已生成 ${result.strategy_id}`, 'success')
      void queryClient.invalidateQueries({ queryKey: ['screener-strategies'] })
      void queryClient.invalidateQueries({ queryKey: QK.strategyLinkOptions() })
      onClose()
    },
    onError: (error: Error) => toast(`生成失败 · ${error.message}`, 'error'),
  })

  return (
    <Modal
      onClose={onClose}
      labelledBy="generate-factor-strategy-title"
      panelClassName="flex max-h-[86vh] w-[92vw] max-w-md flex-col overflow-hidden border border-border bg-surface shadow-xl rounded-card"
    >
      <div className="flex items-start justify-between gap-3 border-b border-border px-4 py-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Sparkles className="h-4 w-4 shrink-0 text-accent" />
            <span id="generate-factor-strategy-title" className="text-sm font-semibold text-foreground">生成策略</span>
          </div>
          <p className="mt-1 font-mono text-[11px] text-muted">{item.id}</p>
        </div>
        <span className="shrink-0 rounded-btn bg-elevated px-1.5 py-0.5 text-[10px] text-secondary">{item.label}</span>
      </div>

      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-4 py-3">
        <div className="rounded-btn border border-border bg-base/40 px-3 py-2 text-[11px] leading-relaxed text-secondary">
          单因子排名 · 入场 ≥ 70 · 离场 ≤ 40 · 每日前 20
        </div>
        <label className="block">
          <span className="mb-1 block text-[10px] text-muted">策略名称</span>
          <input type="text" value={name} maxLength={24} onChange={event => setName(event.target.value)} className={INPUT_CLS} />
        </label>
        <div>
          <span className="mb-1 block text-[10px] text-muted">方向</span>
          <div className="flex gap-1.5">
            {([['high', '值大看多'], ['low', '值小看多']] as const).map(([value, label]) => (
              <button
                key={value}
                type="button"
                onClick={() => setDirection(value)}
                className={`rounded-btn border px-2.5 py-1.5 text-[11px] transition-colors ${direction === value
                  ? 'border-accent/50 bg-accent/10 text-accent'
                  : 'border-border bg-base text-muted hover:border-accent/40'}`}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
        <p className="text-[10px] leading-relaxed text-muted">
          止损 −8% · 持有 ≤ 30 日
        </p>
      </div>

      <div className="flex items-center justify-end gap-2 border-t border-border px-4 py-3">
        <button type="button" onClick={onClose} className="rounded-btn bg-elevated px-3 py-1.5 text-xs text-secondary transition-colors hover:text-foreground">取消</button>
        <button
          type="button"
          onClick={() => generate.mutate()}
          disabled={generate.isPending || !name.trim() || !direction}
          className="inline-flex items-center gap-1.5 rounded-btn bg-accent px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <Sparkles className="h-3.5 w-3.5" />
          {generate.isPending ? '生成中' : '生成策略'}
        </button>
      </div>
    </Modal>
  )
}
