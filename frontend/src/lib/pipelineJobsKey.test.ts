// 同步任务列表的查询键必须带 limit: 侧栏 (limit=1, 15s 轮询) 与数据页同步历史 (limit=15) 曾共用
// ['pipeline-jobs'] 一个缓存, 数据页拿到的永远是侧栏那 1 条 (2026-10-07 生产实测: 同步历史只剩 1 行)。
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { QueryClient } from '@tanstack/react-query'
import { expect, it } from 'vitest'
import { QK } from './queryKeys'

it('pipeline job lists with different limits use different cache entries', () => {
  expect(QK.pipelineJobsList(1)).not.toEqual(QK.pipelineJobsList(15))
})

it('invalidating QK.pipelineJobs still refreshes every list (prefix match)', async () => {
  const qc = new QueryClient()
  qc.setQueryData(QK.pipelineJobsList(1), { jobs: [] })
  qc.setQueryData(QK.pipelineJobsList(15), { jobs: [] })
  await qc.invalidateQueries({ queryKey: QK.pipelineJobs })
  expect(qc.getQueryState(QK.pipelineJobsList(1))!.isInvalidated).toBe(true)
  expect(qc.getQueryState(QK.pipelineJobsList(15))!.isInvalidated).toBe(true)
})

it('sidebar and data page query their own limit', () => {
  const layout = readFileSync(resolve(__dirname, '../components/Layout.tsx'), 'utf-8')
  const data = readFileSync(resolve(__dirname, '../pages/Data.tsx'), 'utf-8')
  expect(layout).toMatch(/queryKey: QK\.pipelineJobsList\(1\),\s*\n\s*queryFn: \(\) => api\.pipelineJobs\(1\)/)
  expect(data).toMatch(/queryKey: QK\.pipelineJobsList\(15\),\s*\n\s*queryFn: \(\) => api\.pipelineJobs\(15\)/)
})
