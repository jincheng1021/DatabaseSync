/** 认证与数据源 API 端点定义 */
import { http } from './client'

export interface PrecheckItem {
  key: string
  level: 'pass' | 'warn' | 'fail'
  message: string
  hint?: string | null
  affected?: string[]
}

export interface Datasource {
  id: number
  name: string
  db_type: string
  host: string
  port: number
  username: string
  ssl_mode: string
  default_schema: string | null
  charset: string
  connect_timeout: number
  note: string | null
  has_password: boolean
  precheck?: { overall: string; checked_at: string } | null
  referenced_tasks?: number
}

export const authApi = {
  status: () => http.get<never, { initialized: boolean; authenticated: boolean }>('/api/auth/status'),
  bootstrap: (username: string, password: string) => http.post('/api/auth/bootstrap', { username, password }),
  login: (username: string, password: string) => http.post('/api/auth/login', { username, password }),
  logout: () => http.post('/api/auth/logout'),
  me: () => http.get<never, { username: string }>('/api/auth/me'),
}

export const dsApi = {
  list: () => http.get<never, Datasource[]>('/api/datasources'),
  create: (body: Record<string, unknown>) => http.post<never, Datasource>('/api/datasources', body),
  update: (id: number, body: Record<string, unknown>) => http.put<never, Datasource>(`/api/datasources/${id}`, body),
  remove: (id: number) => http.delete(`/api/datasources/${id}`),
  clone: (id: number) => http.post<never, Datasource>(`/api/datasources/${id}/clone`),
  precheck: (id: number, role = 'both') =>
    http.post<never, { datasource_id: number; overall: string; items: PrecheckItem[] }>(
      `/api/datasources/${id}/precheck`, { role }),
  tables: (id: number, schema?: string) =>
    http.get<never, { tables: unknown[] }>(`/api/datasources/${id}/tables`, { params: { schema } }),
  schemas: (id: number) =>
    http.get<never, { schemas: string[] }>(`/api/datasources/${id}/schemas`),
}

export const settingsApi = {
  get: () => http.get<never, {
    global_defaults: Record<string, number>
    master_key: { fingerprint: string; path: string; hint: string }
    channels: { webhook: boolean; smtp: boolean }
    alert_channels?: {
      webhook_url: string | null
      smtp: Record<string, unknown> | null
      silence_minutes: number | null
    }
  }>('/api/settings'),
  updateAlertChannels: (body: Record<string, unknown>) =>
    http.put<never, { ok: boolean }>('/api/settings/alert-channels', body),
  testAlert: () =>
    http.post<never, { results: Array<{ channel: string; sent: boolean; reason?: string }> }>(
      '/api/settings/alert-test'),
  alerts: (limit = 50) =>
    http.get<never, { items: AlertLog[] }>('/api/alerts', { params: { limit } }),
  resolveAlert: (alertKey: string) =>
    http.post<never, { ok: boolean }>('/api/alerts/resolve', { alert_key: alertKey }),
}

export interface AlertLog {
  id: number
  alert_key: string
  level: string
  task_id: number | null
  message: string
  fired_at: string
  resolved_at: string | null
}

// ================= M4：调度 / 校验 / 表映射元数据 =================

export type ScheduleAction =
  | 'cdc_start' | 'cdc_stop' | 'full_resync' | 'verify' | 'backup_export' | 'orphan_cleanup'

export interface Schedule {
  id: number
  task_id: number
  name: string
  action: ScheduleAction
  cron: string
  timezone: string
  enabled: boolean
  action_params: Record<string, unknown>
  overlap_policy: 'skip' | 'queue'
  misfire_policy: 'skip' | 'run_once'
  next_fire_at: string | null
}

export const scheduleApi = {
  listByTask: (taskId: number) =>
    http.get<never, { schedules: Schedule[] }>(`/api/tasks/${taskId}/schedules`),
  create: (taskId: number, body: Record<string, unknown>) =>
    http.post<never, Schedule>(`/api/tasks/${taskId}/schedules`, body),
  update: (sid: number, body: Record<string, unknown>) =>
    http.put<never, Schedule>(`/api/schedules/${sid}`, body),
  remove: (sid: number) => http.delete(`/api/schedules/${sid}`),
  trigger: (sid: number) =>
    http.post<never, { status: string; schedule_id: number; task_id: number; action: string }>(
      `/api/schedules/${sid}/trigger`),
  preview: (cron: string, timezone: string, count = 5) =>
    http.get<never, { cron: string; timezone: string; next: string[] }>(
      '/api/schedules/preview', { params: { cron, timezone, count } }),
}

export const ACTION_LABELS: Record<ScheduleAction, string> = {
  cdc_start: '启动增量',
  cdc_stop: '停止增量',
  full_resync: '全量重刷',
  verify: '数据校验',
  backup_export: '备份导出',
  orphan_cleanup: '孤儿行清理',
}

export interface VerifyDiffColumn { source: unknown; target: unknown }

export interface VerifyDiff {
  pk: (string | number)[]
  type: 'mismatch' | 'target_missing' | 'target_extra'
  columns?: Record<string, VerifyDiffColumn>
  source?: Record<string, unknown>
  target?: Record<string, unknown>
}

export interface VerifyTableResult {
  status: 'match' | 'mismatch'
  src_count?: number
  tgt_count?: number
  chunks?: number
  mismatch_chunks?: number
  diff_count?: number
  diffs?: VerifyDiff[]
  warning?: string
  filtered?: boolean
  src_crc?: string
  tgt_crc?: string
}

export interface VerifyResult {
  id: number
  task_id: number
  run_id: number | null
  method: 'row_count' | 'checksum'
  state: 'running' | 'success' | 'mismatch' | 'failed'
  trigger: string
  tables_total: number
  tables_checked: number
  mismatch_tables: number
  summary: { tables_checked?: number; mismatch_tables?: number; mismatch_rows?: number; error?: string } | null
  started_at: string
  ended_at: string | null
}

export interface VerifyResultDetail extends VerifyResult {
  detail: { tables: Record<string, VerifyTableResult>; warnings?: string[] } | null
}

export const verifyApi = {
  start: (taskId: number, method: 'row_count' | 'checksum', tables?: string[] | null) =>
    http.post<never, { verify_id: number }>(`/api/tasks/${taskId}/verify`,
      { method, tables: tables ?? null }),
  list: (params: Record<string, unknown>) =>
    http.get<never, { items: VerifyResult[]; total: number; page: number; page_size: number }>(
      '/api/verify-results', { params }),
  get: (id: number, detail = false) =>
    http.get<never, VerifyResultDetail>(`/api/verify-results/${id}`, { params: { detail } }),
  repair: (id: number, body: Record<string, unknown> = {}) =>
    http.post<never, { verify_id: number; replaced: number; deleted: number; skipped: number }>(
      `/api/verify-results/${id}/repair`, body),
}

export interface TableMetaColumn {
  name: string
  nullable: boolean
  column_default: string | null
  column_key: string
  data_type: string
}

export interface TableMeta {
  name: string
  type: string | null
  rows_est: number
  primary_key: string[]
  has_pk: boolean
  columns: TableMetaColumn[]
}

export interface TableMetaResponse {
  schema: string | null
  tables: TableMeta[]
}

export interface RowFilterCond { col: string; op: string; value?: unknown; values?: unknown[] }

export const dsTableMeta = (dsId: number, schema?: string) =>
  http.get<never, TableMetaResponse>(`/api/datasources/${dsId}/table-meta`,
    { params: schema ? { schema } : {} })

// ================= M2：任务 / 备份 / 执行历史 =================

export type TaskMode = 'backup_only' | 'init_plus_cdc' | 'scheduled_full'

export interface SyncTask {
  id: number
  name: string
  source_id: number
  target_id: number
  mode: TaskMode
  mode_label: string
  mapping: {
    scope?: 'all' | 'selected'
    tables?: string[]
    rename?: Record<string, string>
    prefix?: string
    suffix?: string
    source_database?: string
    target_database?: string
    column_filter?: Record<string, string[]>
    row_filters?: Record<string, Record<string, unknown>>
    pk_mode?: Record<string, 'cdc' | 'degraded'>
  }
  migrate_triggers: boolean
  rate_limit_rows: number
  batch_size: number
  batch_interval_ms: number
  safety_net_backup: boolean
  orphan_cleanup: boolean
  ddl_policy?: string
  state: string
  subset_sync?: boolean
  mapping_warnings?: string[]
  lag_alert_sec: number
  progress?: RunProgressSnapshot | null
  position?: PositionInfo | null
  // ---- M3：增量概览 ----
  lag_seconds?: number | null
  cdc_stats?: CdcStats | null
  ddl_pending?: DdlPendingItem[]
}

export interface PositionInfo {
  gtid_executed: string | null
  binlog_file: string | null
  binlog_pos: number | null
  server_id_used: number | null
  last_commit_at: string | null
}

// ================= M3：增量概览（CDC 统计 / DDL 待决策 / 延迟曲线） =================

export interface CdcStats {
  running: boolean
  counters?: { inserted: number; updated: number; deleted: number }
  queue_size?: number
  queue_capacity?: number
  backpressure?: boolean
  big_tx?: boolean
  caching?: boolean
  replaying?: boolean
  cache_size?: number
  filter_dropped?: number
  out_of_scope_dropped?: number
  paused?: boolean
  lag_seconds?: number | null
}

export interface DdlPendingItem {
  id: number
  task_id: number
  ddl_text: string
  source_position: string | null
  affected_tables: string[]
  status: string
}

// ---- M6：多库批量创建 ----

export type TargetDbState = 'missing' | 'empty' | 'nonempty'

export interface ProbeTargetItem {
  target_database: string
  exists: boolean
  tables: number
  state: TargetDbState
}

export interface BatchCreatedItem {
  task_id: number
  name: string
  source_database: string
  target_database: string
  target_state: TargetDbState
}

export interface BatchFailedItem {
  source_database: string
  target_database: string
  reason: string
}

export interface BatchCreateResult {
  created: BatchCreatedItem[]
  failed: BatchFailedItem[]
  init_enqueued: number[]
  init_skipped: number[]
}

export interface LagPoint {
  ts: number
  lag_seconds: number
}

export interface TableProgressRow {
  name: string
  state: string
  rows_total: number | null
  rows_done: number
  error: string | null
}

export interface RunProgressSnapshot {
  run_id: number
  task_id: number | null
  run_type: string
  phase: string
  paused: boolean
  rows_total: number
  rows_done: number
  speed_rows_per_sec: number
  eta_seconds: number | null
  tables_total: number
  tables_done: number
  tables: TableProgressRow[]
  error: string | null
}

export interface RunHistory {
  id: number
  task_id: number | null
  backup_id: number | null
  run_type: string
  trigger: string
  state: string
  started_at: string
  ended_at: string | null
  rows_inserted: number
  tables_total: number
  tables_done: number
  error_summary: string | null
  report?: Record<string, unknown>
  live_progress?: RunProgressSnapshot | null
  table_details?: Array<Record<string, unknown>>
}

export const taskApi = {
  list: () => http.get<never, { tasks: SyncTask[] }>('/api/tasks'),
  get: (id: number) => http.get<never, SyncTask>(`/api/tasks/${id}`),
  create: (body: Record<string, unknown>) => http.post<never, SyncTask>('/api/tasks', body),
  // ---- M6：多库批量创建 / 批量初始化 / 目标库探测 ----
  batchCreate: (body: Record<string, unknown>) =>
    http.post<never, BatchCreateResult>('/api/tasks/batch', body),
  batchInit: (taskIds: number[], maxParallel = 2) =>
    http.post<never, { enqueued: number[]; skipped: number[] }>('/api/tasks/batch-init',
      { task_ids: taskIds, max_parallel: maxParallel }),
  batchInitStatus: () =>
    http.get<never, { queued: number[]; running: number; default_parallel: number }>(
      '/api/tasks/batch-init/status'),
  probeBatchTargets: (targetId: number, targetDatabases: string[]) =>
    http.post<never, { items: ProbeTargetItem[] }>('/api/tasks/batch/probe-targets',
      { target_id: targetId, target_databases: targetDatabases }),
  update: (id: number, body: Record<string, unknown>) => http.put<never, SyncTask>(`/api/tasks/${id}`, body),
  remove: (id: number) => http.delete(`/api/tasks/${id}`),
  init: (id: number, confirmRebuild: boolean, confirmName: string | null) =>
    http.post<never, { run_id: number }>(`/api/tasks/${id}/init`,
      { confirm_rebuild: confirmRebuild, confirm_name: confirmName }),
  fullResync: (id: number, tables: string[] | null, safetyNet = true, orphanCleanup = true) =>
    http.post<never, { run_id: number }>(`/api/tasks/${id}/full-resync`,
      { tables, safety_net: safetyNet, orphan_cleanup: orphanCleanup }),
  progress: (id: number) =>
    http.get<never, { progress: RunProgressSnapshot | null }>(`/api/tasks/${id}/progress`),
  position: (id: number) => http.get<never, PositionInfo>(`/api/tasks/${id}/position`),
  pause: (id: number) => http.post(`/api/tasks/${id}/pause`),
  resume: (id: number) => http.post(`/api/tasks/${id}/resume`),
  cancel: (id: number) => http.post(`/api/tasks/${id}/cancel`),
  // ---- M3：增量生命周期与概览 ----
  start: (id: number) => http.post<never, { run_id: number; started: boolean }>(`/api/tasks/${id}/start`),
  stop: (id: number) => http.post<never, { stopped: boolean }>(`/api/tasks/${id}/stop`),
  recover: (id: number) =>
    http.post<never, { task_id: number; state: string; recovered: boolean }>(`/api/tasks/${id}/recover`),
  ddlPending: (id: number) =>
    http.get<never, { items: DdlPendingItem[] }>(`/api/tasks/${id}/ddl-pending`),
  metricsLag: (id: number, rangeMinutes = 1440) =>
    http.get<never, { task_id: number; live: boolean; current_lag: number | null; points: LagPoint[] }>(
      `/api/metrics/lag`, { params: { task_id: id, range_minutes: rangeMinutes } }),
  ddlDecision: (id: number, decision: 'execute' | 'skip', remember: boolean) =>
    http.post<never, { decision: string; path: string; applied?: number; redo_run_id?: number }>(
      `/api/tasks/${id}/ddl-decision`, { decision, remember }),
  // ---- M4：孤儿行清理 / 映射高级特性 ----
  orphanCleanup: (id: number, dryRun = false) =>
    http.post<never, { run_id: number; dry_run: boolean }>(`/api/tasks/${id}/orphan-cleanup`,
      dryRun ? { dry_run: true } : undefined),
}

export interface BackupFile {
  id: number
  task_id: number | null
  datasource_id: number
  file_name: string
  file_path: string
  size_bytes: number
  sha256: string
  scope: { schema?: string; tables?: string[]; structure_only?: boolean; uncompressed_bytes?: number; source_version?: string }
  rows_baseline: Record<string, { rows: number; auto_increment: number | null }> | null
  created_by: string
  created_at: string
  retention_tag: string
}

export interface RestorePrecheck {
  items: Array<{ key: string; level: 'pass' | 'warn' | 'fail'; message: string; hint?: string | null }>
  affected_tasks: Array<{ task_id: number; name: string; state: string; target_db: string | null }>
  non_empty_tables: string[]
  rows_baseline: Record<string, { rows: number; auto_increment: number | null }> | null
  scope: Record<string, unknown>
}

export const backupApi = {
  list: () => http.get<never, { backups: BackupFile[] }>('/api/backups'),
  create: (body: Record<string, unknown>) =>
    http.post<never, { backup_ids: number[]; backup: BackupFile | null }>('/api/backups', body),
  get: (id: number) => http.get<never, BackupFile>(`/api/backups/${id}`),
  header: (id: number) =>
    http.get<never, { file_name: string; sha256: string; header: string; replay_notes: string[] }>(
      `/api/backups/${id}/file-header`),
  remove: (id: number) => http.delete(`/api/backups/${id}`),
  restorePrecheck: (id: number, targetDatasourceId: number, targetDb: string) =>
    http.get<never, RestorePrecheck>(`/api/backups/${id}/restore-precheck`,
      { params: { target_datasource_id: targetDatasourceId, target_db: targetDb } }),
  restore: (id: number, body: Record<string, unknown>) =>
    http.post<never, { run_id: number }>(`/api/backups/${id}/restore`, body),
}

export const runApi = {
  list: (params: Record<string, unknown>) =>
    http.get<never, { runs: RunHistory[]; total: number; page: number; page_size: number }>(
      '/api/runs', { params }),
  get: (id: number) => http.get<never, RunHistory>(`/api/runs/${id}`),
  pause: (id: number) => http.post(`/api/runs/${id}/pause`),
  resume: (id: number) => http.post(`/api/runs/${id}/resume`),
  cancel: (id: number) => http.post(`/api/runs/${id}/cancel`),
}

// ================= 仪表盘（§6.2 页面 1） =================

export interface DashboardSummary {
  task_counts: { total: number; running: number; error: number; paused: number; needs_reinit: number }
  today_events: { rows_inserted: number; rows_updated: number; rows_deleted: number; total: number }
  unresolved_alerts: number
  generated_at: string
}

export interface LagBatchSeries {
  task_id: number
  live: boolean
  current_lag: number | null
  points: LagPoint[]
}

export const dashboardApi = {
  summary: () => http.get<never, DashboardSummary>('/api/dashboard/summary'),
}

/** 多任务延迟历史批量（仪表盘聚合曲线，最多 8 个任务） */
export const metricsLagBatch = (taskIds: number[], rangeMinutes = 360) =>
  http.get<never, { series: LagBatchSeries[]; requested: number; returned: number }>(
    '/api/metrics/lag-batch',
    { params: { task_ids: taskIds.join(','), range_minutes: rangeMinutes } })
