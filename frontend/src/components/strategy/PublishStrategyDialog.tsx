import { useId, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { CheckCircle2, History, Loader2, Rocket, RotateCcw, Save, SlidersHorizontal } from 'lucide-react'
import { Modal } from '@/components/Modal'

/** 与后端 mining_candidates.STRATEGY_NAME_MAX 一致 */
export const STRATEGY_NAME_MAX = 40

/** 去首尾空白、内部空白压成一个 (与后端 clean_strategy_name 同口径) */
export function cleanStrategyName(raw: string): string {
  return raw.split(/\s+/).filter(Boolean).join(' ')
}

export function strategyNameError(raw: string): string | null {
  const name = cleanStrategyName(raw)
  if (!name) return '名称不能为空'
  if (Array.from(name).length > STRATEGY_NAME_MAX) return `名称最多 ${STRATEGY_NAME_MAX} 个字`
  return null
}

/** 发布后直达: 回测页预选该策略并自动跑一次 / 策略页定位到该策略 */
export const backtestHref = (strategyId: string) => `/backtest?strategy=${encodeURIComponent(strategyId)}&run=1`
export const strategyHref = (strategyId: string) => `/screener?s=${encodeURIComponent(strategyId)}`

interface Published { strategy_id: string; name?: string }

/**
 * 挖掘候选「发布为策略」: 名称默认 = 因子名 + 方向, 可改; 发布成功后同一个弹窗里直接去回测 / 看策略,
 * 不用再自己找到策略页、回测页重新选一遍 (多面板切换, 新手不会用)。
 */
export function PublishStrategyDialog({
  defaultName,
  facts,
  republish = false,
  onPublish,
  onClose,
}: {
  defaultName: string
  /** 该候选已发布过: 再发布 = 改名 (id 与打分不变) */
  republish?: boolean
  /** 名称下的一行数字 (样本外夏普 / 交易数等) */
  facts?: string
  onPublish: (name: string) => Promise<Published>
  onClose: () => void
}) {
  const titleId = useId()
  const inputRef = useRef<HTMLInputElement>(null)
  const [name, setName] = useState(defaultName)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [published, setPublished] = useState<Published | null>(null)
  const nameError = strategyNameError(name)

  const submit = async () => {
    if (nameError || saving) return
    setSaving(true)
    setError(null)
    try {
      const out = await onPublish(cleanStrategyName(name))
      setPublished(out)
    } catch (err) {
      setError(`发布失败 · ${String((err as Error)?.message || err)}`)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      onClose={saving ? () => undefined : onClose}
      labelledBy={titleId}
      initialFocusRef={published ? undefined : inputRef as React.RefObject<HTMLElement>}
      closeOnBackdrop={!saving}
      panelClassName="w-[92vw] max-w-md rounded-card border border-border bg-surface shadow-xl"
    >
      <div data-publish-dialog className="p-4">
        {published ? (
          <div data-publish-done>
            <h2 id={titleId} className="flex items-center gap-2 text-sm font-semibold text-foreground">
              <CheckCircle2 className="h-4 w-4 shrink-0 text-success" />
              {republish ? '已更新' : '已发布'}
            </h2>
            <div className="mt-2 truncate text-sm text-foreground" title={published.name}>{published.name ?? cleanStrategyName(name)}</div>
            <div className="mt-1 text-[11px] text-muted">不自动下单 · 不进机会板</div>
            <div className="mt-4 flex flex-wrap justify-end gap-2">
              <button
                type="button"
                onClick={onClose}
                className="inline-flex h-8 items-center rounded-btn border border-border px-3 text-xs text-secondary hover:border-accent/40 hover:text-accent"
              >
                完成
              </button>
              <Link
                to={strategyHref(published.strategy_id)}
                onClick={onClose}
                data-publish-view
                className="inline-flex h-8 items-center gap-1.5 rounded-btn border border-border px-3 text-xs text-secondary hover:border-accent/40 hover:text-accent"
              >
                <SlidersHorizontal className="h-3.5 w-3.5" />
                查看策略
              </Link>
              <Link
                to={backtestHref(published.strategy_id)}
                onClick={onClose}
                data-publish-backtest
                className="inline-flex h-8 items-center gap-1.5 rounded-btn btn-primary px-3 text-xs font-medium"
              >
                <History className="h-3.5 w-3.5" />
                回测
              </Link>
            </div>
          </div>
        ) : (
          <form
            onSubmit={event => { event.preventDefault(); void submit() }}
            noValidate
          >
            <h2 id={titleId} className="text-sm font-semibold text-foreground">{republish ? '已发布 · 改名' : '发布为策略'}</h2>
            {facts && <div className="mt-1 text-[11px] tabular-nums text-muted">{facts}</div>}
            <label className="mt-3 block text-[11px] font-medium text-secondary" htmlFor={`${titleId}-name`}>名称</label>
            <div className="mt-1 flex items-center gap-1.5">
              <input
                id={`${titleId}-name`}
                ref={inputRef}
                data-publish-name
                value={name}
                maxLength={STRATEGY_NAME_MAX + 10}
                onChange={event => { setName(event.target.value); setError(null) }}
                aria-invalid={nameError ? true : undefined}
                className="h-8 min-w-0 flex-1 rounded-btn border border-border bg-base px-2 text-sm text-foreground outline-none focus:border-accent"
              />
              {cleanStrategyName(name) !== defaultName && (
                <button
                  type="button"
                  onClick={() => { setName(defaultName); setError(null) }}
                  title={`恢复默认名称：${defaultName}`}
                  className="inline-flex h-8 shrink-0 items-center gap-1 rounded-btn border border-border px-2 text-[11px] text-secondary hover:border-accent/40 hover:text-accent"
                >
                  <RotateCcw className="h-3 w-3" />
                  默认
                </button>
              )}
            </div>
            <div className="mt-1 flex justify-between gap-2 text-[11px]">
              <span className={nameError || error ? 'text-danger' : 'text-muted'} data-publish-error={nameError || error ? '' : undefined}>
                {error ?? nameError ?? '不自动下单 · 不进机会板'}
              </span>
              <span className="shrink-0 tabular-nums text-muted">{Array.from(cleanStrategyName(name)).length}/{STRATEGY_NAME_MAX}</span>
            </div>
            <div className="mt-4 flex justify-end gap-2">
              <button
                type="button"
                onClick={onClose}
                disabled={saving}
                className="inline-flex h-8 items-center rounded-btn border border-border px-3 text-xs text-secondary hover:border-accent/40 hover:text-accent disabled:opacity-50"
              >
                取消
              </button>
              <button
                type="submit"
                data-publish-submit
                disabled={!!nameError || saving}
                className="inline-flex h-8 items-center gap-1.5 rounded-btn btn-primary px-3 text-xs font-medium disabled:cursor-not-allowed disabled:opacity-50"
              >
                {saving
                  ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  : republish ? <Save className="h-3.5 w-3.5" /> : <Rocket className="h-3.5 w-3.5" />}
                {republish ? '保存' : '发布'}
              </button>
            </div>
          </form>
        )}
      </div>
    </Modal>
  )
}

/** 已发布挖掘策略改名 (策略页标题旁的铅笔): 只改显示名, id / 打分 / 回测口径不变 */
export function RenameStrategyDialog({
  currentName,
  onRename,
  onClose,
}: {
  currentName: string
  onRename: (name: string) => Promise<unknown>
  onClose: () => void
}) {
  const titleId = useId()
  const inputRef = useRef<HTMLInputElement>(null)
  const [name, setName] = useState(currentName)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const nameError = strategyNameError(name)
  const unchanged = cleanStrategyName(name) === currentName

  const submit = async () => {
    if (nameError || unchanged || saving) return
    setSaving(true)
    setError(null)
    try {
      await onRename(cleanStrategyName(name))
      onClose()
    } catch (err) {
      setError(`改名失败 · ${String((err as Error)?.message || err).replace(/^改名失败 · /, '')}`)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      onClose={saving ? () => undefined : onClose}
      labelledBy={titleId}
      initialFocusRef={inputRef as React.RefObject<HTMLElement>}
      closeOnBackdrop={!saving}
      panelClassName="w-[92vw] max-w-md rounded-card border border-border bg-surface shadow-xl"
    >
      <form data-rename-dialog className="p-4" noValidate onSubmit={event => { event.preventDefault(); void submit() }}>
        <h2 id={titleId} className="text-sm font-semibold text-foreground">改名</h2>
        <input
          ref={inputRef}
          data-rename-name
          aria-label="策略名称"
          value={name}
          maxLength={STRATEGY_NAME_MAX + 10}
          onChange={event => { setName(event.target.value); setError(null) }}
          aria-invalid={nameError ? true : undefined}
          className="mt-3 h-8 w-full rounded-btn border border-border bg-base px-2 text-sm text-foreground outline-none focus:border-accent"
        />
        <div className="mt-1 flex justify-between gap-2 text-[11px]">
          <span className={nameError || error ? 'text-danger' : 'text-muted'}>{error ?? nameError ?? '只改名称 · 打分与回测不变'}</span>
          <span className="shrink-0 tabular-nums text-muted">{Array.from(cleanStrategyName(name)).length}/{STRATEGY_NAME_MAX}</span>
        </div>
        <div className="mt-4 flex justify-end gap-2">
          <button type="button" onClick={onClose} disabled={saving} className="inline-flex h-8 items-center rounded-btn border border-border px-3 text-xs text-secondary hover:border-accent/40 hover:text-accent disabled:opacity-50">取消</button>
          <button
            type="submit"
            data-rename-submit
            disabled={!!nameError || unchanged || saving}
            className="inline-flex h-8 items-center gap-1.5 rounded-btn btn-primary px-3 text-xs font-medium disabled:cursor-not-allowed disabled:opacity-50"
          >
            {saving && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            保存
          </button>
        </div>
      </form>
    </Modal>
  )
}
