<script setup lang="ts">
/** 执行进度页（T2.7 / §6.2 页面 6）：任务级总进度 + 表级进度（虚拟滚动）+ 暂停/取消 + WS 实时刷新 */
import { computed, h, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { NAlert, NButton, NCard, NDataTable, NProgress, NSpace, NTag, useMessage } from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import { taskApi, type SyncTask, type TableProgressRow } from '../api'
import { errMsg } from '../api/client'
import StatusPill from '../components/StatusPill.vue'
import { useWsEvent } from '../composables/wsBus'

const route = useRoute()
const message = useMessage()
const taskId = Number(route.params.id)

const task = ref<SyncTask | null>(null)
const progress = ref(task.value?.progress ?? null)

const RUNNING = ['initializing', 'full_resyncing', 'backing_up', 'cdc_running']
const running = computed(() => task.value && RUNNING.includes(task.value.state))
// 忙碌态 = 运行中 ∪ 暂停：暂停中的任务同样允许取消
const busyState = computed(() => running.value || task.value?.state === 'paused')

async function load() {
  try {
    task.value = await taskApi.get(taskId)
    progress.value = task.value.progress ?? null
  } catch (e) {
    message.error(errMsg(e))
  }
}
onMounted(load)

useWsEvent(['task.progress'], (env) => {
  const p = env.payload as { task_id?: number } | undefined
  if (p && p.task_id === taskId) progress.value = p as never
})
useWsEvent(['task.state_changed', 'run.finished'], (env) => {
  // F3：重连快照载荷结构与事件不同（无 task_id），显式触发重拉对齐页面状态
  if (env.type === 'ws.snapshot') { void load(); return }
  const p = env.payload as { task_id?: number } | undefined
  if (!p || p.task_id === taskId) load()
})

const pct = computed(() => {
  const p = progress.value
  if (!p || !p.rows_total) return 0
  return Math.min(100, Math.round((p.rows_done / p.rows_total) * 100))
})

const PHASE_LABEL: Record<string, string> = {
  prepare: '准备', position: '获取一致同步位点', structure: '结构迁移', copy: '数据拷贝',
  finalize: '收尾校验', restore: '恢复回放', done: '完成',
}

const ddlBlocked = computed(() => (progress.value?.tables ?? []).filter((t) => t.state === 'ddl_blocked'))
const failedTables = computed(() => (progress.value?.tables ?? []).filter((t) => t.state === 'failed'))

const fmt = (n: number | null | undefined) => (n ?? 0).toLocaleString()

const tableCols: DataTableColumns<TableProgressRow> = [
  { title: '表', key: 'name', width: 260, render: (r) => h('span', { class: 'mono' }, r.name) },
  { title: '状态', key: 'state', width: 140, render: (r) => h(StatusPill, { state: r.state }) },
  { title: '已拷贝 / 总行数', key: 'rows', render: (r) =>
    h('span', { class: 'mono' }, `${fmt(r.rows_done)} / ${fmt(r.rows_total)}`) },
  { title: '进度', key: 'pct', width: 220, render: (r) => {
    const total = r.rows_total ?? 0
    const p = total > 0 ? Math.min(100, Math.round((r.rows_done / total) * 100)) : 0
    return h(NProgress, { type: 'line', percentage: p, showIndicator: false, height: 8, borderRadius: 4,
      status: r.state === 'failed' ? 'error' : r.state === 'ddl_blocked' ? 'warning' : 'default' })
  } },
  { title: '说明', key: 'error', render: (r) => r.error
    ? h('span', { style: 'color: var(--Semantic-Color-error); font-size: var(--Font-Size-font-caption)' }, r.error)
    : h('span', { style: 'color: var(--Semantic-Color-text-tertiary); font-size: var(--Font-Size-font-caption)' }, '—') },
]

async function doPause() { try { await taskApi.pause(taskId); await load() } catch (e) { message.error(errMsg(e)) } }
async function doResume() { try { await taskApi.resume(taskId); await load() } catch (e) { message.error(errMsg(e)) } }
async function doCancel() { try { await taskApi.cancel(taskId); await load() } catch (e) { message.error(errMsg(e)) } }
async function doDdl(decision: 'execute' | 'skip') {
  try {
    const r = await taskApi.ddlDecision(taskId, decision, false)
    message.success(`已决策：${decision === 'execute' ? '执行' : '跳过'}，受影响表重做中`)
    await load()
  } catch (e) {
    message.error(errMsg(e))
  }
}
</script>

<template>
  <div style="padding: 24px; max-width: 1200px">
    <div style="display: flex; align-items: center; margin-bottom: 16px; gap: 12px">
      <h2 style="margin: 0; font-size: var(--Font-Size-font-h2)">
        {{ task?.name || '任务' }} · 执行进度
      </h2>
      <StatusPill v-if="task" :state="task.state" />
      <n-tag size="small" :bordered="false">
        阶段：{{ progress ? PHASE_LABEL[progress.phase] || progress.phase : '—' }}
      </n-tag>
      <div style="flex: 1" />
      <n-space size="small">
        <n-button v-if="running && !progress?.paused" size="small" secondary @click="doPause">暂停</n-button>
        <n-button v-if="progress?.paused" size="small" secondary type="primary" @click="doResume">恢复</n-button>
        <n-button v-if="busyState" size="small" secondary type="error" @click="doCancel">取消</n-button>
        <n-button v-if="ddlBlocked.length" size="small" type="warning" secondary @click="doDdl('execute')">DDL：执行并重做</n-button>
        <n-button v-if="ddlBlocked.length" size="small" type="warning" secondary @click="doDdl('skip')">DDL：跳过</n-button>
      </n-space>
    </div>

    <n-alert v-if="progress?.paused" type="warning" :bordered="false" style="margin-bottom: 12px">
      运行已暂停：已停取新批，表级断点已记录，恢复后续拷不重不漏。
    </n-alert>
    <n-alert v-if="ddlBlocked.length" type="warning" style="margin-bottom: 12px">
      <b>表结构已变更（检测到源库 DDL）</b>：拷贝期检测到源库 DDL 变更，以下表已中止：
      <span class="mono">{{ ddlBlocked.map((t) => t.name).join('、') }}</span>
      。请决策「执行并重做该表」或「跳过」。
    </n-alert>
    <n-alert v-if="failedTables.length" type="error" :bordered="false" style="margin-bottom: 12px">
      失败表（批级重试耗尽）：<span class="mono">{{ failedTables.map((t) => t.name).join('、') }}</span>
      ，其余表不受影响；可全量重刷单表纠偏。
    </n-alert>

    <n-card style="border-radius: var(--Radius-radius-lg); margin-bottom: 16px">
      <n-progress type="line" :percentage="pct" indicator-placement="inside" :height="18" :border-radius="9"
        :processing="!!(running && !progress?.paused)" />
      <div style="display: flex; gap: 24px; margin-top: 12px; font-size: var(--Font-Size-font-caption); color: var(--Semantic-Color-text-secondary)">
        <span>行进度 <span class="mono">{{ fmt(progress?.rows_done) }} / {{ fmt(progress?.rows_total) }}</span></span>
        <span>速率 <span class="mono">{{ fmt(progress?.speed_rows_per_sec) }}</span> 行/s</span>
        <span>表 <span class="mono">{{ progress?.tables_done ?? 0 }}/{{ progress?.tables_total ?? 0 }}</span></span>
        <span v-if="progress?.eta_seconds != null">预计剩余 <span class="mono">{{ progress.eta_seconds }}s</span></span>
        <span v-if="progress?.run_id" class="mono">运行 #{{ progress.run_id }}</span>
      </div>
    </n-card>

    <n-card style="border-radius: var(--Radius-radius-lg)">
      <n-data-table :columns="tableCols" :data="progress?.tables ?? []" :max-height="420"
        :row-key="(r: TableProgressRow) => r.name" virtual-scroll size="small" />
    </n-card>
  </div>
</template>
