/**
 * 登录后跳转目标的安全过滤: 只接受站内相对路径, 防止 `?redirect=` 被构造成开放重定向。
 *
 * 允许: `/`、`/paper`、`/backtest?tab=evidence#x`。
 * 拒绝: 绝对 URL、协议相对 `//host`、反斜杠变体 `/\\host`、`javascript:` 等, 以及解析后跨源的任何值。
 */
export function safeRedirect(raw: string | null | undefined, fallback = '/'): string {
  const value = (raw ?? '').trim()
  if (!value.startsWith('/') || value.startsWith('//')) return fallback
  if (/[\\\u0000-\u001f]/.test(value)) return fallback
  try {
    const base = typeof window !== 'undefined' ? window.location.origin : 'http://localhost'
    const url = new URL(value, base)
    if (url.origin !== base) return fallback
    return `${url.pathname}${url.search}${url.hash}`
  } catch {
    return fallback
  }
}
