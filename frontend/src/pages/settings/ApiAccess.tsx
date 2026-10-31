/**
 * 设置 → API 与 MCP (0.44.0)
 *
 * 生成对外 API 密钥, 让其他 agent / 脚本接入: 回测、策略增删改、自选、规则集… 全系统操作。
 * - 明文密钥只在生成后显示一次; 服务端只存摘要。
 * - 两档: 完全控制 (与网页登录同等) / 只读 (GET + 回测、选股这类不改数据的计算)。
 * - 任何密钥都不能改登录密码、不能管理密钥 (只能在这里网页登录后操作)。
 * - MCP 端点: <站点>/api/mcp (Streamable HTTP, 无状态), 认证头 Authorization: Bearer <密钥>。
 */
import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { KeyRound, Plus, Trash2, Copy, Check, Plug, ShieldAlert } from 'lucide-react'
import { api, type ApiKeyInfo } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { PageHeader } from '@/components/PageHeader'

const INPUT_CLS =
  'h-9 px-2.5 rounded-lg bg-base border-0 ring-1 ring-border/30 text-xs text-foreground placeholder:text-muted/50 focus:outline-none focus:ring-2 focus:ring-accent/30'

const EXPIRY_OPTIONS: { value: string; label: string }[] = [
  { value: '', label: '永不过期' },
  { value: '30', label: '30 天' },
  { value: '90', label: '90 天' },
  { value: '365', label: '1 年' },
]

function fmtTime(ts: number | null | undefined): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function useCopy(): [string | null, (key: string, text: string) => void] {
  const [copied, setCopied] = useState<string | null>(null)
  const copy = (key: string, text: string) => {
    void navigator.clipboard?.writeText(text).then(() => {
      setCopied(key)
      setTimeout(() => setCopied(c => (c === key ? null : c)), 1500)
    })
  }
  return [copied, copy]
}

export function mcpSnippets(endpoint: string, token: string) {
  const t = token || '<你的密钥>'
  return {
    claudeCode: `claude mcp add --transport http zentrade ${endpoint} --header "Authorization: Bearer ${t}"`,
    json: JSON.stringify({ mcpServers: { zentrade: { type: 'http', url: endpoint, headers: { Authorization: `Bearer ${t}` } } } }, null, 2),
    curl: `curl -s ${endpoint.replace(/\/api\/mcp$/, '')}/api/watchlist -H "Authorization: Bearer ${t}"`,
  }
}

export function SettingsApiAccessPanel() {
  const qc = useQueryClient()
  const q = useQuery({ queryKey: QK.apiKeys, queryFn: () => api.apiKeys() })
  const [name, setName] = useState('')
  const [scope, setScope] = useState<'full' | 'read'>('read')
  const [expiry, setExpiry] = useState('')
  const [fresh, setFresh] = useState<{ token: string; key: ApiKeyInfo } | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const [copied, copy] = useCopy()
  const origin = typeof window !== 'undefined' ? window.location.origin : ''
  const endpoint = `${origin}/api/mcp`
  const snippets = useMemo(() => mcpSnippets(endpoint, fresh?.token ?? ''), [endpoint, fresh])

  const create = useMutation({
    mutationFn: () => api.createApiKey({ name: name.trim(), scope, expires_days: expiry ? Number(expiry) : null }),
    onSuccess: d => {
      qc.setQueryData(QK.apiKeys, { keys: d.keys })
      setFresh({ token: d.token, key: d.key })
      setName('')
      setMsg(null)
    },
    onError: (e: any) => setMsg(String(e?.message ?? '生成失败')),
  })
  const revoke = useMutation({
    mutationFn: (id: string) => api.revokeApiKey(id),
    onSuccess: d => {
      qc.setQueryData(QK.apiKeys, { keys: d.keys })
      if (fresh && !d.keys.some(k => k.id === fresh.key.id)) setFresh(null)
    },
    onError: (e: any) => setMsg(String(e?.message ?? '吊销失败')),
  })
  const keys = q.data?.keys ?? []

  return (
    <>
      <PageHeader title="API 与 MCP" subtitle="生成密钥，让其他 agent 或脚本接入 ZenTrade" />

      <section className="rounded-card border border-border bg-surface p-5" data-api-keys>
        <div className="mb-3 flex items-center gap-2.5">
          <KeyRound className="h-4 w-4 text-secondary" />
          <h2 className="text-sm font-medium text-foreground">API 密钥</h2>
        </div>
        <p className="mb-3 text-[11px] leading-relaxed text-secondary">
          「完全控制」与网页登录后的权限相同：回测、新建 / 编辑 / 删除策略、自选增删、规则集、纸面交易设置等都能操作。「只读」只能查询，以及跑回测、选股这类不改数据的计算。任何密钥都不能改登录密码，也不能生成或吊销密钥。
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <input className={`${INPUT_CLS} w-48`} placeholder="名称（如 Claude Desktop）" value={name}
            onChange={e => setName(e.target.value)} maxLength={60} data-api-key-name />
          <select className={INPUT_CLS} value={scope} onChange={e => setScope(e.target.value as 'full' | 'read')}
            aria-label="权限" data-api-key-scope>
            <option value="read">只读</option>
            <option value="full">完全控制</option>
          </select>
          <select className={INPUT_CLS} value={expiry} onChange={e => setExpiry(e.target.value)} aria-label="有效期">
            {EXPIRY_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
          <button type="button" onClick={() => create.mutate()} disabled={!name.trim() || create.isPending}
            data-api-key-create
            className="inline-flex h-9 items-center gap-1 rounded-btn bg-accent px-3 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50">
            <Plus className="h-3.5 w-3.5" />{create.isPending ? '生成中…' : '生成密钥'}
          </button>
        </div>
        {msg && <div className="mt-2 text-[11px] text-danger">{msg}</div>}

        {fresh && (
          <div className="mt-3 rounded-lg border border-amber-500/40 bg-amber-500/10 p-3" data-api-key-fresh>
            <div className="mb-1.5 flex items-center gap-1.5 text-[11px] font-medium text-amber-700 dark:text-amber-300">
              <ShieldAlert className="h-3.5 w-3.5" />
              新密钥「{fresh.key.name}」只显示这一次，请立即复制保存。关闭页面后无法再查看。
            </div>
            <div className="flex items-center gap-2">
              <code className="min-w-0 flex-1 break-all rounded bg-base px-2 py-1.5 font-mono text-[11px] text-foreground" data-api-key-token>
                {fresh.token}
              </code>
              <button type="button" onClick={() => copy('token', fresh.token)} aria-label="复制密钥"
                className="inline-flex h-8 shrink-0 items-center gap-1 rounded-btn bg-elevated px-2.5 text-[11px] text-secondary hover:text-foreground">
                {copied === 'token' ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
                {copied === 'token' ? '已复制' : '复制'}
              </button>
            </div>
          </div>
        )}

        <div className="mt-4">
          {q.isLoading && <div className="text-xs text-muted">加载中…</div>}
          {!q.isLoading && keys.length === 0 && (
            <div className="rounded-lg border border-dashed border-border px-3 py-4 text-center text-[11px] text-muted" data-api-keys-empty>
              还没有密钥
            </div>
          )}
          {keys.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full whitespace-nowrap text-left text-[11px]">
                <thead className="text-muted">
                  <tr className="border-b border-border">
                    <th className="py-1.5 pr-3 font-normal">名称</th>
                    <th className="py-1.5 pr-3 font-normal">权限</th>
                    <th className="py-1.5 pr-3 font-normal">密钥</th>
                    <th className="hidden py-1.5 pr-3 font-normal md:table-cell">创建</th>
                    <th className="py-1.5 pr-3 font-normal">最近使用</th>
                    <th className="hidden py-1.5 pr-3 font-normal md:table-cell">过期</th>
                    <th className="py-1.5 font-normal" />
                  </tr>
                </thead>
                <tbody>
                  {keys.map(k => (
                    <tr key={k.id} className="border-b border-border/50" data-api-key-row={k.id}>
                      <td className="py-1.5 pr-3 text-foreground">{k.name}</td>
                      <td className="py-1.5 pr-3">
                        <span className={k.scope === 'full'
                          ? 'rounded-full bg-red-500/10 px-1.5 py-px text-red-700 dark:text-red-300'
                          : 'rounded-full bg-elevated px-1.5 py-px text-secondary'}>
                          {k.scope === 'full' ? '完全控制' : '只读'}
                        </span>
                      </td>
                      <td className="py-1.5 pr-3 font-mono text-secondary">{k.prefix}…</td>
                      <td className="hidden py-1.5 pr-3 text-secondary md:table-cell">{fmtTime(k.created_at)}</td>
                      <td className="py-1.5 pr-3 text-secondary">{fmtTime(k.last_used_at)}</td>
                      <td className="hidden py-1.5 pr-3 text-secondary md:table-cell">{k.expires_at ? fmtTime(k.expires_at) : '永不'}</td>
                      <td className="py-1.5 text-right">
                        <button type="button" onClick={() => { if (window.confirm(`吊销密钥「${k.name}」？使用它的 agent 会立即失去访问。`)) revoke.mutate(k.id) }}
                          aria-label={`吊销 ${k.name}`}
                          className="inline-flex h-7 items-center gap-1 rounded-btn px-2 text-secondary hover:text-danger">
                          <Trash2 className="h-3.5 w-3.5" />吊销
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </section>

      <section className="mt-6 rounded-card border border-border bg-surface p-5" data-mcp-guide>
        <div className="mb-3 flex items-center gap-2.5">
          <Plug className="h-4 w-4 text-secondary" />
          <h2 className="text-sm font-medium text-foreground">接入 MCP</h2>
        </div>
        <p className="mb-3 text-[11px] leading-relaxed text-secondary">
          支持 MCP 的 agent（Claude Desktop、Claude Code、Cursor 等）填入下面的地址和密钥即可调用 ZenTrade：自选、策略、回测、选股、机会板、持仓有专用工具；其余功能用 zentrade_endpoints 查接口、zentrade_api 调用。传输方式为 Streamable HTTP。
        </p>
        <Snippet label="MCP 地址" text={endpoint} id="endpoint" copied={copied} copy={copy} />
        <Snippet label="Claude Code" text={snippets.claudeCode} id="cc" copied={copied} copy={copy} />
        <Snippet label="Claude Desktop / Cursor（mcpServers 配置）" text={snippets.json} id="json" copied={copied} copy={copy} multiline />
        <Snippet label="普通 HTTP 调用（所有 /api 接口都认这个头）" text={snippets.curl} id="curl" copied={copied} copy={copy} />
        <p className="mt-2 text-[10px] leading-relaxed text-muted">
          {fresh ? '上面的示例已填入刚生成的密钥。' : '生成密钥后，示例会自动填入。'}
          接口清单：{origin}/openapi.json。经公网隧道开放时，请只给可信的 agent 发「完全控制」密钥。
        </p>
      </section>
    </>
  )
}

function Snippet({ label, text, id, copied, copy, multiline }: {
  label: string; text: string; id: string; copied: string | null; copy: (k: string, t: string) => void; multiline?: boolean
}) {
  return (
    <div className="mb-2.5" data-mcp-snippet={id}>
      <div className="mb-1 flex items-center justify-between">
        <span className="text-[10px] tracking-wider text-muted">{label}</span>
        <button type="button" onClick={() => copy(id, text)} aria-label={`复制${label}`}
          className="inline-flex items-center gap-1 text-[10px] text-secondary hover:text-foreground">
          {copied === id ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}{copied === id ? '已复制' : '复制'}
        </button>
      </div>
      <pre className={`overflow-x-auto rounded bg-base px-2.5 py-2 font-mono text-[11px] leading-relaxed text-foreground ${multiline ? '' : 'whitespace-pre-wrap break-all'}`}>
        {text}
      </pre>
    </div>
  )
}
