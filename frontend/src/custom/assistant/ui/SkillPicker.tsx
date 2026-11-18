/**
 * 技能入口 — 面板顶部技能条 + 输入框打出 / 时弹出的指令卡 (B 档技能管理的对话侧)。
 *
 * - 技能条: 当前页面相关的技能排前, 点一下 = 发送「/触发词」(后端按触发词注入技能步骤)。
 * - 指令卡: 输入以 / 开头时出现, 左列分组列表、右列预览 (适用页面 / 输出 / 权限 / 工具 / 步骤 / 最近运行);
 *   上下键选择, Enter 选中, Esc 关闭。手机宽度改为上下排。
 * - 只读: 这里不改技能; 新建、修改、停用在「设置 › AI 技能」。
 */
import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { CheckCircle2, Play, Settings2 } from 'lucide-react'
import { cn } from '@/lib/cn'
import { fetchSkill, fetchSkills, type SkillDetail, type SkillSummary } from '../client'
import { ASSISTANT_TOOL_LABELS } from './messages'

let cache: { at: number; skills: SkillSummary[] } | null = null
const CACHE_MS = 60_000

/** 技能列表 (60 秒内存缓存; 设置页保存后调用 invalidateSkills)。 */
export function useSkills(): SkillSummary[] {
  const [skills, setSkills] = useState<SkillSummary[]>(() => cache?.skills ?? [])
  useEffect(() => {
    let cancelled = false
    if (cache && Date.now() - cache.at < CACHE_MS) return
    fetchSkills()
      .then(res => {
        cache = { at: Date.now(), skills: res.skills }
        if (!cancelled) setSkills(res.skills)
      })
      .catch(() => {})
    return () => { cancelled = true }
  }, [])
  return skills.filter(s => s.enabled)
}

export function invalidateSkills() {
  cache = null
}

export function primaryTrigger(skill: Pick<SkillSummary, 'triggers'>): string {
  return skill.triggers.find(t => t.startsWith('/')) ?? `/${skill.triggers[0] ?? ''}`
}

/** 当前页面相关的技能排前, 其余按分组顺序。 */
export function rankForPage(skills: SkillSummary[], page: string): SkillSummary[] {
  if (!page) return skills
  const hit = skills.filter(s => s.pages.some(p => p === page))
  return [...hit, ...skills.filter(s => !hit.includes(s))]
}

export function SkillBar({ skills, page, disabled, onPick, onMore }: {
  skills: SkillSummary[]
  page: string
  disabled: boolean
  onPick: (skill: SkillSummary) => void
  onMore: () => void
}) {
  const ranked = useMemo(() => rankForPage(skills, page), [skills, page])
  if (!skills.length) return null
  return (
    <div data-skill-bar className="flex shrink-0 gap-1.5 overflow-x-auto border-b border-border px-4 py-2 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
      {ranked.slice(0, 5).map(skill => (
        <button
          key={skill.name}
          type="button"
          disabled={disabled}
          onClick={() => onPick(skill)}
          title={`${primaryTrigger(skill)} · ${skill.output === 'report' ? '报告' : '对话'}`}
          className={cn(
            'shrink-0 cursor-pointer whitespace-nowrap rounded-full border px-2.5 py-0.5 text-xs transition-colors duration-150 ease-smooth disabled:cursor-not-allowed disabled:opacity-40',
            skill.pages.includes(page)
              ? 'border-accent/50 bg-accent/10 text-accent dark:text-blue-400'
              : 'border-border text-secondary hover:border-accent/40 hover:text-foreground',
          )}
        >
          {skill.title}
        </button>
      ))}
      <button
        type="button"
        onClick={onMore}
        className="shrink-0 cursor-pointer whitespace-nowrap rounded-full border border-border px-2.5 py-0.5 text-xs text-muted transition-colors duration-150 ease-smooth hover:text-foreground"
      >
        全部 {skills.length}
      </button>
    </div>
  )
}

const GROUP_ORDER = ['每日', '研究', '策略', '系统']

export function filterSkills(skills: SkillSummary[], query: string): SkillSummary[] {
  const q = query.replace(/^\//, '').trim().toLowerCase()
  if (!q) return skills
  return skills.filter(s =>
    s.title.toLowerCase().includes(q)
    || s.name.includes(q)
    || s.triggers.some(t => t.replace(/^\//, '').toLowerCase().includes(q)))
}

export function SkillPalette({ skills, query, page, active, onActive, onPick, onClose }: {
  skills: SkillSummary[]
  query: string
  page: string
  active: number
  onActive: (index: number) => void
  onPick: (skill: SkillSummary) => void
  onClose: () => void
}) {
  const navigate = useNavigate()
  const list = useMemo(() => rankForPage(filterSkills(skills, query), page), [skills, query, page])
  const groups = useMemo(() => {
    const order = [...GROUP_ORDER, ...new Set(list.map(s => s.group).filter(g => !GROUP_ORDER.includes(g)))]
    return order.map(g => ({ group: g, items: list.filter(s => s.group === g) })).filter(g => g.items.length)
  }, [list])
  const flat = groups.flatMap(g => g.items)
  const current = flat[Math.min(active, Math.max(flat.length - 1, 0))]
  const [detail, setDetail] = useState<SkillDetail | null>(null)

  useEffect(() => {
    let cancelled = false
    setDetail(null)
    if (!current) return
    fetchSkill(current.name).then(d => { if (!cancelled) setDetail(d) }).catch(() => {})
    return () => { cancelled = true }
  }, [current?.name])

  if (!flat.length) {
    return (
      <div data-skill-palette className="border-t border-border px-4 py-3 text-xs text-muted">
        无匹配技能
      </div>
    )
  }

  const steps = skillSteps(detail?.body ?? '')

  return (
    <div data-skill-palette className="grid min-h-0 shrink-0 grid-cols-1 border-t border-border bg-surface md:h-[420px] md:grid-cols-2">
      <div className="max-h-[30vh] overflow-y-auto border-b border-border p-2 md:max-h-none md:border-b-0 md:border-r">
        {groups.map(g => (
          <div key={g.group} className="mb-1.5 last:mb-0">
            <div className="px-2 py-1 text-[11px] text-muted">{g.group}</div>
            {g.items.map(skill => {
              const index = flat.indexOf(skill)
              const on = skill === current
              return (
                <button
                  key={skill.name}
                  type="button"
                  data-skill-item={skill.name}
                  onMouseEnter={() => onActive(index)}
                  onClick={() => onPick(skill)}
                  className={cn(
                    'flex h-8 w-full cursor-pointer items-center gap-2 rounded-btn px-2 text-left text-[13px] transition-colors duration-150 ease-smooth',
                    on ? 'bg-accent/10 text-foreground shadow-[inset_3px_0_0_hsl(var(--accent))]' : 'text-secondary hover:bg-elevated/60',
                  )}
                >
                  <span className="truncate">{skill.title}</span>
                  <span className="ml-auto shrink-0 font-mono text-[11px] text-secondary">{primaryTrigger(skill)}</span>
                  <span className="shrink-0 rounded-[4px] border border-border px-1.5 text-[11px] leading-5 text-secondary">
                    {skill.output === 'report' ? '报告' : '对话'}
                  </span>
                </button>
              )
            })}
          </div>
        ))}
      </div>
      {current && (
        <div data-skill-preview={current.name} className="flex min-w-0 flex-col gap-2 overflow-y-auto p-3 text-xs">
          <div className="flex items-baseline gap-2">
            <span className="text-sm font-medium text-foreground">{current.title}</span>
            <span className="font-mono text-[11px] text-secondary">{primaryTrigger(current)}</span>
            <button
              type="button"
              data-skill-run
              onClick={() => onPick(current)}
              className="ml-auto inline-flex shrink-0 cursor-pointer items-center gap-1 self-center rounded-btn bg-accent px-2 py-0.5 text-xs text-white transition-colors duration-150 ease-smooth hover:bg-accent/90 dark:bg-blue-700 dark:hover:bg-blue-600"
            >
              <Play className="h-3 w-3" />
              运行
            </button>
            <button
              type="button"
              title="管理技能"
              onClick={() => { onClose(); navigate('/settings?tab=skills') }}
              className="shrink-0 cursor-pointer self-center text-muted hover:text-foreground"
            >
              <Settings2 className="h-3.5 w-3.5" />
            </button>
          </div>
          <Row label="适用页面">{current.pages.join('、') || '全部'}</Row>
          <Row label="输出">{current.output === 'report' ? '报告' : '对话'}</Row>
          <Row label="权限">只读</Row>
          <Row label="工具">
            <span className="flex flex-wrap gap-1">
              {current.tools.map(t => (
                <span key={t} className="inline-flex items-center gap-1 rounded-[4px] border border-border px-1.5 text-[11px] leading-5 text-secondary">
                  <CheckCircle2 className="h-3 w-3 text-accent" />
                  {ASSISTANT_TOOL_LABELS[t] ?? t}
                </span>
              ))}
            </span>
          </Row>
          {steps.length > 0 && (
            <Row label="步骤">
              <ol className="list-decimal space-y-0.5 pl-4 text-secondary">
                {steps.slice(0, 5).map((step, i) => <li key={i}>{step}</li>)}
              </ol>
            </Row>
          )}
          {current.last_run && (
            <Row label="最近">{fmtRun(current.last_run.at)} · {current.last_run.ok ? '通过' : '未完成'}</Row>
          )}
        </div>
      )}
    </div>
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

/** 技能正文 → 展示用步骤: 去序号, 工具英文名换成「中文名」(正文原样留给模型, 界面不露英文标识)。 */
export function skillSteps(body: string): string[] {
  return body
    .split('\n')
    .map(l => l.replace(/^\s*\d+\.\s*/, '').trim())
    .filter(Boolean)
    .map(l => l.replace(/\b((?:get|list|run)_[a-z_]+)\b\s*/g, (_m, name: string) => `「${ASSISTANT_TOOL_LABELS[name] ?? name}」`))
}

/** 运行时刻: 后端存美东 ISO, 显示 MM-DD HH:MM ET。 */
export function fmtRun(at: string): string {
  return `${at.slice(5, 10)} ${at.slice(11, 16)} ET`
}
