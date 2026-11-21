/** 分区阻塞根因 API：按目标表+日期追溯未齐路径、聚合根因、编排去重恢复计划。 */
import { request } from './client'

export type SourceBlock = {
  source_table: string
  partition_field: string
  partition_value: string
  status: string
  ready: boolean
  blocking: boolean
  reason: string
  registry_id?: number | null
}

export type PathCheck = {
  path_id: string
  target_table: string
  target_partition: string
  path_chain: string[]
  ready: boolean
  sources: SourceBlock[]
  blocking_sources: SourceBlock[]
}

export type RootCause = {
  root_key: string
  source_table: string
  partition_field: string
  partition_value: string
  status: string
  reason: string
  blocked_path_count: number
  blocked_path_ids: string[]
  affected_targets: string[]
  blocked_paths: PathCheck[]
  pending_sources_by_path: Record<string, SourceBlock[]>
}

export type BlockingAnalysis = {
  target_table: string
  target_partition: string
  target_partition_status: string
  path_count: number
  blocked_path_count: number
  all_paths_ready: boolean
  paths: PathCheck[]
  root_causes: RootCause[]
  analyzed_at: string
}

export type RecoveryWriteItem = {
  源表: string
  分区字段: string
  分区值: string
  来源根因: string
  关联路径: string[]
  写入来源: string
}

export type RecoveryPlan = {
  plan_id: number
  plan_no: string
  write_item_count: number
  dedup_skipped_count: number
  write_items: RecoveryWriteItem[]
  dedup_skipped: { source_table: string; partition_value: string; reason: string; dedup_key: string }[]
  root_causes: RootCause[]
  created_at: string
}

export type RecheckResult = {
  target_table: string
  target_partition: string
  all_paths_ready: boolean
  newly_unlocked_paths: string[]
  unlocked_paths: {
    path_id: string
    path_chain: string[]
    cleared_root_causes: string[]
    newly_unlocked: boolean
    detail: PathCheck
  }[]
  still_blocked_paths: {
    path_id: string
    path_chain: string[]
    cleared_root_causes: string[]
    remaining_blockers: string[]
    detail: PathCheck
  }[]
  rechecked_at: string
}

export type RunnableTarget = {
  target_table: string
  target_partition: string
  path_count: number
  ready_inputs: string[]
}

async function asJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}))
    throw new Error(detail.detail ? String(detail.detail) : `接口返回 ${response.status}`)
  }
  return (await response.json()) as T
}

export async function analyzeBlocking(
  targetTable: string,
  targetPartition: string,
  onlyBlocked = false,
): Promise<BlockingAnalysis> {
  const params = new URLSearchParams({
    target_table: targetTable,
    target_partition: targetPartition,
    only_blocked: String(onlyBlocked),
  })
  const response = await request(`/api/blocking/analyze?${params}`)
  return asJson<BlockingAnalysis>(response)
}

export async function createRecoveryPlan(body: {
  target_table: string
  target_partition: string
  reason?: string
  requested_ranges?: { source_table: string; start_partition: string; end_partition: string }[]
}): Promise<{ ok: boolean; analysis_summary: Record<string, number>; recovery_plan: RecoveryPlan }> {
  const response = await request('/api/blocking/recovery/plan', {
    method: 'POST',
    body: JSON.stringify(body),
  })
  return asJson(response)
}

export async function markPartitionReady(body: {
  table: string
  partition_value: string
  producer?: string
}): Promise<{ ok: boolean; partition: Record<string, unknown> }> {
  const response = await request('/api/blocking/partitions/ready', {
    method: 'POST',
    body: JSON.stringify(body),
  })
  return asJson(response)
}

export async function recheckBlocking(
  targetTable: string,
  targetPartition: string,
): Promise<RecheckResult> {
  const response = await request('/api/blocking/recheck', {
    method: 'POST',
    body: JSON.stringify({ target_table: targetTable, target_partition: targetPartition }),
  })
  return asJson<RecheckResult>(response)
}

export async function listRunnableTargets(
  targetPartition: string,
): Promise<{ total: number; items: RunnableTarget[] }> {
  const params = new URLSearchParams({ target_partition: targetPartition })
  const response = await request(`/api/blocking/runnable?${params}`)
  return asJson(response)
}

export async function listRecoveryPlans(
  targetTable?: string,
  targetPartition?: string,
): Promise<{
  total: number
  plans: {
    plan_id: number
    plan_no: string
    target_table: string
    target_partition: string
    status: string
    write_items: RecoveryWriteItem[]
    dedup_skipped: { source_table: string; partition_value: string; reason: string; dedup_key: string }[]
    root_causes: string[]
    reason: string
    created_at: string
  }[]
  write_key_owner: Record<string, string>
}> {
  const params = new URLSearchParams()
  if (targetTable) params.set('target_table', targetTable)
  if (targetPartition) params.set('target_partition', targetPartition)
  const response = await request(`/api/blocking/recovery/plans?${params}`)
  return asJson(response)
}
