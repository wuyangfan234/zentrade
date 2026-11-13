/**
 * 推送通道 + 推送日志 (2026-11-13: 所有推送渠道统一在系统设置, 新增渠道后全部推送统一生效)。
 *
 * - 勾选 = 这个渠道接收全部推送 (成交 / 收盘与盘前机会板 / 情绪 / 周报 / 新闻 / 系统告警 / 对账 /
 *   开盘放量 / 监控规则)。后端 services/push_channels.py 是唯一来源。
 * - 新配置的渠道保存后自动勾选; 从未保存过勾选时 = 全部已配置渠道。
 * - 推送日志: 每条推送一行, 展开看每个渠道的结果与原文 (后端 /api/settings/push-log)。
 */
import { useState, useCallback, useEffect } from 'react'
import { useQueryClient, useMutation, useQuery } from '@tanstack/react-query'
import { Webhook, ChevronDown, ScrollText, RefreshCw } from 'lucide-react'
import { usePreferences } from '@/lib/useSharedQueries'
import { api, type EmailSmtpConfig, type PushChannel, type PushLogItem } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { AnchorWrap } from '@/lib/useCardFlash'
import { toast } from '@/components/Toast'
import { isServerChanUrl, pushChannelText } from '@/lib/pushChannels'
import { FeishuAppPanel } from './FeishuAppPanel'

const EMPTY_EMAIL_SMTP: EmailSmtpConfig = {
  host: '',
  port: 465,
  security: 'ssl',
  username: '',
  from_address: '',
  to_addresses: [],
}

const PUSH_SCOPE = '成交、收盘与盘前机会板、盘前情绪、周报、持仓新闻、系统告警、对账、开盘放量、监控规则'

// ===== 推送通道 =====

export function PushChannelsSection({ highlight }: { highlight?: string }) {
  const qc = useQueryClient()
  const { data: prefs } = usePreferences()
  const enabled: string[] = prefs?.push_channels ?? []
  const active: string[] = prefs?.push_channels_active ?? []
  const feishuWebhookUrl = prefs?.feishu_webhook_url ?? ''
  // 审计 2026-10-26 L-9: 后端只回掩码地址与「密钥已设置」, 不回明文; 密钥框留空 = 保持不变
  const feishuWebhookSecretSet = prefs?.feishu_webhook_secret_set ?? false
  const [feishuDraft, setFeishuDraft] = useState(feishuWebhookUrl)
  const [feishuSecretDraft, setFeishuSecretDraft] = useState('')
  const [feishuError, setFeishuError] = useState('')
  // 企业微信 webhook
  const wecomWebhookUrl = prefs?.wecom_webhook_url ?? ''
  const [wecomDraft, setWecomDraft] = useState(wecomWebhookUrl)
  const [wecomError, setWecomError] = useState('')
  // 通用第三方 JSON webhook
  const customWebhookUrl = prefs?.custom_webhook_url ?? ''
  const customWebhookSecretSet = prefs?.custom_webhook_secret_set ?? false
  const [customDraft, setCustomDraft] = useState(customWebhookUrl)
  const [customSecretDraft, setCustomSecretDraft] = useState('')
  const [customError, setCustomError] = useState('')
  // SMTP 邮件通道
  const emailSmtpConfig = prefs?.email_smtp_config ?? EMPTY_EMAIL_SMTP
  const emailSmtpPasswordSet = prefs?.email_smtp_password_set ?? false
  const emailConfigured = !!(
    emailSmtpConfig.host
    && emailSmtpConfig.from_address
    && emailSmtpConfig.to_addresses.length
    && (!emailSmtpConfig.username || emailSmtpPasswordSet)
  )
  const [emailDraft, setEmailDraft] = useState<EmailSmtpConfig>(emailSmtpConfig)
  const [emailPasswordDraft, setEmailPasswordDraft] = useState('')
  const [emailError, setEmailError] = useState('')
  // 企业微信智能机器人 (BotID + Secret, 长连接通道)
  const wecomBotId = prefs?.wecom_bot_id ?? ''
  const wecomBotSecretSet = prefs?.wecom_bot_secret_set ?? false
  const wecomBotEnabled = prefs?.wecom_bot_enabled ?? false
  const [botIdDraft, setBotIdDraft] = useState(wecomBotId)
  const [botSecretDraft, setBotSecretDraft] = useState('')
  const [botError, setBotError] = useState('')
  const [botStatus, setBotStatus] = useState<{connected: boolean; last_error: string} | null>(null)
  // 飞书应用机器人 (推荐, Channel SDK): 凭据在服务端环境变量, 这里只看是否就绪与目标群
  const feishuAppCredentialsSet = prefs?.feishu_app_credentials_set ?? false
  const feishuAppChatId = prefs?.feishu_app_chat_id ?? ''
  const feishuAppConfigured = prefs?.feishu_app_configured ?? false
  // 飞书 (应用机器人 + 群 Webhook 备用) 配置区展开态
  const [channelOpen, setChannelOpen] = useState(false)
  // 企业微信渠道配置区展开态
  const [wecomOpen, setWecomOpen] = useState(false)
  const [customOpen, setCustomOpen] = useState(false)
  const [emailOpen, setEmailOpen] = useState(false)
  // 智能机器人配置区展开态
  const [botOpen, setBotOpen] = useState(false)
  useEffect(() => {
    setFeishuDraft(feishuWebhookUrl)
    setFeishuSecretDraft('')
  }, [feishuWebhookUrl, feishuWebhookSecretSet])
  useEffect(() => {
    setWecomDraft(wecomWebhookUrl)
  }, [wecomWebhookUrl])
  useEffect(() => {
    setCustomDraft(customWebhookUrl)
    setCustomSecretDraft('')
  }, [customWebhookUrl, customWebhookSecretSet])
  useEffect(() => {
    setEmailDraft(emailSmtpConfig)
    setEmailPasswordDraft('')
  }, [emailSmtpConfig, emailSmtpPasswordSet])
  useEffect(() => {
    setBotIdDraft(wecomBotId)
    setBotSecretDraft('')
  }, [wecomBotId, wecomBotSecretSet])
  const feishuConfigured = feishuAppConfigured || !!feishuWebhookUrl

  // 勾选 / 取消勾选: 这个渠道接收 / 不再接收全部推送
  const toggleChannel = useCallback(async (ch: PushChannel, on: boolean) => {
    const next = (on ? [...enabled, ch] : enabled.filter(c => c !== ch)) as PushChannel[]
    await api.updatePushChannels(next)
    qc.invalidateQueries({ queryKey: QK.preferences })
  }, [enabled, qc])

  const saveFeishuWebhook = useMutation({
    mutationFn: ({ url, secret }: { url: string; secret?: string }) => api.updateFeishuWebhook(url, secret),
    onSuccess: () => {
      setFeishuError('')
      setFeishuSecretDraft('')
      toast('飞书 Webhook 已保存', 'success')
      qc.invalidateQueries({ queryKey: QK.preferences })
    },
    onError: (err: any) => setFeishuError(String(err?.message ?? '保存失败')),
  })
  const FEISHU_PREFIX = 'https://open.feishu.cn/open-apis/bot/v2/hook/'
  const submitFeishu = useCallback(() => {
    const url = feishuDraft.trim()
    const secret = feishuSecretDraft.trim()
    if (url && !url.startsWith(FEISHU_PREFIX)) {
      setFeishuError('地址需以 ' + FEISHU_PREFIX + ' 开头')
      return
    }
    saveFeishuWebhook.mutate({ url, ...(secret ? { secret } : {}) })
  }, [feishuDraft, feishuSecretDraft, saveFeishuWebhook])

  const saveWecomWebhook = useMutation({
    mutationFn: (url: string) => api.updateWecomWebhook(url),
    onSuccess: () => {
      setWecomError('')
      toast('企业微信 Webhook 已保存', 'success')
      qc.invalidateQueries({ queryKey: QK.preferences })
    },
    onError: (err: any) => setWecomError(String(err?.message ?? '保存失败')),
  })
  const WECOM_PREFIX = 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send'
  const submitWecom = useCallback(() => {
    const url = wecomDraft.trim()
    // 允许完整 URL 或纯 key (36位UUID样式)
    if (url && !url.startsWith(WECOM_PREFIX) && url.length < 20) {
      setWecomError('请输入完整 Webhook 地址或 key（至少 20 位）')
      return
    }
    saveWecomWebhook.mutate(url)
  }, [wecomDraft, saveWecomWebhook])

  const testFeishu = useMutation({
    mutationFn: () => api.sendTestWebhook('feishu'),
  })
  const testWecom = useMutation({
    mutationFn: () => api.sendTestWebhook('wecom'),
  })

  const saveCustomWebhook = useMutation({
    mutationFn: ({ url, secret }: { url: string; secret?: string }) => api.updateCustomWebhook(url, secret),
    onSuccess: () => {
      setCustomError('')
      setCustomSecretDraft('')
      toast('第三方 Webhook 已保存', 'success')
      qc.invalidateQueries({ queryKey: QK.preferences })
    },
    onError: (err: any) => setCustomError(String(err?.message ?? '保存失败')),
  })
  const submitCustom = useCallback(() => {
    const url = customDraft.trim()
    if (url && !/^https?:\/\//i.test(url)) {
      setCustomError('请输入完整的 HTTP(S) URL')
      return
    }
    saveCustomWebhook.mutate({
      url,
      ...(customSecretDraft ? { secret: customSecretDraft } : {}),
    })
  }, [customDraft, customSecretDraft, saveCustomWebhook])
  const testCustom = useMutation({
    mutationFn: () => api.sendTestWebhook('custom'),
  })

  const saveEmailSmtp = useMutation({
    mutationFn: ({ config, password }: { config: EmailSmtpConfig; password?: string }) =>
      api.updateEmailSmtp(config, password),
    onSuccess: () => {
      setEmailError('')
      setEmailPasswordDraft('')
      toast('邮件推送配置已保存', 'success')
      qc.invalidateQueries({ queryKey: QK.preferences })
    },
    onError: (err: any) => setEmailError(String(err?.message ?? '保存失败')),
  })
  const submitEmail = useCallback(() => {
    saveEmailSmtp.mutate({
      config: {
        ...emailDraft,
        host: emailDraft.host.trim(),
        username: emailDraft.username.trim(),
        from_address: emailDraft.from_address.trim(),
        to_addresses: emailDraft.to_addresses.map(item => item.trim()).filter(Boolean),
      },
      ...(emailPasswordDraft ? { password: emailPasswordDraft } : {}),
    })
  }, [emailDraft, emailPasswordDraft, saveEmailSmtp])
  const testEmail = useMutation({
    mutationFn: () => api.sendTestWebhook('email'),
  })

  // 智能机器人 (BotID + Secret) 保存 → 后端立即重建连接
  const saveWecomBot = useMutation({
    mutationFn: ({ botId, secret }: { botId: string; secret?: string }) =>
      api.updateWecomBot(botId, secret, true),
    onSuccess: (data) => {
      setBotError('')
      setBotSecretDraft('')
      toast('智能机器人凭证已保存 · 连接中', 'success')
      setBotStatus({
        connected: data.wecom_bot_status?.connected ?? false,
        last_error: data.wecom_bot_status?.last_error ?? '',
      })
      qc.invalidateQueries({ queryKey: QK.preferences })
    },
    onError: (err: any) => setBotError(String(err?.message ?? '保存失败')),
  })
  const submitBot = useCallback(() => {
    const secret = botSecretDraft.trim()
    saveWecomBot.mutate({ botId: botIdDraft.trim(), ...(secret ? { secret } : {}) })
  }, [botIdDraft, botSecretDraft, saveWecomBot])

  // 智能机器人长连接开关(不改动凭证): 开启→连接, 关闭→断开
  const toggleBotConnection = useMutation({
    mutationFn: (enabled: boolean) => api.toggleWecomBot(enabled),
    onSuccess: (data) => {
      setBotStatus({
        connected: data.wecom_bot_status?.connected ?? false,
        last_error: data.wecom_bot_status?.last_error ?? '',
      })
      qc.invalidateQueries({ queryKey: QK.preferences })
    },
  })

  return (
    <div className="mt-6">
    <AnchorWrap highlight={highlight} anchor="push">
      <section className="rounded-card border border-border bg-surface p-5" data-push-channels>
        <div className="flex items-center gap-2 mb-2">
          <Webhook className="h-4 w-4 text-accent" />
          <h3 className="text-sm font-medium text-foreground">推送通道</h3>
        </div>
        <p className="text-xs text-secondary leading-relaxed">
          勾选的渠道接收<b className="text-foreground/80">全部推送</b>：{PUSH_SCOPE}。新配置的渠道保存后自动勾选。
        </p>
        <p data-push-active className={`mt-1.5 mb-3 text-[11px] ${active.length ? 'text-emerald-700 dark:text-emerald-500' : 'text-warning'}`}>
          {active.length
            ? `当前发往：${pushChannelText(active)}`
            : '当前没有可用渠道：推送不会发出，会在下方推送日志里显示「未配置」'}
        </p>

        <div className="space-y-2">
            {/* 飞书: 应用机器人优先; 群 Webhook 只在应用机器人未配置时使用 */}
            <div className="rounded-btn border border-accent/40 bg-base/40 overflow-hidden">
              <div
                onClick={() => setChannelOpen(o => !o)}
                className="flex items-center gap-2 px-2.5 py-2 cursor-pointer transition-colors hover:bg-base/60"
              >
                <input
                  type="checkbox"
                  aria-label="飞书"
                  checked={enabled.includes('feishu')}
                  onChange={e => { e.stopPropagation(); void toggleChannel('feishu', e.target.checked) }}
                  onClick={e => e.stopPropagation()}
                  title="勾选 = 全部推送都发到这个渠道"
                  className="h-3 w-3 accent-accent cursor-pointer"
                />
                <span className="text-[11px] font-medium text-foreground">飞书</span>
                <span className="text-[11px] text-muted">
                  {feishuAppConfigured ? '应用机器人' : feishuWebhookUrl ? '群 Webhook' : '应用机器人 / 群 Webhook'}
                </span>
                <span className={`ml-auto text-[11px] ${feishuConfigured ? 'text-emerald-700 dark:text-emerald-500' : 'text-warning'}`}>
                  {feishuConfigured ? '已配置' : feishuAppCredentialsSet ? '待选择群' : '未配置'}
                </span>
                <ChevronDown className={`h-3 w-3 text-muted transition-transform ${channelOpen ? 'rotate-180' : ''}`} />
              </div>

              {channelOpen && (
                <div className="border-t border-border/60 bg-base/30 p-3 space-y-4">
                  <div>
                    <div className="mb-2 flex items-center gap-2">
                      <span className="text-[11px] font-medium text-foreground">应用机器人</span>
                      <span className="rounded bg-accent/15 px-1 py-px text-[11px] text-accent dark:text-blue-400">推荐</span>
                    </div>
                    <FeishuAppPanel credentialsSet={feishuAppCredentialsSet} chatId={feishuAppChatId} />
                  </div>
                  <div className="border-t border-border/60 pt-3">
                    <div className="mb-2 text-[11px] font-medium text-foreground">
                      群 Webhook
                      <span className="ml-1 font-normal text-muted">
                        {feishuAppConfigured ? '· 应用机器人已配置，此项不使用' : '· 应用机器人未配置时使用'}
                      </span>
                    </div>
                      <label className="block space-y-1.5">
                        <span className="text-[11px] text-muted">Webhook 地址</span>
                        <input
                          value={feishuDraft}
                          onChange={e => { setFeishuDraft(e.target.value); if (!testFeishu.isPending) testFeishu.reset() }}
                          placeholder={FEISHU_PREFIX + 'xxxxxxxx'}
                          className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground focus:outline-none focus:border-accent/50"
                        />
                      </label>

                      <label className="block mt-2 space-y-1.5">
                        <span className="text-[11px] text-muted">签名密钥（可选 · 启用签名校验时填写）</span>
                        <input
                          type="password"
                          value={feishuSecretDraft}
                          onChange={e => { setFeishuSecretDraft(e.target.value); if (!testFeishu.isPending) testFeishu.reset() }}
                          placeholder={feishuWebhookSecretSet ? '已保存；留空保持不变' : '机器人未启用签名校验则留空'}
                          className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground focus:outline-none focus:border-accent/50"
                        />
                      </label>

                      {feishuError && (
                        <div className="mt-2 text-[11px] text-danger">{feishuError}</div>
                      )}

                      <div className="mt-2 flex items-center gap-2">
                        <button
                          onClick={submitFeishu}
                          disabled={saveFeishuWebhook.isPending || (feishuDraft.trim() === feishuWebhookUrl && !feishuSecretDraft.trim())}
                          className="px-3 py-1.5 rounded-btn bg-accent dark:bg-blue-700 dark:hover:bg-blue-600 text-white text-xs font-medium disabled:opacity-50 cursor-pointer hover:bg-accent/90 transition-colors"
                        >
                          {saveFeishuWebhook.isPending ? '保存中…' : '保存'}
                        </button>
                        {feishuWebhookUrl && feishuWebhookSecretSet && (
                          <button
                            type="button"
                            onClick={() => saveFeishuWebhook.mutate({ url: feishuWebhookUrl, secret: '' })}
                            disabled={saveFeishuWebhook.isPending}
                            className="px-2 py-1.5 rounded-btn border border-border text-xs text-secondary disabled:opacity-50 cursor-pointer hover:text-foreground"
                          >
                            清除签名密钥
                          </button>
                        )}
                        <TestSendButton test={testFeishu} configured={!!feishuWebhookUrl} />
                        {feishuWebhookUrl && (
                          <span className="text-[11px] text-emerald-700 dark:text-emerald-500">● 已配置</span>
                        )}
                        <TestResult test={testFeishu} />
                      </div>

                      <details className="mt-3 text-[11px] text-muted">
                        <summary className="cursor-pointer hover:text-secondary">如何获取飞书 Webhook 地址?</summary>
                        <ol className="mt-1.5 space-y-1 pl-4 list-decimal leading-relaxed">
                          <li>打开飞书，进入目标群聊 → 群设置 → <b>群推送 Webhook</b></li>
                          <li>点击「添加机器人」→ 选择「<b>自定义机器人</b>」</li>
                          <li>填写机器人名称后添加，复制生成的 Webhook 地址</li>
                          <li>安全设置若启用了「<b>签名校验</b>」，把密钥一并复制填到「签名密钥」框</li>
                          <li>粘贴到上方输入框并保存</li>
                        </ol>
                        <p className="mt-1.5 pl-4 text-muted">
                          官方文档:
                          <a href="https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot?lang=zh-CN" target="_blank" rel="noreferrer" className="text-accent hover:text-accent/80">
                            自定义机器人使用指南 ↗
                          </a>
                        </p>
                      </details>
                  </div>
                </div>
              )}
            </div>

            {/* 企业微信群推送 Webhook (可用): 与飞书并列, 勾选默认 + 展开地址配置 */}
            <div className="rounded-btn border border-border/60 bg-base/40 overflow-hidden">
              <div
                onClick={() => setWecomOpen(o => !o)}
                className="flex items-center gap-2 px-2.5 py-2 cursor-pointer transition-colors hover:bg-base/60"
              >
                <input
                  type="checkbox"
                  checked={enabled.includes('wecom')}
                  onChange={e => { e.stopPropagation(); void toggleChannel('wecom', e.target.checked) }}
                  onClick={e => e.stopPropagation()}
                  title="勾选 = 全部推送都发到这个渠道"
                  className="h-3 w-3 accent-accent cursor-pointer"
                />
                <span className="text-[11px] font-medium text-foreground">企业微信</span>
                <span className="text-[11px] text-muted">群推送 Webhook</span>
                <span className={`ml-auto text-[11px] ${wecomWebhookUrl ? 'text-emerald-700 dark:text-emerald-500' : 'text-warning'}`}>
                  {wecomWebhookUrl ? '已配置' : '未配置'}
                </span>
                <ChevronDown className={`h-3 w-3 text-muted transition-transform ${wecomOpen ? 'rotate-180' : ''}`} />
              </div>

              {wecomOpen && (
                <div className="border-t border-border/60 bg-base/30 p-3">
                  <label className="block space-y-1.5">
                    <span className="text-[11px] text-muted">Webhook 地址 或 Key</span>
                    <input
                      value={wecomDraft}
                      onChange={e => { setWecomDraft(e.target.value); if (!testWecom.isPending) testWecom.reset() }}
                      placeholder={WECOM_PREFIX + '?key=xxxxxxxx' + ' 或直接填 key'}
                      className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground focus:outline-none focus:border-accent/50"
                    />
                  </label>

                  {wecomError && (
                    <div className="mt-2 text-[11px] text-danger">{wecomError}</div>
                  )}

                  <div className="mt-2 flex items-center gap-2">
                    <button
                      onClick={submitWecom}
                      disabled={saveWecomWebhook.isPending || wecomDraft.trim() === wecomWebhookUrl}
                      className="px-3 py-1.5 rounded-btn bg-accent dark:bg-blue-700 dark:hover:bg-blue-600 text-white text-xs font-medium disabled:opacity-50 cursor-pointer hover:bg-accent/90 transition-colors"
                    >
                      {saveWecomWebhook.isPending ? '保存中…' : '保存'}
                    </button>
                    <TestSendButton test={testWecom} configured={!!wecomWebhookUrl} />
                    {wecomWebhookUrl && (
                      <span className="text-[11px] text-emerald-700 dark:text-emerald-500">● 已配置</span>
                    )}
                    <TestResult test={testWecom} />
                  </div>

                  <details className="mt-3 text-[11px] text-muted">
                    <summary className="cursor-pointer hover:text-secondary">如何获取企业微信 Webhook 地址?</summary>
                    <ol className="mt-1.5 space-y-1 pl-4 list-decimal leading-relaxed">
                      <li>打开企业微信，进入目标群聊 → 右上角「...」→ <b>群推送 Webhook</b></li>
                      <li>点击「添加」→ 选择「<b>消息推送</b>」→ 填写名字</li>
                      <li>复制生成的 <b>Webhook 地址</b>(含 key 参数),粘贴到上方输入框</li>
                      <li>也可只复制 key 参数部分(= 后面的内容)填入</li>
                      <li>企业微信群的消息可同步到绑定的个人微信，实现"微信推送"</li>
                    </ol>
                    <p className="mt-1.5 pl-4 text-muted">
                      官方文档:
                      <a href="https://developer.work.weixin.qq.com/document/path/91770" target="_blank" rel="noreferrer" className="text-accent hover:text-accent/80">
                        群推送 Webhook 使用指南 ↗
                      </a>
                    </p>
                  </details>
                </div>
              )}
            </div>

            {/* 通用第三方 JSON Webhook */}
            <div className="rounded-btn border border-border/60 bg-base/40 overflow-hidden">
              <div
                onClick={() => setCustomOpen(o => !o)}
                className="flex items-center gap-2 px-2.5 py-2 cursor-pointer transition-colors hover:bg-base/60"
              >
                <input
                  type="checkbox"
                  checked={enabled.includes('custom')}
                  onChange={e => { e.stopPropagation(); void toggleChannel('custom', e.target.checked) }}
                  onClick={e => e.stopPropagation()}
                  title="勾选 = 全部推送都发到这个渠道"
                  className="h-3 w-3 accent-accent cursor-pointer"
                />
                <span className="text-[11px] font-medium text-foreground">第三方系统</span>
                <span className="text-[11px] text-muted" data-custom-kind>{isServerChanUrl(customWebhookUrl) ? 'Server酱' : 'JSON Webhook'}</span>
                <span className={`ml-auto text-[11px] ${customWebhookUrl ? 'text-emerald-700 dark:text-emerald-500' : 'text-warning'}`}>
                  {customWebhookUrl ? '已配置' : '未配置'}
                </span>
                <ChevronDown className={`h-3 w-3 text-muted transition-transform ${customOpen ? 'rotate-180' : ''}`} />
              </div>

              {customOpen && (
                <div className="border-t border-border/60 bg-base/30 p-3">
                  <label className="block space-y-1.5">
                    <span className="text-[11px] text-muted">接收端 URL</span>
                    <input
                      value={customDraft}
                      onChange={e => { setCustomDraft(e.target.value); if (!testCustom.isPending) testCustom.reset() }}
                      placeholder="https://example.com/webhooks/zentrade"
                      className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground focus:outline-none focus:border-accent/50"
                    />
                  </label>
                  <label className="block mt-2 space-y-1.5">
                    <span className="text-[11px] text-muted">签名密钥（可选 · HMAC-SHA256）</span>
                    <input
                      type="password"
                      value={customSecretDraft}
                      onChange={e => setCustomSecretDraft(e.target.value)}
                      placeholder={customWebhookSecretSet ? '已保存；留空保持不变' : '留空则不签名'}
                      className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground focus:outline-none focus:border-accent/50"
                    />
                  </label>
                  {customError && <div className="mt-2 text-[11px] text-danger">{customError}</div>}
                  <div className="mt-2 flex items-center gap-2">
                    <button
                      onClick={submitCustom}
                      disabled={saveCustomWebhook.isPending || (customDraft.trim() === customWebhookUrl && !customSecretDraft)}
                      className="px-3 py-1.5 rounded-btn bg-accent dark:bg-blue-700 dark:hover:bg-blue-600 text-white text-xs font-medium disabled:opacity-50 cursor-pointer hover:bg-accent/90 transition-colors"
                    >
                      {saveCustomWebhook.isPending ? '保存中…' : '保存'}
                    </button>
                    <TestSendButton test={testCustom} configured={!!customWebhookUrl} />
                    <TestResult test={testCustom} />
                  </div>
                  <details className="mt-3 text-[11px] text-muted">
                    <summary className="cursor-pointer hover:text-secondary">请求格式</summary>
                    <p className="mt-1.5 leading-relaxed">
                      POST JSON 包含 event、timestamp、title、subtitle、body、data；User-Agent 为 ZenTrade-Webhook/1.0。
                      配置密钥后会附带 X-ZenTrade-Timestamp 和 X-ZenTrade-Signature（sha256=HMAC 十六进制）请求头。
                    </p>
                    <p className="mt-1.5 leading-relaxed">
                      填 Server酱 地址（https://sctapi.ftqq.com/&lt;SendKey&gt;.send，或 Server酱³ 的 push.ft07.com 地址）时，
                      自动按 Server酱 格式发送：标题 + Markdown 正文，返回 code 不为 0 记失败。Server酱 免费版每天 5 条，超出的推送会失败。
                    </p>
                  </details>
                </div>
              )}
            </div>

            {/* SMTP 邮件推送 */}
            <div className="rounded-btn border border-border/60 bg-base/40 overflow-hidden">
              <div
                onClick={() => setEmailOpen(o => !o)}
                className="flex items-center gap-2 px-2.5 py-2 cursor-pointer transition-colors hover:bg-base/60"
              >
                <input
                  type="checkbox"
                  checked={enabled.includes('email')}
                  onChange={e => { e.stopPropagation(); void toggleChannel('email', e.target.checked) }}
                  onClick={e => e.stopPropagation()}
                  title="勾选 = 全部推送都发到这个渠道"
                  className="h-3 w-3 accent-accent cursor-pointer"
                />
                <span className="text-[11px] font-medium text-foreground">邮件</span>
                <span className="text-[11px] text-muted">SMTP</span>
                <span className={`ml-auto text-[11px] ${emailConfigured ? 'text-emerald-700 dark:text-emerald-500' : 'text-warning'}`}>
                  {emailConfigured ? '已配置' : '未配置'}
                </span>
                <ChevronDown className={`h-3 w-3 text-muted transition-transform ${emailOpen ? 'rotate-180' : ''}`} />
              </div>

              {emailOpen && (
                <div className="border-t border-border/60 bg-base/30 p-3">
                  <div className="grid grid-cols-1 gap-2 sm:grid-cols-[1fr_6rem_8rem]">
                    <label className="space-y-1.5">
                      <span className="text-[11px] text-muted">SMTP 主机</span>
                      <input value={emailDraft.host} onChange={e => setEmailDraft(d => ({ ...d, host: e.target.value }))} placeholder="smtp.example.com" className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground" />
                    </label>
                    <label className="space-y-1.5">
                      <span className="text-[11px] text-muted">端口</span>
                      <input type="number" min={1} max={65535} value={emailDraft.port} onChange={e => setEmailDraft(d => ({ ...d, port: Number(e.target.value) }))} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
                    </label>
                    <label className="space-y-1.5">
                      <span className="text-[11px] text-muted">加密</span>
                      <select value={emailDraft.security} onChange={e => setEmailDraft(d => ({ ...d, security: e.target.value as EmailSmtpConfig['security'] }))} className="h-9 w-full rounded-btn border border-border bg-base px-2 text-xs text-foreground">
                        <option value="ssl">SSL</option>
                        <option value="starttls">STARTTLS</option>
                        <option value="none">无</option>
                      </select>
                    </label>
                  </div>
                  <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
                    <label className="space-y-1.5">
                      <span className="text-[11px] text-muted">登录用户名（可选）</span>
                      <input value={emailDraft.username} onChange={e => setEmailDraft(d => ({ ...d, username: e.target.value }))} placeholder="bot@example.com" className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground" />
                    </label>
                    <label className="space-y-1.5">
                      <span className="text-[11px] text-muted">密码 / 授权码（可选）</span>
                      <input type="password" value={emailPasswordDraft} onChange={e => setEmailPasswordDraft(e.target.value)} placeholder={emailSmtpPasswordSet ? '已保存 · 留空则不变' : '无需认证时可留空'} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground" />
                    </label>
                    <label className="space-y-1.5">
                      <span className="text-[11px] text-muted">发件人</span>
                      <input value={emailDraft.from_address} onChange={e => setEmailDraft(d => ({ ...d, from_address: e.target.value }))} placeholder="bot@example.com" className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground" />
                    </label>
                    <label className="space-y-1.5">
                      <span className="text-[11px] text-muted">收件人（多个用逗号分隔）</span>
                      <input value={emailDraft.to_addresses.join(', ')} onChange={e => setEmailDraft(d => ({ ...d, to_addresses: e.target.value.split(/[,;，；\n]/) }))} placeholder="alerts@example.com" className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground" />
                    </label>
                  </div>
                  {emailError && <div className="mt-2 text-[11px] text-danger">{emailError}</div>}
                  <div className="mt-2 flex items-center gap-2">
                    <button onClick={submitEmail} disabled={saveEmailSmtp.isPending} className="px-3 py-1.5 rounded-btn bg-accent dark:bg-blue-700 dark:hover:bg-blue-600 text-white text-xs font-medium disabled:opacity-50 cursor-pointer hover:bg-accent/90 transition-colors">
                      {saveEmailSmtp.isPending ? '保存中…' : '保存'}
                    </button>
                    <TestSendButton test={testEmail} configured={emailConfigured} />
                    <TestResult test={testEmail} />
                  </div>
                </div>
              )}
            </div>
        </div>

        <div className="mt-4 border-t border-border/60 pt-3">
            {/* 企业微信智能机器人 (BotID + Secret): 长连接通道, 与群推送 Webhook 并列 */}
            <div className="rounded-btn border border-border/60 bg-base/40 overflow-hidden">
              <div
                onClick={() => setBotOpen(o => !o)}
                className="flex items-center gap-2 px-2.5 py-2 cursor-pointer transition-colors hover:bg-base/60"
              >
                <input
                  type="checkbox"
                  checked={wecomBotEnabled}
                  onChange={e => { e.stopPropagation(); toggleBotConnection.mutate(e.target.checked) }}
                  onClick={e => e.stopPropagation()}
                  disabled={!wecomBotId || toggleBotConnection.isPending}
                  title="开启后建立长连接保活，关闭则断开"
                  className="h-3 w-3 accent-accent cursor-pointer disabled:opacity-40"
                />
                <span className="shrink-0 whitespace-nowrap text-[11px] font-medium text-foreground">企业微信</span>
                <span className="shrink-0 whitespace-nowrap text-[11px] text-muted">智能机器人</span>
                {/* 手机宽度放不下长标签 (会挤成两行): 窄屏用短说法 */}
                <span className="min-w-0 truncate rounded bg-elevated px-1 py-px text-[11px] text-muted">
                  <span className="sm:hidden">不参与推送</span>
                  <span className="hidden sm:inline">只收消息，不参与推送</span>
                </span>
                <span className={`ml-auto shrink-0 whitespace-nowrap text-[11px] ${wecomBotId ? (botStatus?.connected ? 'text-emerald-700 dark:text-emerald-500' : 'text-warning') : 'text-muted'}`}>
                  {wecomBotId ? (botStatus?.connected ? '已连接' : (wecomBotEnabled ? '连接中' : '已配置')) : '未配置'}
                </span>
                <ChevronDown className={`h-3 w-3 text-muted transition-transform ${botOpen ? 'rotate-180' : ''}`} />
              </div>

              {botOpen && (
                <div className="border-t border-border/60 bg-base/30 p-3">
                  <p className="mb-2.5 text-[11px] text-muted leading-relaxed">
                    勾选卡片左侧开关可启用长连接保活(开启后后端持续保持与企业微信的
                    WebSocket 连接)。保存凭证后需勾选才会连接，取消勾选则立即断开。
                  </p>
                  <label className="block space-y-1.5">
                    <span className="text-[11px] text-muted">BotID</span>
                    <input
                      value={botIdDraft}
                      onChange={e => setBotIdDraft(e.target.value)}
                      placeholder="智能机器人的唯一标识"
                      className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground focus:outline-none focus:border-accent/50"
                    />
                  </label>

                  <label className="block mt-2 space-y-1.5">
                    <span className="text-[11px] text-muted">Secret（长连接密钥）</span>
                    <input
                      type="password"
                      value={botSecretDraft}
                      onChange={e => setBotSecretDraft(e.target.value)}
                      placeholder={wecomBotSecretSet ? '已保存；留空保持不变' : '开启长连接 API 模式后获取的密钥'}
                      className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground focus:outline-none focus:border-accent/50"
                    />
                  </label>

                  {botError && (
                    <div className="mt-2 text-[11px] text-danger">{botError}</div>
                  )}

                  {botStatus?.last_error && !botError && (
                    <div className="mt-2 text-[11px] text-warning">连接异常: {botStatus.last_error}</div>
                  )}

                  <div className="mt-2 flex items-center gap-2">
                    <button
                      onClick={submitBot}
                      disabled={saveWecomBot.isPending || (botIdDraft.trim() === wecomBotId && !botSecretDraft.trim())}
                      className="px-3 py-1.5 rounded-btn bg-accent dark:bg-blue-700 dark:hover:bg-blue-600 text-white text-xs font-medium disabled:opacity-50 cursor-pointer hover:bg-accent/90 transition-colors"
                    >
                      {saveWecomBot.isPending ? '保存中…' : '保存并连接'}
                    </button>
                    {wecomBotId && (
                      <span className="text-[11px] text-emerald-700 dark:text-emerald-500">● 已配置</span>
                    )}
                  </div>

                  <details className="mt-3 text-[11px] text-muted">
                    <summary className="cursor-pointer hover:text-secondary">如何获取 BotID 和 Secret?</summary>
                    <ol className="mt-1.5 space-y-1 pl-4 list-decimal leading-relaxed">
                      <li>登录<b>企业微信管理后台</b> → 应用管理 → <b>智能机器人</b> → 创建机器人</li>
                      <li>填写名称、头像后进入机器人配置页</li>
                      <li>开启「<b>API 模式</b>」→ 选择「<b>长连接</b>」方式(另一项"回调URL"需公网IP)</li>
                      <li>页面显示 <b>BotID</b> 和 <b>Secret</b>, 复制填到上方输入框</li>
                    </ol>
                    <p className="mt-1.5 pl-4 text-muted">
                      支持 @ 交互与流式回复；群推送 Webhook 为单向推送。保存后自动建立长连接。
                    </p>
                    <p className="mt-1.5 pl-4 text-muted">
                      官方文档:
                      <a href="https://developer.work.weixin.qq.com/document/path/101463" target="_blank" rel="noreferrer" className="text-accent hover:text-accent/80">
                        智能机器人长连接 ↗
                      </a>
                    </p>
                  </details>
                </div>
              )}
            </div>
        </div>
      </section>
    </AnchorWrap>
    </div>
  )
}

// ===== 推送测试按钮 + 内联结果 =====

function TestSendButton({ test, configured }: {
  test: { isPending: boolean; mutate: () => void }
  configured: boolean
}) {
  return (
    <button
      onClick={() => test.mutate()}
      disabled={test.isPending || !configured}
      title={!configured ? '请先配置该渠道' : '发送测试消息'}
      className="inline-flex items-center gap-1 px-3 py-1.5 rounded-btn bg-elevated text-secondary hover:text-foreground text-xs disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
    >
      {test.isPending ? '测试中…' : '测试'}
    </button>
  )
}

function TestResult({ test }: {
  test: { data?: { ok: boolean; detail: string } | null; isError: boolean; error?: Error | null; reset: () => void }
}) {
  // 成功结果 2 秒后自动消失; 失败保留, 便于阅读
  useEffect(() => {
    if (test.data?.ok) {
      const t = window.setTimeout(test.reset, 2000)
      return () => window.clearTimeout(t)
    }
  }, [test.data, test.reset])

  let text: string | null = null
  let tone = ''
  if (test.isError) {
    text = String(test.error?.message ?? '发送失败')
    tone = 'text-danger'
  } else if (test.data) {
    text = (test.data.ok ? '✓ ' : '✗ ') + test.data.detail
    tone = test.data.ok ? 'text-emerald-700 dark:text-emerald-500' : 'text-danger'
  }
  if (!text) return null
  return <span className={`min-w-0 text-[11px] leading-snug ${tone}`}>{text}</span>
}

// ===== 推送日志 =====

const ET_FMT = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'America/New_York', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
})
const BJ_FMT = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
})
const fmtTs = (f: Intl.DateTimeFormat, ts: number | null | undefined) =>
  ts == null ? '—' : f.format(new Date(ts * 1000)).replace(/\//g, '-')

const STATUS_TONE: Record<PushLogItem['status'], string> = {
  sent: 'text-emerald-700 dark:text-emerald-500',
  failed: 'bg-danger/10 text-danger',
  pending: 'bg-elevated text-secondary',
  unconfigured: 'bg-warning/10 text-warning',
}
const ERROR_LABEL: Record<string, string> = {
  send_failed: '对方未受理',
  TimeoutError: '超时',
  RuntimeError: '发送通道忙或异常',
}
const errText = (e: string | null) => (e ? ERROR_LABEL[e] ?? e : '')

export function PushLogSection() {
  // 默认折叠: 推送日志很长, 需要看时再点开; 每次进页面都从折叠开始, 不记忆展开状态
  const [expanded, setExpanded] = useState(false)
  const [onlyFailed, setOnlyFailed] = useState(false)
  const [open, setOpen] = useState<string | null>(null)
  // 折叠时只取条数 (limit=1), 不拉 100 条原文; 点开后才加载列表
  const summary = useQuery({
    queryKey: [...QK.pushLog, 'summary'],
    queryFn: () => api.pushLog({ limit: 1 }),
    refetchInterval: 60_000,
    enabled: !expanded,
  })
  const log = useQuery({
    queryKey: [...QK.pushLog, 'list', onlyFailed],
    queryFn: () => api.pushLog({ limit: 100, failed: onlyFailed }),
    refetchInterval: 30_000,
    enabled: expanded,
  })
  const counts = expanded && log.data ? log.data : summary.data
  const items = log.data?.items ?? []
  return (
    <section className="rounded-card border border-border bg-surface p-5 mt-6" data-push-log>
      <h3 className="text-sm font-medium text-foreground">
        <button
          type="button"
          data-push-log-toggle=""
          aria-expanded={expanded}
          aria-controls="push-log-body"
          onClick={() => { setExpanded(v => !v); setOpen(null) }}
          className="group flex w-full min-w-0 cursor-pointer items-center gap-2 text-left"
        >
          <ScrollText className="h-4 w-4 shrink-0 text-accent" />
          <span className="shrink-0">推送日志</span>
          {counts && (
            <span className="min-w-0 truncate text-[11px] font-normal text-muted">共 {counts.total} 条 · 失败 {counts.failed}</span>
          )}
          <span className="ml-auto flex shrink-0 items-center gap-0.5 rounded-btn px-1.5 py-0.5 text-[11px] font-normal text-accent group-hover:bg-accent/10 dark:text-blue-400">
            {expanded ? '收起' : '展开'}
            <ChevronDown className={`h-3.5 w-3.5 transition-transform ${expanded ? 'rotate-180' : ''}`} />
          </span>
        </button>
      </h3>
      {expanded && (
        <div id="push-log-body" className="mt-3">
          <div className="mb-2 flex items-center gap-1.5">
            {([false, true] as const).map(v => (
              <button
                key={String(v)}
                type="button"
                aria-pressed={onlyFailed === v}
                onClick={() => { setOnlyFailed(v); setOpen(null) }}
                className={`px-2 py-1 rounded-btn text-[11px] ${onlyFailed === v ? 'bg-accent/15 text-accent dark:text-blue-400' : 'text-secondary hover:text-foreground'}`}
              >
                {v ? '只看未成功' : '全部'}
              </button>
            ))}
            <button
              type="button"
              aria-label="刷新推送日志"
              onClick={() => void log.refetch()}
              className="p-1 rounded-btn text-secondary hover:text-foreground"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${log.isFetching ? 'animate-spin' : ''}`} />
            </button>
          </div>
          <p className="mb-3 text-[11px] text-muted leading-relaxed">
            每条推送一行，最新在前（最多 100 条）；时间为美东，点开看北京时间、各渠道结果和原文。「✓」= 渠道接口已受理。
          </p>
          {log.isLoading && <div className="py-6 text-center text-[11px] text-muted">读取中…</div>}
          {log.isError && <div className="text-[11px] text-danger">推送日志读取失败</div>}
          {!log.isLoading && !log.isError && items.length === 0 && (
            <div className="py-6 text-center text-[11px] text-muted">{onlyFailed ? '没有未成功的推送' : '还没有推送记录'}</div>
          )}
          {items.length > 0 && (
            <ul className="divide-y divide-border/60 rounded-btn border border-border/60">
            {items.map(it => (
              <li key={it.key} data-push-log-row={it.key}>
                <button
                  type="button"
                  onClick={() => setOpen(o => (o === it.key ? null : it.key))}
                  aria-expanded={open === it.key}
                  className="grid w-full grid-cols-[4.75rem_4.5rem_minmax(0,1fr)] sm:grid-cols-[4.75rem_5rem_minmax(0,1fr)_auto] items-center gap-x-2 gap-y-1 px-2.5 py-1.5 text-left hover:bg-base/60"
                >
                  <span className="font-mono text-[11px] text-muted tabular-nums">{fmtTs(ET_FMT, it.created_at)}</span>
                  <span className="truncate text-[11px] text-secondary">{it.kind_label}</span>
                  <span className="min-w-0 truncate text-xs text-foreground">
                    {it.title.replace(/^ZenTrade · /, '')}
                    {it.subtitle && <span className="text-muted"> · {it.subtitle}</span>}
                  </span>
                  <span className="col-span-3 sm:col-span-1 flex flex-wrap items-center gap-1 sm:justify-end">
                    {it.channels.map(c => (
                      <span
                        key={c.channel}
                        title={c.ok ? '已受理' : errText(c.error)}
                        className={`rounded px-1 py-px text-[11px] ${c.ok ? 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-500' : 'bg-danger/10 text-danger'}`}
                      >
                        {c.ok ? '✓' : '✗'} {c.label}
                      </span>
                    ))}
                    {/* 已有渠道结果时 ✓/✗ 已说明一切, 只在没有渠道结果或已停止重试时再标状态 */}
                    {it.status !== 'sent' && (it.channels.length === 0 || it.exhausted) && (
                      <span className={`rounded px-1 py-px text-[11px] ${STATUS_TONE[it.status]}`}>
                        {it.status === 'unconfigured' ? '未发送 · 无可用渠道' : it.exhausted ? '失败 · 已停止重试' : it.label}
                      </span>
                    )}
                    <ChevronDown className={`h-3 w-3 shrink-0 text-muted transition-transform ${open === it.key ? 'rotate-180' : ''}`} />
                  </span>
                </button>
                {open === it.key && (
                  <div className="mx-2.5 mb-2.5 space-y-1 rounded-btn bg-base/50 p-2.5 text-[11px] text-secondary">
                    <div>
                      美东 {fmtTs(ET_FMT, it.created_at)} · 北京 {fmtTs(BJ_FMT, it.created_at)} · {it.label} · 发送 {it.attempts} 次
                    </div>
                    {it.channels.map(c => (
                      <div key={c.channel}>
                        {c.label}：{c.ok ? '已受理' : `失败（${errText(c.error)}）`} · 北京 {fmtTs(BJ_FMT, c.at)}
                        {c.tries > 1 ? ` · 共 ${c.tries} 次` : ''}
                        {c.legacy ? ' · 推送日志上线前的记录' : ''}
                      </div>
                    ))}
                    <pre className="mt-1 max-h-72 overflow-auto whitespace-pre-wrap break-words rounded bg-base/60 p-2 text-[11px] text-secondary">{it.body}</pre>
                  </div>
                )}
              </li>
            ))}
            </ul>
          )}
        </div>
      )}
    </section>
  )
}
