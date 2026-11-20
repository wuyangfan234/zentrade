// @vitest-environment jsdom
// 主题默认值与设备记忆: 没手动选过主题 = 亮色; 手动选过的按本设备存值走。
// 首屏内联脚本 (index.html) 与 getTheme() 必须同口径, 否则首屏与挂载后主题不一致 (闪一下再变色)。
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { getTheme, setTheme, toggleTheme } from './theme'

const KEY = 'tf-theme'
const isDark = () => document.documentElement.classList.contains('dark')

function runFirstPaintScript() {
  const html = readFileSync(resolve(__dirname, '../../index.html'), 'utf8')
  const m = html.match(/<script>([\s\S]*?)<\/script>/)
  if (!m) throw new Error('index.html 缺少首屏主题脚本')
  document.documentElement.classList.remove('dark')
  new Function(m[1])()
}

afterEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  document.documentElement.classList.remove('dark')
})

describe('主题默认值与设备记忆', () => {
  it('没手动选过主题时默认亮色', () => {
    expect(getTheme()).toBe('light')
  })

  it('手动选过暗色的设备保持暗色', () => {
    localStorage.setItem(KEY, 'dark')
    expect(getTheme()).toBe('dark')
  })

  it('手动选过亮色的设备保持亮色', () => {
    localStorage.setItem(KEY, 'light')
    expect(getTheme()).toBe('light')
  })

  it('读不到存值时按亮色', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('denied') })
    expect(getTheme()).toBe('light')
  })

  it('切换主题写入本设备并同步 html.dark', () => {
    setTheme('dark')
    expect(localStorage.getItem(KEY)).toBe('dark')
    expect(isDark()).toBe(true)
    expect(toggleTheme()).toBe('light')
    expect(localStorage.getItem(KEY)).toBe('light')
    expect(isDark()).toBe(false)
  })

  it('首屏内联脚本与 getTheme 同口径: 只有存值为 dark 才加暗色', () => {
    runFirstPaintScript()
    expect(isDark()).toBe(false)
    localStorage.setItem(KEY, 'light')
    runFirstPaintScript()
    expect(isDark()).toBe(false)
    localStorage.setItem(KEY, 'dark')
    runFirstPaintScript()
    expect(isDark()).toBe(true)
  })

  it('首屏内联脚本读不到存值时按亮色', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('denied') })
    runFirstPaintScript()
    expect(isDark()).toBe(false)
  })
})
