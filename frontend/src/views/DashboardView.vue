<script setup lang="ts">
/**
 * 仪表盘（§6.2 页面 1）：统计卡 / 任务健康度表格 / 多任务延迟曲线 / 最近告警与运行时间线。
 * 数据：REST /api/dashboard/summary + /api/tasks 初始化；WS 事件增量更新；
 * ws.snapshot 重连快照后全量重拉对齐（F3 同 TaskDetailView 口径）。
 */
import { onMounted, ref } from 'vue'
import { NAlert, NButton, NCard, NTag } from 'naive-ui'
import { dashboardApi, taskApi, type DashboardSummary, type SyncTask } from '../api'
import { errMsg } from '../api/client'
import { useWsEvent, useWsStatus } from '../composables/wsBus'
import StatCards from '../components/dashboard/StatCards.vue'
import HealthTable from '../components/dashboard/HealthTable.vue'
import LagChart from '../components/dashboard/LagChart.vue'
import RecentAlerts from '../components/dashboard/RecentAlerts.vue'
import RecentRuns from '../components/dashboard/RecentRuns.vue'

const summary = ref<DashboardSummary | null>(null)
const tasks = ref<SyncTask[]>([])
const loadError = ref('')
const loading = ref(false)
const { status } = useWsStatus()

let lastRunReload = 0

async function loadAll() {
  loading.value = true
  loadError.value = ''
  try {
    const [s, t] = await Promise.all([dashboardApi.summary(), taskApi.list()])
    summary.value = s
    tasks.value = t.tasks
  } catch (e) {
    loadError.value = errMsg(e)
  } finally {
    loading.value = false
  }
}

async function reloadTasks() {
  try {
    tasks.value = (await taskApi.list()).tasks
  } catch { /* 保留旧数据，断线时冻结 */ }
}

async function reloadSummary() {
  try {
    summary.value = await dashboardApi.summary()
  } catch { /* 保留旧数据 */ }
}

onMounted(loadAll)

useWsEvent(['task.state_changed', 'alert.fired', 'alert.resolved', 'run.finished', 'ws.snapshot'], (env) => {
  if (env.type === 'ws.snapshot') {
    // F3：重连快照对齐 → 全量重拉
    void loadAll()
    return
  }
  if (env.type === 'task.state_changed') {
    void reloadTasks()
    void reloadSummary()
    return
  }
  if (env.type === 'alert.fired' || env.type === 'alert.resolved') {
    void reloadSummary()
    return
  }
  // run.finished：今日事件量 / 最近运行节流重拉（5s）
  const now = Date.now()
  if (now - lastRunReload < 5000) return
  lastRunReload = now
  void reloadSummary()
})
</script>

<template>
  <div class="page">
    <div style="display: flex; align-items: center; gap: 12px">
      <h1 class="page-title">仪表盘</h1>
      <div style="flex: 1" />
      <span style="font-size: var(--Font-Size-font-micro); color: var(--Semantic-Color-text-tertiary)">
        实时通道
      </span>
      <n-tag :type="status === 'open' ? 'success' : status === 'connecting' ? 'warning' : 'error'"
        size="small" round>
        {{ status === 'open' ? 'WS 已连接' : status === 'connecting' ? '连接中' : '已断开' }}
      </n-tag>
    </div>

    <n-alert v-if="loadError" type="error" style="margin-bottom: 16px" :show-icon="true">
      {{ loadError }}
      <n-button size="tiny" quaternary type="primary" @click="loadAll">重试</n-button>
    </n-alert>

    <StatCards :summary="summary" />

    <div class="mid-grid">
      <n-card title="任务健康度" size="small" :bordered="true">
        <HealthTable :tasks="tasks" />
      </n-card>
      <n-card title="延迟趋势" size="small" :bordered="true">
        <LagChart :tasks="tasks" />
      </n-card>
    </div>

    <div class="bottom-grid">
      <n-card title="最近告警" size="small" :bordered="true">
        <RecentAlerts />
      </n-card>
      <n-card title="最近运行" size="small" :bordered="true">
        <RecentRuns />
      </n-card>
    </div>
  </div>
</template>

<style scoped>
.mid-grid {
  display: grid;
  grid-template-columns: 3fr 2fr;
  gap: var(--Spacing-space-4, 16px);
  margin-bottom: var(--Spacing-space-4, 16px);
}
.bottom-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--Spacing-space-4, 16px);
}
@media (max-width: 1100px) {
  .mid-grid,
  .bottom-grid {
    grid-template-columns: 1fr;
  }
}
</style>
