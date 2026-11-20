/**
 * 设置 › AI 技能 (B 档技能管理) — 列表 + 详情 + 编辑 + 版本 + 体检。
 *
 * 经 settings.skills 插槽挂进设置页 (删除 custom/assistant 目录即整体卸载, 设置页不再出现这个标签)。
 * - 列表: 技能 / 触发 / 分组 / 工具数 / 输出 / 检查 / 启用; 点行看详情。
 * - 详情: 适用页面、权限、工具、四项检查、全文; 内置技能只读, 「复制为我的」后可改;
 *   自建技能可编辑保存 (每次保存一版)、回滚到旧版、删除。试运行 = 打开助手发送触发词。
 * - 体检: 每天 08:00 ET 自动跑 (只记录、不推送), 这里可手动再跑, 逐个工具看结果。
 */
import { useEffect, useMemo, useState } from 'react'
import { BookOpen, CheckCircle2, Copy, FilePlus2, Play, RotateCcw, ShieldCheck, Trash2, AlertTriangle } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { cn } from '@/lib/cn'
import {
  checkSkill,
  deleteSkill,
  fetchHealth,
  fetchSkill,
  fetchSkills,
  rollbackSkill,
  runHealth,
  saveSkill,
  setSkillEnabled,
  type HealthReport,
  type SkillCheck,
  type SkillDetail,
  type SkillSummary,
} from '../client'
import { openAssistant, sendMessage } from '../store'
import { ASSISTANT_TOOL_LABELS } from './messages'
import { fmtRun, invalidateSkills, primaryTrigger } from './SkillPicker'

const TEMPLATE = `---
name: my-skill
title: 我的技能
group: 每日
triggers: [/我的技能]
tools: [get_portfolio]
output: chat
pages: [持仓]
---
1. get_portfolio 读模拟持仓，写明数据时点。
2. 只陈述数字与台账事实，不给买卖建议。
`

const BTN = 'inline-flex h-8 shrink-0 cursor-pointer items-center gap-1.5 whitespace-nowrap rounded-btn border border-border px-2.5 text-xs text-secondary transition-colors duration-150 ease-smooth hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50'
const BTN_PRIMARY = 'inline-flex h-8 shrink-0 cursor-pointer items-center gap-1.5 whitespace-nowrap rounded-btn btn-primary px-2.5 text-xs transition-colors duration-150 ease-smooth disabled:cursor-not-allowed disabled:opacity-50'

export function AssistantSkillsSettings() {
  const [skills, setSkills] = useState<SkillSummary[]>([])
  const [tools, setTools] = useState<string[]>([])
  const [selected, setSelected] = useState<string>('')
  const [detail, setDetail] = useState<SkillDetail | null>(null)
  const [editing, setEditing] = useState<string | null>(null)
  const [draftChecks, setDraftChecks] = useState<SkillCheck[] | null>(null)
  const [msg, setMsg] = useState<string>('')
  const [busy, setBusy] = useState(false)
  const [health, setHealth] = useState<HealthReport | null>(null)

  const reload = async (keep?: string) => {
    const res = await fetchSkills()
    setSkills(res.skills)
    setTools(res.tools)
    invalidateSkills()
    const name = keep ?? (selected || res.skills[0]?.name || '')
    setSelected(name)
  }

  useEffect(() => {
    reload().catch(e => setMsg(String(e?.message ?? e)))
    fetchHealth().then(setHealth).catch(() => {})
  }, [])

  useEffect(() => {
    let cancelled = false
    setDetail(null)
    if (!selected) return
    fetchSkill(selected).then(d => { if (!cancelled) setDetail(d) }).catch(e => setMsg(String(e?.message ?? e)))
    return () => { cancelled = true }
  }, [selected, skills])

  const enabledN = skills.filter(s => s.enabled).length
  const warnRows = skills.filter(s => (s.errors ?? 0) + (s.warnings ?? 0) > 0)

  const act = async (fn: () => Promise<unknown>, done?: string) => {
    setBusy(true)
    setMsg('')
    try {
      await fn()
      if (done) setMsg(done)
    } catch (e) {
      setMsg(String((e as Error)?.message ?? e))
    } finally {
      setBusy(false)
    }
  }

  const startEdit = (text: string) => {
    setEditing(text)
    setDraftChecks(null)
  }

  const copyBuiltin = (d: SkillDetail) => {
    const text = d.text
      .replace(/^name: .*$/m, `name: ${d.name}-mine`)
      .replace(/^title: (.*)$/m, (_, t) => `title: 我的${String(t).slice(0, 16)}`)
      .replace(/^triggers: .*$/m, `triggers: [/${d.title.slice(0, 6)}2]`)
      .replace(/^---\n/, `---\nbased_on: ${d.name}\n`)
    startEdit(text)
  }

  const onCheck = () => act(async () => {
    const res = await checkSkill(editing ?? '')
    setDraftChecks(res.checks)
  })

  const onSave = () => act(async () => {
    const res = await saveSkill(editing ?? '')
    setDraftChecks(res.checks)
    if (res.saved) {
      setEditing(null)
      await reload(res.skill.name)
      setMsg(`已保存 v${res.skill.version}`)
    }
  })

  const onTrial = (s: SkillSummary) => {
    openAssistant()
    sendMessage(primaryTrigger(s))
  }

  return (
    <>
      <PageHeader title="AI 技能" subtitle="助手可复用的问答流程 · 只读" />
      <div className="space-y-3 p-5" data-skills-settings>
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-semibold text-foreground">{skills.length} 技能</span>
          <span className="text-xs text-muted">{enabledN} 启用 · {skills.length - enabledN} 停用</span>
          {warnRows.length > 0 && (
            <span className="inline-flex h-5 items-center rounded-[4px] border border-warning/60 px-1.5 text-[11px] text-warning">
              检查 {warnRows.length} 项提示
            </span>
          )}
          <span className="ml-auto" />
          <button type="button" className={BTN} disabled={busy}
            onClick={() => act(async () => setHealth(await runHealth()), '体检完成')}>
            <ShieldCheck className="h-3.5 w-3.5" />运行体检
          </button>
          <button type="button" className={BTN_PRIMARY} onClick={() => startEdit(TEMPLATE)} data-skill-new>
            <FilePlus2 className="h-3.5 w-3.5" />新建
          </button>
        </div>
        {msg && <div className="text-xs text-secondary" data-skill-msg>{msg}</div>}

        <div className="grid grid-cols-1 gap-3 xl:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
          <div className="min-w-0 overflow-hidden rounded-card border border-border bg-surface">
            <div className="overflow-x-auto">
              <table className="w-full text-[13px]" data-skill-table>
                <thead>
                  <tr className="bg-elevated text-left text-xs text-muted">
                    <th className="h-8 px-3 font-medium">技能</th>
                    <th className="px-3 font-medium">触发</th>
                    <th className="hidden px-3 font-medium md:table-cell">分组</th>
                    <th className="px-3 text-right font-medium">工具</th>
                    <th className="hidden px-3 font-medium md:table-cell">输出</th>
                    <th className="px-3 font-medium">检查</th>
                    <th className="px-3 text-right font-medium">启用</th>
                  </tr>
                </thead>
                <tbody>
                  {skills.map(s => (
                    <tr key={s.name} data-skill-row={s.name} onClick={() => { setSelected(s.name); setEditing(null) }}
                      className={cn('h-10 cursor-pointer border-t border-border transition-colors duration-150 ease-smooth hover:bg-elevated/50',
                        s.name === selected && 'bg-accent/[0.08] shadow-[inset_3px_0_0_hsl(var(--accent))]',
                        !s.enabled && 'text-muted')}>
                      <td className="whitespace-nowrap px-3 font-medium">
                        {s.title}
                        {s.source === 'user' && <span className="ml-1.5 rounded-[4px] border border-border px-1 text-[11px] font-normal text-secondary">自建</span>}
                      </td>
                      <td className="whitespace-nowrap px-3 font-mono text-xs text-secondary">{primaryTrigger(s)}</td>
                      <td className="hidden whitespace-nowrap px-3 text-secondary md:table-cell">{s.group}</td>
                      <td className="px-3 text-right tabular-nums">{s.tools.length}</td>
                      <td className="hidden whitespace-nowrap px-3 text-secondary md:table-cell">{s.output === 'report' ? '报告' : '对话'}</td>
                      <td className="whitespace-nowrap px-3 text-xs">
                        {(s.errors ?? 0) > 0
                          ? <span className="text-warning">{s.errors} 错误</span>
                          : (s.warnings ?? 0) > 0
                            ? <span className="text-warning">{s.warnings} 提示</span>
                            : <span className="inline-flex items-center gap-1 text-secondary"><CheckCircle2 className="h-3 w-3 text-accent" />通过</span>}
                      </td>
                      <td className="px-3 text-right" onClick={e => e.stopPropagation()}>
                        <Toggle on={s.enabled} label={`${s.enabled ? '停用' : '启用'}${s.title}`} disabled={busy}
                          onChange={v => act(async () => { await setSkillEnabled(s.name, v); await reload(s.name) })} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <div className="min-w-0 rounded-card border border-border bg-surface p-4 text-xs" data-skill-detail>
            {editing !== null ? (
              <Editor text={editing} onText={t => { setEditing(t); setDraftChecks(null) }} checks={draftChecks}
                busy={busy} onCheck={onCheck} onSave={onSave} onCancel={() => { setEditing(null); setDraftChecks(null) }} tools={tools} />
            ) : detail ? (
              <Detail d={detail} busy={busy} onTrial={() => onTrial(detail)} onCopy={() => copyBuiltin(detail)}
                onEdit={() => startEdit(detail.text)}
                onRollback={v => act(async () => { await rollbackSkill(detail.name, v); await reload(detail.name) }, `已回滚到 v${v}`)}
                onDelete={() => act(async () => { await deleteSkill(detail.name); await reload('') }, '已删除')} />
            ) : (
              <div className="py-8 text-center text-muted">选择左侧技能</div>
            )}
          </div>
        </div>

        <HealthCard health={health} />
      </div>
    </>
  )
}

function Toggle({ on, label, disabled, onChange }: { on: boolean; label: string; disabled?: boolean; onChange: (v: boolean) => void }) {
  return (
    <button type="button" role="switch" aria-checked={on} aria-label={label} disabled={disabled}
      onClick={() => onChange(!on)}
      className={cn('relative inline-flex h-4 w-[30px] shrink-0 cursor-pointer rounded-full border transition-colors duration-150 ease-smooth disabled:opacity-50',
        on ? 'border-accent bg-accent' : 'border-muted bg-base')}>
      <span className={cn('absolute top-px h-3 w-3 rounded-full transition-all duration-150 ease-smooth',
        on ? 'left-[15px] bg-white' : 'left-px bg-muted')} />
    </button>
  )
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex min-w-0 items-start gap-2.5">
      <span className="w-14 shrink-0 text-muted">{label}</span>
      <span className="min-w-0 flex-1 text-foreground">{children}</span>
    </div>
  )
}

function ToolChips({ names }: { names: string[] }) {
  return (
    <span className="flex flex-wrap gap-1">
      {names.map(t => (
        <span key={t} className="inline-flex items-center gap-1 rounded-[4px] border border-border px-1.5 text-[11px] leading-5 text-secondary">
          <CheckCircle2 className="h-3 w-3 text-accent" />{ASSISTANT_TOOL_LABELS[t] ?? t}
        </span>
      ))}
    </span>
  )
}

function Checks({ checks }: { checks: SkillCheck[] }) {
  if (!checks.length) {
    return <span className="inline-flex items-center gap-1 text-secondary"><CheckCircle2 className="h-3 w-3 text-accent" />必填项 · 工具存在 · 触发词无冲突 · 要求数据时点</span>
  }
  return (
    <ul className="space-y-0.5" data-skill-checks>
      {checks.map((c, i) => (
        <li key={i} className={cn('flex items-start gap-1', c.level === 'error' ? 'text-warning' : 'text-secondary')}>
          <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
          <span>{c.level === 'error' ? '错误' : '提示'} · {c.item} · {c.detail}</span>
        </li>
      ))}
    </ul>
  )
}

function Detail({ d, busy, onTrial, onCopy, onEdit, onRollback, onDelete }: {
  d: SkillDetail
  busy: boolean
  onTrial: () => void
  onCopy: () => void
  onEdit: () => void
  onRollback: (v: number) => void
  onDelete: () => void
}) {
  const runs = d.runs ?? []
  return (
    <div className="flex flex-col gap-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <BookOpen className="h-4 w-4 text-accent" />
        <span className="text-sm font-medium text-foreground">{d.title}</span>
        <span className="font-mono text-[11px] text-secondary">{primaryTrigger(d)}</span>
        <span className="rounded-[4px] border border-border px-1.5 text-[11px] leading-5 text-secondary">
          {d.source === 'builtin' ? '内置' : `v${d.version}`}
        </span>
        <span className="ml-auto" />
        <button type="button" className={BTN} onClick={onTrial} disabled={!d.enabled} data-skill-trial>
          <Play className="h-3 w-3" />试运行
        </button>
      </div>
      <Row label="适用页面">{d.pages.join('、') || '全部'}</Row>
      <Row label="输出">{d.output === 'report' ? '报告' : '对话'}</Row>
      <Row label="权限">只读</Row>
      <Row label="工具"><ToolChips names={d.tools} /></Row>
      <Row label="检查"><Checks checks={d.checks} /></Row>
      <details className="group rounded-btn border border-border bg-base" data-skill-source>
        <summary className="cursor-pointer select-none px-3 py-1.5 text-xs text-secondary hover:text-foreground">源文件</summary>
        <pre className="max-h-64 overflow-auto whitespace-pre-wrap border-t border-border p-3 font-mono text-[11px] leading-relaxed text-secondary">{d.text}</pre>
      </details>
      <div className="flex flex-wrap gap-2">
        {d.source === 'builtin'
          ? <button type="button" className={BTN} onClick={onCopy} data-skill-copy><Copy className="h-3 w-3" />复制为我的技能</button>
          : <>
              <button type="button" className={BTN} onClick={onEdit} disabled={busy}>编辑</button>
              <button type="button" className={BTN} onClick={onDelete} disabled={busy}><Trash2 className="h-3 w-3" />删除</button>
            </>}
      </div>
      {d.versions.length > 0 && (
        <div>
          <div className="mb-1 text-muted">版本</div>
          <div className="flex flex-wrap gap-x-4 gap-y-1">
            {d.versions.slice(0, 6).map(v => (
              <span key={v.version} className="whitespace-nowrap text-secondary">
                <b className="text-foreground">v{v.version}</b> {v.saved_at.slice(5, 10)}
                {v.current
                  ? <span className="ml-1 text-muted">当前</span>
                  : <button type="button" className="ml-1 cursor-pointer text-accent dark:text-blue-400" disabled={busy}
                      onClick={() => onRollback(v.version)}><RotateCcw className="inline h-3 w-3" /> 回滚</button>}
              </span>
            ))}
          </div>
        </div>
      )}
      <div>
        <div className="mb-1 text-muted">最近 {runs.length} 次运行</div>
        {runs.length === 0
          ? <span className="text-muted">还没有运行记录</span>
          : <div className="flex items-center gap-1" data-skill-runs>
              {runs.map((r, i) => (
                <span key={i} title={`${fmtRun(r.at)} · ${r.ok ? '通过' : '未完成'} · ${r.tools} 个工具`}
                  className={cn('h-2 w-5 rounded-sm', r.ok ? 'bg-secondary/35' : 'bg-warning')} />
              ))}
              <span className="ml-2 text-secondary">{runs.filter(r => r.ok).length} 通过</span>
            </div>}
      </div>
    </div>
  )
}

function Editor({ text, onText, checks, busy, onCheck, onSave, onCancel, tools }: {
  text: string
  onText: (t: string) => void
  checks: SkillCheck[] | null
  busy: boolean
  onCheck: () => void
  onSave: () => void
  onCancel: () => void
  tools: string[]
}) {
  const hasError = useMemo(() => (checks ?? []).some(c => c.level === 'error'), [checks])
  return (
    <div className="flex flex-col gap-2.5" data-skill-editor>
      <div className="flex items-center gap-2">
        <span className="text-sm font-medium text-foreground">编辑技能</span>
        <span className="text-[11px] text-muted">保存时新增一个版本</span>
      </div>
      <textarea value={text} onChange={e => onText(e.target.value)} spellCheck={false} rows={16}
        className="w-full resize-y rounded-btn border border-border bg-base p-3 font-mono text-[12px] leading-relaxed text-foreground outline-none focus:border-accent/50" />
      <div className="text-[11px] text-muted">
        可用工具: <span className="font-mono">{tools.join(', ')}</span>
      </div>
      {checks && <Checks checks={checks} />}
      <div className="flex flex-wrap gap-2">
        <button type="button" className={BTN} onClick={onCheck} disabled={busy}>检查</button>
        <button type="button" className={BTN_PRIMARY} onClick={onSave} disabled={busy || hasError} data-skill-save>保存</button>
        <button type="button" className={BTN} onClick={onCancel} disabled={busy}>取消</button>
      </div>
    </div>
  )
}

const STATE_LABEL: Record<string, string> = { ok: '正常', warn: '空结果', error: '故障', skip: '跳过' }

function HealthCard({ health }: { health: HealthReport | null }) {
  const tools = health?.tools ?? []
  return (
    <section className="rounded-card border border-border bg-surface" data-assistant-health>
      <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-2.5">
        <ShieldCheck className="h-4 w-4 text-secondary" />
        <span className="text-sm font-medium text-foreground">助手体检</span>
        <span className="text-xs text-muted">
          {health?.at ? `${fmtRun(health.at)} · ${health.summary ?? ''}` : '每日 08:00 ET 自动运行 · 只记录不推送'}
        </span>
      </div>
      {tools.length > 0 && (
        <div className="grid grid-cols-1 gap-x-6 px-4 py-2 sm:grid-cols-2 xl:grid-cols-3">
          {tools.map(t => (
            <div key={t.name} data-health-tool={t.name} className="flex min-w-0 items-center gap-2 border-b border-border/60 py-1.5 text-xs last:border-b-0">
              <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full',
                t.state === 'error' ? 'bg-warning' : t.state === 'warn' ? 'bg-warning/60' : t.state === 'skip' ? 'bg-muted' : 'bg-accent')} />
              <span className="w-20 shrink-0 text-foreground">{t.label}</span>
              <span className="min-w-0 flex-1 truncate text-secondary" title={t.detail}>{t.detail}</span>
              <span className={cn('shrink-0', t.state === 'error' ? 'text-warning' : 'text-muted')}>{STATE_LABEL[t.state] ?? t.state}</span>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}
