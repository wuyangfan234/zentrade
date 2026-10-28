// @vitest-environment jsdom
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { ExtDataConfig } from '@/lib/api'
import { ExtDataPullPanel } from './ExtDataPullPanel'

function dateFormatSelect(dateFormat: string) {
  const config: ExtDataConfig = {
    id: 'date-format-test', label: 'Test', mode: 'timeseries', fields: [],
    created_at: '2026-10-01T00:00:00', updated_at: '2026-10-01T00:00:00',
    pull: {
      url: 'https://example.invalid/data', method: 'GET', response_path: '',
      schedule_minutes: 1440, enabled: false, date_param: 'date', date_format: dateFormat,
    },
  }
  const host = document.createElement('div')
  // Static rendering exercises the real form without mounting network effects.
  host.innerHTML = renderToStaticMarkup(<ExtDataPullPanel config={config} onSaved={() => {}} />)
  return host.querySelector<HTMLSelectElement>('select[aria-label="日期参数值格式"]')!
}

describe('external pull date serialization help', () => {
  it('labels epoch date parameters as ET midnight, matching the backend market clock', () => {
    const select = dateFormatSelect('ts_s')
    expect(select.title).toContain('美东时间 (ET) 00:00:00')
    expect(select.title).not.toContain('北京')
  })

  it.each(['iso', 'compact', 'ts_s', 'ts_ms'])('preserves the configured %s format and existing options', format => {
    const select = dateFormatSelect(format)
    expect(select.value).toBe(format)
    expect(Array.from(select.options, option => option.value)).toEqual(['iso', 'compact', 'ts_s', 'ts_ms'])
    expect(select.disabled).toBe(false)
  })
})
