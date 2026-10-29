/**
 * 机会页 (0.29.0 起一个请求 GET /api/opportunities/page 取齐整页, 某块失败只在那一块写读取失败)。
 * 0.41.0 (10-28 产品决策 「非显然档」): 一页作战台 —— 状态栏 (保留) + 大盘状态条 + 共振 + 四栏同屏 + 持仓。
 *   四栏顺序按大盘状态 (普涨 / 平常 / 普跌) 换; 共振卡只进影子账本。雷达图与页底引擎明细退役。
 *   状态变化由后端帧经 SSE 推送, 页面立即重取; 30/60 秒轮询保留作兜底。
 *   只改展示: 交易环 / 准入 / 许可 / 下单簿 / 台账都不变。
 */
import { lazy, Suspense, useMemo, useState } from 'react'
import { Settings2 } from 'lucide-react'
import { RulesConsole } from '@/components/opportunities/RulesConsole'
import { StatusRail } from '@/components/opportunities/StatusRail'
import { COL_ORDER, EngineCols, Holdings, RegimeBar, Resonance } from '@/components/opportunities/Desk'
import { useOppPage } from '@/components/opportunities/useOppPage'
import { hm, md } from '@/components/opportunities/format'
import type { OppPagePayload, SwingBoardsPayload } from '@/lib/api'

// 个股预览 (K 线 / 分时, 含 ~1MB ECharts) 只在点开后才需要: 懒加载, 不进首屏包。
const StockPreviewDialog = lazy(() => import('@/components/StockPreviewDialog').then((m) => ({ default: m.StockPreviewDialog })))

function SectionError({ name, err }: { name: string; err: string }) {
  return <div data-section-error={name} className="mb-3 rounded-card border border-dashed border-danger/40 px-3 py-3 text-xs text-danger">{`${name}读取失败（${err}）`}</div>
}

/** 页头第二行: 数据时点 (信号日 / 机会板刷新) */
export function headMeta(sw: SwingBoardsPayload): string {
  return [sw.as_of ? `信号日 ${md(sw.as_of)}` : '', sw.refresh?.last_at ? `机会板刷新 ${hm(sw.refresh.last_at)} 北京` : ''].filter(Boolean).join(' · ')
}

function Head({ d, onGear }: { d: OppPagePayload | undefined; onGear: () => void }) {
  const sw = d?.swing
  return (
    <div data-head className="mb-3 flex items-center gap-2">
      <h1 className="shrink-0 text-lg font-semibold tracking-tight">机会</h1>
      <span className="rounded-btn border border-dashed border-border px-1.5 text-[11px] leading-5 text-secondary">纸面</span>
      {sw && <span data-swing-head className="min-w-0 truncate text-xs tabular-nums text-muted">{headMeta(sw)}</span>}
      <button type="button" aria-label="规则" title="规则设置" data-gear onClick={onGear}
        className="ml-auto flex h-9 w-9 shrink-0 items-center justify-center rounded-btn text-secondary hover:bg-elevated hover:text-foreground max-md:h-11 max-md:w-11">
        <Settings2 className="h-[18px] w-[18px]" />
      </button>
    </div>
  )
}

export function Opportunities() {
  const q = useOppPage()
  const d = q.data
  const [open, setOpen] = useState<string | null>(null)
  const [consoleOpen, setConsoleOpen] = useState(false)
  const errs = d?.errors ?? {}
  const view = d?.radar_view ?? null
  const desk = d?.desk ?? null
  const order = COL_ORDER[desk?.regime.key ?? 'normal']
  const res = useMemo(() => new Set((desk?.resonance ?? []).map((x) => x.symbol)), [desk])
  // 预览里左右切票顺序 = 共振 → 各栏 → 持仓, 同票只出现一次
  const navList = useMemo(() => {
    if (!d) return []
    const seen = new Set<string>()
    const all = [...(desk?.resonance ?? []).map((x) => x.symbol), ...(d.intraday?.active ?? []).map((c) => c.symbol), ...(d.radar?.picks ?? []).map((r) => r.symbol),
      ...(d.gaps?.rows ?? []).map((r) => r.symbol), ...[...(d.swing?.long ?? []), ...(d.swing?.short ?? [])].map((c) => c.symbol)]
    return all.filter((s) => !seen.has(s) && seen.add(s)).map((symbol) => ({ symbol }))
  }, [d, desk])
  const stale = !!(q.isError && d)
  return (
    <div className="mx-auto max-w-[1440px] px-4 py-4 pb-24 md:px-6">
      <Head d={d} onGear={() => setConsoleOpen(true)} />
      {q.isLoading && <div data-loading role="status" className="py-12 text-center text-sm text-muted">加载中…</div>}
      {q.isError && !d && <div data-error className="py-6 text-sm text-danger">{(q.error as Error)?.message || '读取失败'}</div>}
      {stale && <div data-stale className="mb-2 rounded-btn border border-warning/40 bg-warning/10 px-3 py-1.5 text-xs font-semibold text-warning">{`刷新失败 · 显示 ${hm(d!.generated_at)} 北京的数据`}</div>}
      {d && (
        <>
          {view ? <StatusRail view={view} /> : <SectionError name="状态栏" err={errs.radar_view ?? errs.swing ?? '无数据'} />}
          {desk ? (
            <>
              <RegimeBar desk={desk} mood={d.mood?.judgment ?? null} />
              <Resonance desk={desk} onOpen={setOpen} />
            </>
          ) : <SectionError name="大盘状态" err={errs.desk ?? '无数据'} />}
          <EngineCols order={order} onOpen={setOpen}
            d={{ gaps: d.gaps, radar: d.radar, intraday: d.intraday, swing: d.swing, errors: errs, chg: desk?.regime.chg ?? {}, res }} />
          {view && <Holdings view={view} onOpen={setOpen} />}
        </>
      )}
      {open && (
        <Suspense fallback={null}>
          <StockPreviewDialog symbol={open} onClose={() => setOpen(null)} navList={navList.length ? navList : [{ symbol: open }]} onNavigate={setOpen} />
        </Suspense>
      )}
      {consoleOpen && <RulesConsole settings={d?.settings ?? null} onClose={() => setConsoleOpen(false)} />}
    </div>
  )
}
