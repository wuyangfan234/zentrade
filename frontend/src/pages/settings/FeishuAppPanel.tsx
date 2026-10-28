import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { toast } from '@/components/Toast'

/**
 * 飞书应用机器人推送 (飞书 Agent 集成能力 · Channel SDK)。
 * 凭据只在服务端环境变量 (ZENTRADE_FEISHU_APP_ID / SECRET), 页面只显示是否就绪、选择目标群、发测试。
 * 已配置时所有飞书推送 (机会卡/成交/日报/周报/监控告警) 都走这里, 不再走群 Webhook。
 */
export function FeishuAppPanel({ credentialsSet, chatId }: { credentialsSet: boolean; chatId: string }) {
  const qc = useQueryClient()
  const [draft, setDraft] = useState(chatId)
  useEffect(() => { setDraft(chatId) }, [chatId])

  const chats = useQuery({
    queryKey: ['feishu-app-chats'],
    queryFn: api.feishuAppChats,
    enabled: credentialsSet,
    staleTime: 60_000,
  })
  const save = useMutation({
    mutationFn: (id: string) => api.updateFeishuApp(id),
    onSuccess: () => {
      toast('飞书应用机器人推送目标已保存', 'success')
      qc.invalidateQueries({ queryKey: QK.preferences })
    },
  })
  const test = useMutation({ mutationFn: () => api.sendTestWebhook('feishu_app') })

  if (!credentialsSet) {
    return (
      <div className="text-[11px] text-warning">
        服务端未配置飞书应用凭据（ZENTRADE_FEISHU_APP_ID / ZENTRADE_FEISHU_APP_SECRET），应用机器人推送不可用。
      </div>
    )
  }
  const list = chats.data?.chats ?? []
  const knownName = list.find(c => c.chat_id === chatId)?.name
  return (
    <div className="space-y-2">
      <div className="text-[11px] text-muted">
        应用凭据已在服务端配置。推送目标为机器人所在的群；把机器人拉进新群后点「刷新」即可选择。
      </div>
      <div className="flex items-center gap-2">
        <select
          aria-label="推送目标群"
          value={draft}
          onChange={e => { setDraft(e.target.value); test.reset() }}
          className="h-9 min-w-0 flex-1 rounded-btn border border-border bg-base px-2 text-xs text-foreground focus:outline-none focus:border-accent/50"
        >
          <option value="">— 选择推送目标群 —</option>
          {list.map(c => <option key={c.chat_id} value={c.chat_id}>{c.name || c.chat_id}</option>)}
          {chatId && !knownName && <option value={chatId}>{chatId}（当前已保存）</option>}
        </select>
        <button
          type="button"
          onClick={() => void chats.refetch()}
          className="px-2 py-1.5 rounded-btn bg-elevated text-secondary hover:text-foreground text-xs"
        >
          {chats.isFetching ? '刷新中…' : '刷新'}
        </button>
      </div>
      {chats.data?.detail && <div className="text-[11px] text-danger">{chats.data.detail}</div>}
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => save.mutate(draft)}
          disabled={save.isPending || draft === chatId}
          className="px-3 py-1.5 rounded-btn bg-accent text-white text-xs font-medium disabled:opacity-50 hover:bg-accent/90"
        >
          {save.isPending ? '保存中…' : '保存'}
        </button>
        <button
          type="button"
          onClick={() => test.mutate()}
          disabled={test.isPending || !chatId}
          className="px-3 py-1.5 rounded-btn bg-elevated text-secondary hover:text-foreground text-xs disabled:opacity-40"
        >
          {test.isPending ? '测试中…' : '发送测试'}
        </button>
        {chatId && <span className="text-[10px] text-emerald-700 dark:text-emerald-500">● 推送到 {knownName || chatId}</span>}
      </div>
      {test.data && (
        <div className={`text-[11px] ${test.data.ok ? 'text-emerald-700 dark:text-emerald-500' : 'text-danger'}`}>{test.data.detail}</div>
      )}
    </div>
  )
}
