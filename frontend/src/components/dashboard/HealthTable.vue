<script setup lang="ts">
/**
 * 仪表盘 · 任务健康度表格（§6.2：n-data-table）。
 * 数据：GET /api/tasks（含 state/lag_seconds/progress）；WS task.state_changed / task.lag 原地更新。
 * 行点击 → 任务详情；延迟绿/黄/红 = 正常/接近阈值/超阈值。
 */
import { computed, h, ref } from 'vue'
import { NButton, NDataTable, type DataTableColumns } from 'naive-ui'
import { useRouter } from 'vue-router'
import type { SyncTask } from '../../api'
import StatusPill from '../StatusPill.vue'

const props = defineProps<{ tasks: SyncTask[] }>()

const router = useRouter()

function fmtLag(v: number | null | undefined): string {
  if (v == null) return '—'
  if (v < 1) return `${Math.round(v * 1000)} ms`
  if (v < 60) return `${v.toFixed(1)} s`
  return `${Math.floor(v / 60)} 分 ${Math.round(v % 60)} 秒`
}

function lagColor(t: SyncTask): string {
  if (t.lag_seconds == null) return 'var(--Semantic-Color-text-tertiary)'
  const th = t.lag_alert_sec || 60
  if (t.lag_seconds > th) return 'var(--Semantic-Color-error)'
  if (t.lag_seconds > th * 0.5) return 'var(--Semantic-Color-warning)'
  return 'var(--Semantic-Color-success)'
}

function progressText(t: SyncTask): string {
  const p = t.progress
  if (!p || t.progress == null) return '—'
  if (p.run_type === 'cdc' || p.phase === 'streaming') return '实时流'
  return `${p.tables_done}/${p.tables_total} 表`
}

const columns = computed<DataTableColumns<SyncTask>>(() => [
  { title: '任务', key: 'name', width: 180, ellipsis: { tooltip: true } },
  { title: '模式', key: 'mode_label', width: 130 },
  {
    title: '状态', key: 'state', width: 140,
    render: (row) => h(StatusPill, { state: row.state }),
  },
  {
    title: '延迟', key: 'lag_seconds', width: 120,
    render: (row) => h('span', {
      class: 'mono',
      style: { color: lagColor(row) },
    }, fmtLag(row.lag_seconds)),
  },
  { title: '进度', key: 'progress', width: 110, render: (row) => progressText(row) },
])

const rowProps = (row: SyncTask) => ({
  style: 'cursor: pointer',
  onClick: () => router.push({ name: 'task-detail', params: { id: row.id } }),
})

const loading = ref(false)
</script>

<template>
  <n-data-table :columns="columns" :data="props.tasks" :row-props="rowProps"
    :bordered="false" size="small" :loading="loading" />
  <div v-if="props.tasks.length === 0" class="empty">
    <span style="color: var(--Semantic-Color-text-tertiary)">暂无同步任务</span>
    <n-button size="small" type="primary" secondary
      @click="router.push({ name: 'task-wizard' })">去创建任务</n-button>
  </div>
</template>

<style scoped>
.empty {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--Spacing-space-3, 12px);
  padding: var(--Spacing-space-5, 24px) 0;
}
</style>
