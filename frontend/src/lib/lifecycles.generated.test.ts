import { describe, expect, it } from 'vitest'
import { LIFECYCLE_LABELS, lifecycleLabel } from './lifecycles.generated'

describe('lifecycleLabel', () => {
  it('keeps the mirror wording the portfolio page already used', () => {
    const before: Record<string, string> = {
      new: '待提交', submitted: '已提交', filled: '已成交', rejected: '被拒', skipped_not_mirrored: '未镜像（开启前的持仓）',
      skipped_zero_qty: '数量为 0', blocked_daily_cap: '超过每日上限', expired_out_of_session: '超时未提交',
    }
    for (const [code, label] of Object.entries(before)) expect(lifecycleLabel('mirror_order', code)).toBe(label)
  })

  it('gives every mirror status a Chinese name instead of the raw code', () => {
    for (const [code, label] of Object.entries(LIFECYCLE_LABELS.mirror_order)) {
      expect(label).not.toBe(code)
      expect(label).toMatch(/[\u4e00-\u9fff]/)
    }
    for (const code of ['submitting', 'submission_unknown', 'alpaca_canceled', 'alpaca_expired', 'alpaca_rejected',
      'skipped_internal_exit', 'superseded_by_final_close']) {
      expect(lifecycleLabel('mirror_order', code)).not.toBe(code)
    }
  })

  it('returns an unknown code unchanged', () => {
    expect(lifecycleLabel('mirror_order', 'brand_new_status')).toBe('brand_new_status')
  })
})
