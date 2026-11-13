/** 外部推送渠道显示名 (渠道统一在 系统设置 → 推送通道, 2026-11-13) */
export const PUSH_CHANNEL_LABEL: Record<string, string> = {
  feishu: '飞书', wecom: '企业微信', custom: '第三方系统', email: '邮件',
}

export const pushChannelText = (channels: readonly string[]) =>
  channels.map(c => PUSH_CHANNEL_LABEL[c] ?? c).join('、')
