<script setup lang="ts">
/** 执行历史页（§6.2 页面 10）：全局 run 表格 + 筛选分页 + 详情（报告 / 表级明细） */
import { h, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NButton, NCard, NDataTable, NModal, NSelect, NTag, useMessage } from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import { runApi, type RunHistory } from '../api'
import { errMsg } from '../api/client'
import StatusPill from '../components/StatusPill.vue'
import { useWsEvent } from '../composables/wsBus'

const route = useRoute()
const router = useRouter()
const message = useMessage()

const runs = ref<RunHistory[]>([])
const total = ref(0)
const page = ref(Number(route.query.page) > 0 ? Number(route.query.page) : 1)
const pageSize = ref(20)
const runType = ref<string | null>((route.query.run_type as string) || null)
const loading = ref(false)

const detailShow = ref(false)
const detail = ref<RunHistory | null>(null)

const RUN_TYPES = [
  { label: '全部类型', value: '' },
  { label: '初始化', value: 'init' },
  { label: '全量重刷', value: 'full_resync' },
  { label: '备份', value: 'backup' },
  { label: '恢复', value: 'restore' },
  { label: '表重做', value: 'table_redo' },
  { label: '孤儿清理', value: 'orphan_cleanup' },
  { label: '增量会话', value: 'cdc_session' },
  { label: '定时重刷', value: 'schedule_full_resync' },
  { label: '定时备份', value: 'schedule_backup_export' },
  { label: '定时·启增量', value: 'schedule_cdc_start' },
  { label: '定时·停增量', value: 'schedule_cdc_stop' },
  { label: '定时校验', value: 'schedule_verify' },
  { label: '定时孤儿清理', value: 'schedule_orphan_cleanup' },
  { label: '状态迁移', value: 'state_change' },
]

const TYPE_LABEL: Record<string, string> = {
  init: '初始化', full_resync: '重刷', backup: '备份', restore: '恢复',
  table_redo: '表重做', cdc_session: '增量会话',
  orphan_cleanup: '孤儿清理', state_change: '状态迁移',
  schedule_cdc_start: '定时·启增量', schedule_cdc_stop: '定时·停增量',
  schedule_full_resync: '定时重刷', schedule_verify: '定时校验',
  schedule_backup_export: '定时备份', schedule_orphan_cleanup: '定时孤儿清理',
}

// 校验结果走 verify_result 表（不落 run_history），run_type='verify' 查询恒空；
// 调度 run 的实际类型是 schedule_{action}（非 schedule_action）
function typeLabel(runType: string): string {
  return TYPE_LABEL[runType] ?? runType
}

// 触发方式中文映射（未知值回退原值直出）
const TRIGGER_LABEL: Record<string, string> = {
  cron: '定时',
  misfire_run: '错过补跑',
  manual: '手动',
  batch_init: '批量初始化',
  auto_recover: '自动恢复',
  auto_handoff: '自动移交',
  restore_resume: '恢复续跑',
  auto: '自动',
}

function triggerLabel(trigger: string | null): string {
  if (!trigger) return '—'
  return TRIGGER_LABEL[trigger] ?? trigger
}

// T5.4：筛选/页码与 URL query 双向持久化（刷新、分享链接均可还原视图）
function syncQuery() {
  router.replace({ query: {
    ...(runType.value ? { run_type: runType.value } : {}),
    ...(page.value > 1 ? { page: String(page.value) } : {}),
  } })
}

async function load() {
  loading.value = true
  try {
    const r = await runApi.list({
      page: page.value, page_size: pageSize.value,
      run_type: runType.value || undefined,
    })
    runs.value = r.runs
    total.value = r.total
  } catch (e) {
    message.error(errMsg(e))   // F4：与全站统一错误处理对齐（修复前失败时表格空白无提示）
  } finally {
    loading.value = false
  }
}
onMounted(load)

// 实时刷新（此前页面只在挂载时拉一次：run 结束后列表状态停留「运行中」、详情弹窗数据过期）
useWsEvent(['run.finished'], (env) => {
  const p = env.payload as { run_id?: number }
  if (p?.run_id && detail.value?.id === p.run_id) {
    runApi.get(p.run_id).then((d) => { detail.value = d }).catch(() => {})
  }
  load()
})

async function openDetail(row: RunHistory) {
  try {
    detail.value = await runApi.get(row.id)
    detailShow.value = true
  } catch (e) {
    message.error(errMsg(e))
  }
}

const columns: DataTableColumns<RunHistory> = [
  { title: '编号', key: 'id', width: 80, render: (r) => h('span', { class: 'mono' }, `#${r.id}`) },
  { title: '任务', key: 'task_id', width: 80, render: (r) => r.task_id ? h('span', { class: 'mono' }, `#${r.task_id}`) : r.backup_id ? h('span', { class: 'mono', style: 'color:var(--Semantic-Color-info)' }, `备份 #${r.backup_id}`) : '—' },
  { title: '类型', key: 'run_type', width: 110, render: (r) => typeLabel(r.run_type) },
  { title: '触发', key: 'trigger', width: 90, render: (r) => triggerLabel(r.trigger) },
  { title: '状态', key: 'state', width: 130, render: (r) => h(StatusPill, { state: r.state }) },
  { title: '开始', key: 'started_at', width: 160, render: (r) => h('span', { class: 'mono', style: 'font-size:12px' }, r.started_at) },
  { title: '结束', key: 'ended_at', width: 160, render: (r) => h('span', { class: 'mono', style: 'font-size:12px' }, r.ended_at ?? '—') },
  { title: '表进度', key: 'tables', width: 90, render: (r) => h('span', { class: 'mono' }, `${r.tables_done}/${r.tables_total}`) },
  { title: '摘要', key: 'error_summary', ellipsis: { tooltip: true }, render: (r) =>
    r.error_summary ? h('span', { style: 'color: var(--Semantic-Color-error); font-size: 12px' }, r.error_summary) : '—' },
  { title: '', key: 'op', width: 70, render: (r) =>
    h(NButton, { size: 'tiny', quaternary: true, type: 'primary', onClick: () => openDetail(r) }, { default: () => '详情' }) },
]
</script>

<template>
  <div style="padding: 24px; max-width: 1400px">
    <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 16px">
      <h2 style="margin: 0; font-size: var(--Font-Size-font-h2)">执行历史</h2>
      <div style="flex: 1" />
      <n-select v-model:value="runType" :options="RUN_TYPES" style="width: 160px" @update:value="() => { page = 1; syncQuery(); load() }" />
    </div>
    <n-card style="border-radius: var(--Radius-radius-lg)">
      <n-data-table remote :columns="columns" :data="runs" :loading="loading" :row-key="(r: RunHistory) => r.id"
        :pagination="{
          page, pageSize, itemCount: total, showSizePicker: false,
          onChange: (p: number) => { page = p; syncQuery(); load() },
        }" size="small" />
    </n-card>

    <n-modal v-model:show="detailShow" preset="card" title="运行详情" style="width: 820px">
      <template v-if="detail">
        <n-descriptions label-placement="left" bordered :column="3" size="small">
          <n-descriptions-item label="状态"><StatusPill :state="detail.state" /></n-descriptions-item>
          <n-descriptions-item label="类型">{{ typeLabel(detail.run_type) }}</n-descriptions-item>
          <n-descriptions-item label="触发">{{ triggerLabel(detail.trigger) }}</n-descriptions-item>
        </n-descriptions>
        <template v-if="detail.report">
          <p style="font-size: var(--Font-Size-font-caption); margin: 12px 0 6px">运行报告：</p>
          <pre class="mono" style="background: var(--Semantic-Color-bg-base); border: 1px solid var(--Semantic-Color-border); border-radius: var(--Radius-radius-md); padding: 12px; font-size: 12px; max-height: 260px; overflow: auto">{{ JSON.stringify(detail.report, null, 2) }}</pre>
        </template>
        <template v-if="detail.table_details?.length">
          <p style="font-size: var(--Font-Size-font-caption); margin: 12px 0 6px">表级明细：</p>
          <n-data-table size="small" :max-height="240"
            :columns="[
              { title: '表', key: 'table_name', render: (r) => h('span', { class: 'mono' }, r.table_name) },
              { title: '状态', key: 'state', render: (r) => h(StatusPill, { state: r.state }) },
              { title: '行数', key: 'rows_done', render: (r) => h('span', { class: 'mono' }, `${r.rows_done ?? 0}/${r.rows_total ?? '—'}`) },
              { title: '错误', key: 'error', ellipsis: { tooltip: true } },
            ]"
            :data="detail.table_details" :row-key="(r: Record<string, unknown>) => String(r.id)" />
        </template>
        <n-tag v-if="detail.live_progress" type="info" size="small" style="margin-top: 10px">
          本次运行仍在进行中，可在「运行进度」页实时查看
        </n-tag>
      </template>
    </n-modal>
  </div>
</template>
