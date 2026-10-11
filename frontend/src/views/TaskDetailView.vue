<script setup lang="ts">
/**
 * 任务详情 · 概览页签（T3.9 / §6.2 页面 4 概览 / 页面 7）：
 * 状态机当前态、位点信息、延迟曲线（ECharts + WS task.lag 增量追加）、事件统计、
 * 启停操作（start/pause/resume/stop）、DDL 决策弹窗、异常待处理/需重新初始化
 * 提示与一键全量重刷入口。
 * 样式一律引用 tokens.css 设计系统变量，不硬编码色值。
 */
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  NAlert, NButton, NCard, NGrid, NGridItem, NProgress, NSpace, NTabs, NTabPane, NTag,
  useDialog, useMessage,
} from 'naive-ui'
import * as echarts from 'echarts'
import {
  backupApi, dsApi, settingsApi, taskApi, type AlertLog, type CdcStats, type Datasource,
  type DdlPendingItem, type LagPoint, type RunProgressSnapshot, type SyncTask,
} from '../api'
import { errMsg } from '../api/client'
import StatusPill from '../components/StatusPill.vue'
import DdlDecisionModal from '../components/DdlDecisionModal.vue'
import ScheduleTab from '../components/ScheduleTab.vue'
import PolicyTab from '../components/PolicyTab.vue'
import MappingTab from '../components/MappingTab.vue'
import { useWsEvent } from '../composables/wsBus'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const dialog = useDialog()
const taskId = Number(route.params.id)

const task = ref<SyncTask | null>(null)
const cdc = ref<CdcStats | null>(null)
const lagCurrent = ref<number | null>(null)
const ddlItems = ref<DdlPendingItem[]>([])
const ddlShow = ref(false)
const busy = ref(false)
const progress = ref<RunProgressSnapshot | null>(null)
const dsList = ref<Datasource[]>([])
const lagHistoryError = ref(false)
const unresolvedAlerts = ref<AlertLog[]>([])

async function loadAlerts() {
  try {
    const r = await settingsApi.alerts(200)
    unresolvedAlerts.value = r.items.filter((a) => a.task_id === taskId && !a.resolved_at)
  } catch { /* 非阻塞：横幅按有告警的保守态展示 */ }
}

async function load() {
  try {
    task.value = await taskApi.get(taskId)
    cdc.value = (task.value as unknown as { cdc_stats?: CdcStats }).cdc_stats ?? null
    lagCurrent.value = (task.value as unknown as { lag_seconds?: number | null }).lag_seconds
      ?? cdc.value?.lag_seconds ?? null
    ddlItems.value = (task.value as unknown as { ddl_pending?: DdlPendingItem[] }).ddl_pending ?? []
    progress.value = (task.value as unknown as { progress?: RunProgressSnapshot | null }).progress ?? null
    renderChart()
  } catch (e) {
    message.error(errMsg(e))
  }
}
onMounted(async () => {
  void dsApi.list().then((r) => { dsList.value = r }).catch(() => { /* 名称降级为 #id */ })
  void loadAlerts()
  await load()
  await initChart()
  await loadLagHistory()
})

// ---- WS 实时注入（§5.3：task.lag / task.stats / task.state_changed / ddl.pending） ----
const lagSeries = ref<LagPoint[]>([])
useWsEvent(['task.lag', 'task.stats'], (env) => {
  const p = env.payload as { task_id?: number; lag_seconds?: number | null;
    timestamp?: number; stats?: CdcStats['counters'] } | undefined
  if (!p || p.task_id !== taskId) return
  if (env.type === 'task.lag') {
    lagCurrent.value = p.lag_seconds ?? null
    if (p.lag_seconds != null && p.timestamp) {
      pushLagPoint({ ts: p.timestamp, lag_seconds: p.lag_seconds })
    }
  }
  cdc.value = { ...(cdc.value ?? { running: true }), counters: p.stats, lag_seconds: p.lag_seconds ?? null }
})
// B1：运行进度实时注入（每秒推送；run 结束后 tracker 移除 → 快照随 load() 置空）
useWsEvent(['task.progress'], (env) => {
  const p = env.payload as unknown as RunProgressSnapshot | undefined
  if (!p || p.task_id !== taskId) return
  progress.value = p
})
useWsEvent(['task.state_changed', 'run.finished'], (env) => {
  // F3：重连快照载荷结构与事件不同（无 task_id），显式触发重拉对齐页面状态
  if (env.type === 'ws.snapshot') { void load(); return }
  const p = env.payload as { task_id?: number } | undefined
  if (!p || p.task_id === taskId) load()
})
useWsEvent(['ddl.pending'], (env) => {
  const p = env.payload as { task_id?: number } | undefined
  if (!p || p.task_id !== taskId) return
  load()
  ddlShow.value = true
})
// 告警产生/解除 → 异常横幅的「告警已处理」状态实时刷新
useWsEvent(['alert.fired', 'alert.resolved'], (env) => {
  const p = env.payload as { task_id?: number | null } | undefined
  if (env.type === 'alert.resolved' || p?.task_id === taskId) void loadAlerts()
})

// ---- 延迟历史（1 分钟粒度） + ECharts 增量追加 ----
let chart: echarts.ECharts | null = null
const chartEl = ref<HTMLElement | null>(null)
const MAX_POINTS = 1440

function cssVar(name: string, fallback: string): string {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return v || fallback
}

async function loadLagHistory() {
  try {
    const r = await taskApi.metricsLag(taskId, 1440)
    lagSeries.value = r.points.slice(-MAX_POINTS)
    lagHistoryError.value = false
    renderChart()
  } catch {
    // 指标缺失不阻塞页面，但空态/失败可见化（B4）
    lagHistoryError.value = true
  }
}

function pushLagPoint(pt: LagPoint) {
  const arr = lagSeries.value
  // 与 1 分钟粒度历史合并：同分钟覆盖，否则追加
  const bucket = Math.floor(pt.ts / 60000)
  const last = arr[arr.length - 1]
  if (last && Math.floor(last.ts / 60000) === bucket) arr[arr.length - 1] = pt
  else arr.push(pt)
  if (arr.length > MAX_POINTS) arr.splice(0, arr.length - MAX_POINTS)
  renderChart()
}

async function initChart() {
  await nextTick()
  if (!chartEl.value || chart) return
  chart = echarts.init(chartEl.value)
  renderChart()
}

function renderChart() {
  if (!chart) return
  const data = lagSeries.value.map((p) => [p.ts, Number(p.lag_seconds.toFixed(2))])
  chart.setOption({
    backgroundColor: 'transparent',
    grid: { left: 48, right: 16, top: 16, bottom: 28 },
    tooltip: { trigger: 'axis',
      valueFormatter: (v: unknown) => `${v as number}s` },
    xAxis: { type: 'time',
      axisLine: { lineStyle: { color: cssVar('--Semantic-Color-border', '#262629') } },
      axisLabel: { color: cssVar('--Semantic-Color-text-tertiary', '#75757A'), fontSize: 11 },
      splitLine: { show: false } },
    yAxis: { type: 'value', name: '延迟 (s)',
      nameTextStyle: { color: cssVar('--Semantic-Color-text-tertiary', '#75757A') },
      axisLabel: { color: cssVar('--Semantic-Color-text-tertiary', '#75757A'), fontSize: 11 },
      splitLine: { lineStyle: { color: cssVar('--Semantic-Color-border', '#262629') } } },
    series: [{
      type: 'line', showSymbol: false, smooth: true, data,
      lineStyle: { color: cssVar('--Semantic-Color-info', '#21D4ED'), width: 1.5 },
      // F6：面积色由 info 色 token 派生（修复前硬编码 rgba(33,212,237,.10) 绕过 tokens）
      areaStyle: { color: `color-mix(in srgb, ${cssVar('--Semantic-Color-info', '#21D4ED')} 10%, transparent)` },
      markLine: thresholdLine(),
    }],
  }, { notMerge: false })
}

function thresholdLine() {
  const th = task.value?.lag_alert_sec ?? 60
  return { silent: true, symbol: 'none',
    lineStyle: { color: cssVar('--Semantic-Color-warning', '#F59E0A'), type: 'dashed', width: 1 },
    label: { formatter: `阈值 ${th}s`, color: cssVar('--Semantic-Color-text-tertiary', '#75757A') },
    data: [{ yAxis: th }] }
}

const _onResize = () => chart?.resize()
onMounted(() => window.addEventListener('resize', _onResize))
onUnmounted(() => {
  window.removeEventListener('resize', _onResize)
  chart?.dispose()
  chart = null
})

// ---- 操作 ----
const isCdcMode = computed(() => task.value?.mode === 'init_plus_cdc')
const hasPosition = computed(() => !!task.value?.position)
const state = computed(() => task.value?.state ?? '')
const showCdcActions = computed(() => isCdcMode.value && hasPosition.value)

// 按钮显隐与 orchestrator 状态机逐格对齐（杜绝必 409 的按钮）：
// - backup_only 不允许 FULL_RESYNCING（原「全量重刷」点击必 409）
// - init_plus_cdc 的 error 态不允许 FULL_RESYNCING（原条件未排除）
const RUN_BASED = ['initializing', 'full_resyncing', 'backing_up'] as const
const isRunBased = computed(() => (RUN_BASED as readonly string[]).includes(state.value))
const showResync = computed(() => {
  if (task.value?.mode === 'init_plus_cdc') return state.value === 'idle' || state.value === 'needs_reinit'
  if (task.value?.mode === 'scheduled_full') return state.value === 'idle'
  return false
})
const showBackupAction = computed(() =>
  task.value?.mode === 'backup_only' && state.value === 'idle')
const showCancel = computed(() => isRunBased.value)   // CDC 运行态无 tracker run，cancel 必 404
// 异常态人工恢复入口：定时全量/仅备份 error → idle（增量任务有位点时用「启动增量」直连恢复）
const showRecover = computed(() =>
  state.value === 'error' && (task.value?.mode !== 'init_plus_cdc' || !hasPosition.value))

async function wrap(fn: () => Promise<unknown>, ok: string): Promise<boolean> {
  busy.value = true
  try {
    await fn()
    message.success(ok)
    await load()
    return true
  } catch (e) {
    message.error(errMsg(e))
    return false
  } finally {
    busy.value = false
  }
}

const doStart = () => wrap(() => taskApi.start(taskId), '增量已启动（从持久化同步位点续传）')
const doRecover = () =>
  wrap(() => taskApi.recover(taskId), '任务已恢复为空闲：可重新发起全量/备份，或等待下次调度')
const doPause = () => wrap(() => taskApi.pause(taskId), '增量已暂停（队列排空后同步位点落盘）')
const doResume = () => wrap(() => taskApi.resume(taskId), '增量已恢复')
const doStop = () => dialog.warning({
  title: '停止增量',
  content: '停止前会排空缓存队列并最终保存同步位点，之后可随时再次启动续传。确认停止？',
  positiveText: '停止增量', negativeText: '取消',
  onPositiveClick: () => wrap(() => taskApi.stop(taskId), '增量已停止'),
})

// A2：draft 态发起初始化（此前必须回任务列表操作）
function doInit() {
  dialog.warning({
    title: `发起初始化 · ${task.value?.name ?? ''}`,
    content: '将初始化目标库（自动建库 + 迁移结构 + 拷贝数据）。目标库非空时引擎会要求「清空重建」确认。确认发起？',
    positiveText: '发起初始化', negativeText: '取消',
    onPositiveClick: async () => {
      if (await wrap(() => taskApi.init(taskId, false, null), '初始化已发起')) {
        router.push({ name: 'task-progress', params: { id: taskId } })
      }
    },
  })
}

// A1：backup_only 发起备份（口径对齐调度器 backup_export：目标数据源 + mapping 目标库）
function doBackup() {
  const t = task.value
  if (!t) return
  const tables = t.mapping?.scope === 'selected' ? (t.mapping?.tables ?? null) : null
  dialog.warning({
    title: `发起备份 · ${t.name}`,
    content: `将对「${dsName(t.target_id)} / ${t.mapping?.target_database || '—'}」执行备份导出`
      + `（${tables ? `选中 ${tables.length} 张表` : '整库'}，gzip + SHA-256）。大库导出耗时较长，确认执行？`,
    positiveText: '开始导出', negativeText: '取消',
    onPositiveClick: () => wrap(async () => {
      await backupApi.create({
        datasource_id: t.target_id,
        schema: t.mapping?.target_database || '',
        tables, structure_only: false,
      })
    }, '备份导出完成，可前往「备份管理」下载'),
  })
}

// A4：运行中取消（进度卡断点保留；CDC 增量运行态由暂停/停止承担）
function doCancelRun() {
  dialog.warning({
    title: '取消运行',
    content: '将请求取消当前运行（已落库的断点进度保留）。确认取消？',
    positiveText: '取消运行', negativeText: '继续等待',
    onPositiveClick: () => wrap(() => taskApi.cancel(taskId), '已请求取消，运行将在安全点终止'),
  })
}

function doResync() {
  dialog.error({
    title: '全量重刷',
    content: '重刷将以源库当前数据覆盖目标库（REPLACE 语义），重刷前自动备份目标库涉及表。确认执行？',
    positiveText: '发起全量重刷', negativeText: '取消',
    onPositiveClick: async () => {
      if (await wrap(() => taskApi.fullResync(taskId, null),
        '全量重刷已发起，涉及表增量已转入缓存队列')) {
        router.push({ name: 'task-progress', params: { id: taskId } })
      }
    },
  })
}

// ---- B1：运行进度卡 ----
const PHASE_LABEL: Record<string, string> = {
  prepare: '准备', position: '获取一致位点', structure: '结构迁移', copy: '数据拷贝',
  finalize: '收尾校验', restore: '恢复回放', done: '完成',
}
const showProgressCard = computed(() => {
  const p = progress.value
  if (!p) return false
  return isRunBased.value || p.run_type === 'backup'   // backup 分支兜底调度备份（任务态停留 idle）
})
const progressPct = computed(() => {
  const p = progress.value
  if (!p) return 0
  if (p.rows_total > 0) return Math.min(100, Math.round((p.rows_done / p.rows_total) * 100))
  if (p.tables_total > 0) return Math.min(100, Math.round((p.tables_done / p.tables_total) * 100))
  return 0
})
const failedTableCount = computed(() =>
  (progress.value?.tables ?? []).filter((t) => t.state === 'failed').length)

// ---- B2：源库 → 目标库 ----
const dsName = (id: number | null | undefined) =>
  dsList.value.find((d) => d.id === id)?.name ?? `#${id ?? '—'}`
const srcDb = computed(() => task.value?.mapping?.source_database || '—')
const tgtDb = computed(() => task.value?.mapping?.target_database || '—')

// ---- B3：统计卡片按模式区分 ----
const statCols = computed(() => isCdcMode.value ? 4 : 2)

// B4：延迟卡 v-if="isCdcMode"——task 异步加载后卡片才渲染，需补 init（否则 ECharts 不挂载）
watch(isCdcMode, async (v) => {
  if (v) {
    await initChart()
    renderChart()
  }
})

// ---- 展示辅助 ----
const lagLevel = computed(() => {
  const v = lagCurrent.value
  if (v == null) return 'unknown'
  const th = task.value?.lag_alert_sec ?? 60
  if (v > th) return 'error'
  if (v > th * 0.5) return 'warn'
  return 'ok'
})
const lagColor = computed(() => ({
  ok: 'var(--Semantic-Color-success)',
  warn: 'var(--Semantic-Color-warning)',
  error: 'var(--Semantic-Color-error)',
  unknown: 'var(--Semantic-Color-text-tertiary)',
}[lagLevel.value]))

function fmtLag(v: number | null | undefined): string {
  if (v == null) return '—'
  if (v < 1) return `${Math.round(v * 1000)} ms`
  if (v < 60) return `${v.toFixed(1)} s`
  return `${Math.floor(v / 60)} 分 ${Math.round(v % 60)} 秒`
}
const fmt = (n: number | null | undefined) => (n ?? 0).toLocaleString()
const posFile = computed(() => {
  const p = task.value?.position
  if (!p) return '—'
  return p.binlog_file ? `${p.binlog_file}:${p.binlog_pos ?? 4}` : '—'
})
const queuePct = computed(() => {
  const c = cdc.value
  if (!c?.queue_capacity) return 0
  return Math.min(100, Math.round(((c.queue_size ?? 0) / c.queue_capacity) * 100))
})
</script>

<template>
  <div style="padding: 24px; max-width: 1200px">
    <!-- 头部：名称 + 状态 + 启停操作 -->
    <div style="display: flex; align-items: center; margin-bottom: 16px; gap: 12px">
      <n-button quaternary size="small" @click="router.push({ name: 'tasks' })">← 返回</n-button>
      <h2 style="margin: 0; font-size: var(--Font-Size-font-h2)">{{ task?.name || '任务详情' }}</h2>
      <StatusPill v-if="task" :state="task.state" />
      <n-tag size="small" :bordered="false">{{ task?.mode_label }}</n-tag>
      <div style="flex: 1" />
      <n-space size="small">
        <n-button v-if="state === 'draft' && task?.mode === 'init_plus_cdc'" size="small"
          type="primary" :loading="busy" :disabled="busy" @click="doInit">发起初始化</n-button>
        <template v-if="showCdcActions">
          <n-button v-if="state === 'idle' || state === 'error'" size="small" type="primary"
            :loading="busy" @click="doStart">启动增量</n-button>
          <n-button v-if="state === 'cdc_running'" size="small" secondary
            :loading="busy" @click="doPause">暂停</n-button>
          <n-button v-if="state === 'paused'" size="small" secondary type="primary"
            :loading="busy" @click="doResume">恢复</n-button>
          <n-button v-if="state === 'cdc_running' || state === 'paused'" size="small"
            secondary type="error" :loading="busy" @click="doStop">停止</n-button>
        </template>
        <n-button v-if="showResync" size="small" secondary type="error"
          :loading="busy" :disabled="busy" @click="doResync">
          {{ state === 'needs_reinit' ? '一键全量重刷' : '全量重刷' }}
        </n-button>
        <n-button v-if="showBackupAction" size="small" type="primary" secondary
          :loading="busy" :disabled="busy" @click="doBackup">发起备份</n-button>
        <n-button v-if="showCancel" size="small" secondary type="error"
          :loading="busy" :disabled="busy" @click="doCancelRun">取消运行</n-button>
        <n-button v-if="showRecover" size="small" type="primary"
          :loading="busy" :disabled="busy" @click="doRecover">人工恢复</n-button>
        <n-button size="small" quaternary
          @click="router.push({ name: 'task-progress', params: { id: taskId } })">执行进度</n-button>
      </n-space>
    </div>

    <!-- B2：源库 → 目标库（M6 批量任务靠任务名难以分辨库对应） -->
    <div style="margin: -6px 0 12px; font-size: var(--Font-Size-font-caption);
      color: var(--Semantic-Color-text-secondary)">
      源 <span class="mono">{{ dsName(task?.source_id) }} / {{ srcDb }}</span>
      &nbsp;→&nbsp; 目标 <span class="mono">{{ dsName(task?.target_id) }} / {{ tgtDb }}</span>
    </div>

    <!-- 异常与拦截提示 -->
    <n-alert v-if="state === 'needs_reinit'" type="error" style="margin-bottom: 12px">
      <b>同步位点失效</b>：重连时所需 binlog 已被源库清理，任务已自动停止并标记为「需重新初始化」。
      请在源库低峰期执行<span style="font-weight: 600">一键全量重刷</span>重建数据与同步位点。
    </n-alert>
    <n-alert v-if="state === 'error'" :type="unresolvedAlerts.length ? 'error' : 'warning'"
      :bordered="false" style="margin-bottom: 12px">
      <template v-if="unresolvedAlerts.length">
        任务异常待处理：请根据告警消息排除故障
        （{{ task?.mode === 'init_plus_cdc' ? '目标库可写性 / 源库连接 / 事件解析' : '目标库可写性 / 磁盘空间 / 连接配置' }}）。
        处理后{{ task?.mode === 'init_plus_cdc' && hasPosition
          ? '可点击「启动增量」人工恢复。'
          : '可点击右上角「人工恢复」将任务恢复为空闲，再重新发起或等待下次调度。' }}
        <div style="margin-top: 6px; font-size: var(--Font-Size-font-caption)">
          最新告警：<span class="mono">{{ unresolvedAlerts[0].message }}</span>
        </div>
      </template>
      <template v-else>
        任务处于异常态，但相关告警已全部标记处理。若故障已排除，请点击
        {{ task?.mode === 'init_plus_cdc' && hasPosition ? '「启动增量」恢复续传' : '右上角「人工恢复」将任务恢复为空闲' }}。
      </template>
    </n-alert>
    <n-alert v-if="ddlItems.length" type="warning" style="margin-bottom: 12px">
      <n-space align="center">
        <span>检测到 <b>{{ ddlItems.length }}</b> 条待决策 DDL，同步已暂停等待决策。</span>
        <n-button size="tiny" type="warning" @click="ddlShow = true">打开决策弹窗</n-button>
      </n-space>
    </n-alert>
    <n-alert v-if="cdc?.backpressure" type="warning" :bordered="false" style="margin-bottom: 12px">
      队列接近满载：已自动暂停拉取 binlog（防止内存溢出）；持续高位建议调高任务级限流或检查目标库写入速度。
    </n-alert>
    <n-alert v-if="cdc?.big_tx" type="warning" :bordered="false" style="margin-bottom: 12px">
      大事务流式提交中（超过 10 万行）。已知限制：流式提交拆散源事务边界（部分子变更先行可见），
      最终一致由幂等写保证。
    </n-alert>

    <n-tabs type="line" default-value="overview" :animated="false">
      <!-- ============ 概览 ============ -->
      <n-tab-pane name="overview" tab="概览">
        <!-- B1：运行进度卡（初始化/重刷/备份进行中；每秒 WS task.progress 刷新） -->
        <n-card v-if="showProgressCard"
          style="border-radius: var(--Radius-radius-lg); margin-bottom: 12px">
          <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 8px">
            <span class="stat-label" style="margin: 0">
              运行进度 · {{ PHASE_LABEL[progress?.phase ?? ''] || progress?.phase || '—' }}
            </span>
            <n-tag v-if="progress?.paused" size="small" type="warning" :bordered="false">已暂停</n-tag>
            <span v-if="failedTableCount"
              style="color: var(--Semantic-Color-error); font-size: var(--Font-Size-font-caption)">
              失败表 {{ failedTableCount }}
            </span>
            <div style="flex: 1" />
            <n-button size="tiny" quaternary
              @click="router.push({ name: 'task-progress', params: { id: taskId } })">查看详情</n-button>
            <n-button size="tiny" secondary type="error" :loading="busy" :disabled="busy"
              @click="doCancelRun">取消</n-button>
          </div>
          <n-progress type="line" :percentage="progressPct" indicator-placement="inside"
            :height="10" :border-radius="6" processing />
          <div class="stat-sub">
            行 <span class="mono">{{ fmt(progress?.rows_done) }}/{{ fmt(progress?.rows_total) }}</span>
            · 表 <span class="mono">{{ progress?.tables_done ?? 0 }}/{{ progress?.tables_total ?? 0 }}</span>
            · <span class="mono">{{ fmt(progress?.speed_rows_per_sec) }}</span> 行/s
            <template v-if="progress?.eta_seconds != null"> · 剩余约
              <span class="mono">{{ progress.eta_seconds }}s</span></template>
          </div>
        </n-card>

        <n-grid :cols="statCols" :x-gap="12" :y-gap="12" style="margin-bottom: 12px">
          <n-grid-item v-if="isCdcMode">
            <n-card style="border-radius: var(--Radius-radius-lg); height: 100%">
            <div class="stat-label">当前延迟</div>
            <div class="stat-value mono" :style="{ color: lagColor }">{{ fmtLag(lagCurrent) }}</div>
            <div class="stat-sub">阈值 {{ task?.lag_alert_sec ?? 60 }}s · 超阈告警 / 回落 50% 解除</div>
            </n-card>
          </n-grid-item>
          <n-grid-item v-if="isCdcMode">
            <n-card style="border-radius: var(--Radius-radius-lg); height: 100%">
              <div class="stat-label">事件统计（累计）</div>
              <div class="stat-value" style="font-size: var(--Font-Size-font-h3)">
                <span style="color: var(--Semantic-Color-success)">增 {{ fmt(cdc?.counters?.inserted) }}</span>
                <span style="color: var(--Semantic-Color-info)"> · 改 {{ fmt(cdc?.counters?.updated) }}</span>
                <span style="color: var(--Semantic-Color-error)"> · 删 {{ fmt(cdc?.counters?.deleted) }}</span>
              </div>
              <div class="stat-sub">
                队列 <span class="mono">{{ fmt(cdc?.queue_size) }}/{{ fmt(cdc?.queue_capacity) }}</span>
                （{{ queuePct }}%）
                <template v-if="cdc?.caching"> · <span style="color: var(--Semantic-Color-warning)">重刷缓存中</span></template>
              </div>
            </n-card>
          </n-grid-item>
          <n-grid-item>
            <n-card style="border-radius: var(--Radius-radius-lg); height: 100%">
              <div class="stat-label">binlog 同步位点</div>
              <div class="stat-value mono" style="font-size: var(--Font-Size-font-mono-body); color: var(--Semantic-Color-info)">
                {{ posFile }}
              </div>
              <div class="stat-sub" v-if="!isCdcMode">该模式不产生增量位点（仅全量 / 备份链路）</div>
              <div class="stat-sub" v-else>server_id <span class="mono">{{ task?.position?.server_id_used ?? '—' }}</span>
                · 最近提交 <span class="mono">{{ task?.position?.last_commit_at ?? '—' }}</span></div>
            </n-card>
          </n-grid-item>
          <n-grid-item>
            <n-card style="border-radius: var(--Radius-radius-lg); height: 100%">
              <div class="stat-label">GTID 已执行集合</div>
              <div class="stat-value mono gtid" style="font-size: var(--Font-Size-font-mono-body); color: var(--Semantic-Color-info)">
                {{ task?.position?.gtid_executed || '（非 GTID 模式，按 binlog 文件与位置续传）' }}
              </div>
            </n-card>
          </n-grid-item>
        </n-grid>

        <n-card v-if="isCdcMode" title="延迟趋势（1 分钟粒度 · 实时追加）"
          style="border-radius: var(--Radius-radius-lg); margin-bottom: 12px">
          <div style="position: relative">
            <div ref="chartEl" style="width: 100%; height: 260px" />
            <div v-if="!lagSeries.length && !lagHistoryError" class="chart-empty">
              暂无数据（增量启动后产生延迟采样）
            </div>
            <div v-else-if="lagHistoryError" class="chart-empty"
              style="color: var(--Semantic-Color-warning)">
              延迟历史加载失败，实时数据仍正常推送
            </div>
          </div>
        </n-card>

        <n-card v-if="isCdcMode" style="border-radius: var(--Radius-radius-lg)">
          <div class="stat-label" style="margin-bottom: 8px">运行时状态</div>
          <n-space size="small">
            <n-tag size="small" :bordered="false"
              :type="cdc?.running ? 'info' : 'default'">
              {{ cdc?.running ? (cdc?.paused ? '已暂停（队列排空中）' : '写入线程运行中') : '未运行' }}
            </n-tag>
            <n-tag size="small" :bordered="false">范围外事件
              <span class="mono">{{ fmt(cdc?.out_of_scope_dropped) }}</span></n-tag>
            <n-tag size="small" :bordered="false">过滤丢弃
              <span class="mono">{{ fmt(cdc?.filter_dropped) }}</span></n-tag>
            <n-tag v-if="cdc?.replaying" size="small" :bordered="false" type="warning">
              缓存重放中（同步位置暂不推进）</n-tag>
          </n-space>
          <p class="stat-sub" style="margin: 8px 0 0">
            增量写入采用「先记进度、后写数据」的方式：每批写入目标库成功后才保存同步位置，
            中断重启后从上次进度继续；配合 INSERT 转 REPLACE 的写法，重复投递也不会产生重复数据。
          </p>
        </n-card>
      </n-tab-pane>

      <!-- M4 页签：调度 / 策略 / 表映射 -->
      <n-tab-pane name="schedule" tab="调度">
        <ScheduleTab :task-id="taskId" :task-state="state" />
      </n-tab-pane>
      <n-tab-pane name="policy" tab="策略">
        <PolicyTab :task-id="taskId" :task="task" @saved="load" />
      </n-tab-pane>
      <n-tab-pane name="mapping" tab="表映射">
        <MappingTab :task-id="taskId" :task="task" @saved="load" />
      </n-tab-pane>
      <n-tab-pane name="history" tab="运行历史">
        <n-button size="small" quaternary
          @click="router.push({ name: 'runs', query: { task_id: String(taskId) } })">
          查看该任务的执行历史 →
        </n-button>
      </n-tab-pane>
    </n-tabs>

    <DdlDecisionModal v-model:show="ddlShow" :task-id="taskId" :items="ddlItems"
      @decided="load" />
  </div>
</template>

<style scoped>
.stat-label {
  font-size: var(--Font-Size-font-micro);
  color: var(--Semantic-Color-text-tertiary);
  margin-bottom: 6px;
}
.stat-value {
  font-size: var(--Font-Size-font-h2);
  font-weight: 600;
  color: var(--Semantic-Color-text-primary);
}
.stat-sub {
  margin-top: 6px;
  font-size: var(--Font-Size-font-caption);
  color: var(--Semantic-Color-text-tertiary);
}
.gtid {
  word-break: break-all;
  max-height: 44px;
  overflow: hidden;
}
.mono {
  font-family: 'JetBrains Mono', Consolas, monospace;
}
.chart-empty {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--Semantic-Color-text-tertiary);
  font-size: var(--Font-Size-font-caption);
  background: var(--Semantic-Color-bg-elevated);
  border-radius: var(--Radius-radius-md);
  pointer-events: none;
}
</style>
