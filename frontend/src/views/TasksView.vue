<script setup lang="ts">
/** 同步任务页（§6.2 页面 3，M2 版）：列表 + 快捷操作 + 进度入口 */
import { h, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { NButton, NCard, NPopconfirm, NSpace, NSpin, useDialog, useMessage } from 'naive-ui'
import { taskApi, type SyncTask } from '../api'
import { errMsg } from '../api/client'
import StatusPill from '../components/StatusPill.vue'
import { useWsEvent } from '../composables/wsBus'

const router = useRouter()
const message = useMessage()
const dialog = useDialog()

const tasks = ref<SyncTask[]>([])
const loading = ref(false)

async function load(markSilent = false) {
  if (!markSilent) loading.value = true
  try {
    tasks.value = (await taskApi.list()).tasks
  } catch (e) {
    message.error(errMsg(e))
  } finally {
    loading.value = false
  }
}
onMounted(() => load())

useWsEvent(['task.state_changed', 'run.finished', 'task.progress'], () => {
  if (!loading.value) load(true)
})

const RUNNING = ['initializing', 'full_resyncing', 'backing_up', 'cdc_running']
// 忙碌态 = 运行中 ∪ 暂停：删除/全量重刷必须对 paused 也隐藏（否则暂停后按钮异常冒出）
const BUSY = [...RUNNING, 'paused']
const pendingId = ref<number | null>(null)   // 暂停/恢复请求进行中的任务，按钮防抖

async function doInit(t: SyncTask) {
  dialog.warning({
    title: `发起初始化 · ${t.name}`,
    content: '目标库为空时直接初始化；非空需「清空重建」（将删除目标库全部表，不可恢复）。',
    positiveText: '目标库为空，直接初始化',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await taskApi.init(t.id, false, null)
        message.success('初始化已发起')
        router.push({ name: 'task-progress', params: { id: t.id } })
      } catch (e) {
        message.error(errMsg(e))
      }
    },
  })
}

function doInitRebuild(t: SyncTask) {
  let name = ''
  dialog.error({
    title: `清空重建 · ${t.name}`,
    content: () =>
      h('div', [
        h('p', { style: 'margin:0 0 8px' }, '将删除目标库中的全部表并重建，数据不可恢复。'),
        h('p', { style: 'margin:0 0 8px; color: var(--Semantic-Color-error)' }, '请手动输入目标库名以确认：'),
        h('input', {
          style: 'width:100%;padding:6px 10px;border:1px solid var(--Semantic-Color-border-strong);border-radius:6px;background:var(--Semantic-Color-bg-base);color:var(--Semantic-Color-text-primary)',
          placeholder: '目标库名',
          onInput: (ev: Event) => { name = (ev.target as HTMLInputElement).value },
        }),
      ]),
    positiveText: '确认清空重建',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await taskApi.init(t.id, true, name)
        message.success('清空重建初始化已发起')
        router.push({ name: 'task-progress', params: { id: t.id } })
      } catch (e) {
        message.error(errMsg(e))
      }
    },
  })
}

function doResync(t: SyncTask) {
  dialog.warning({
    title: `全量重刷 · ${t.name}`,
    content: '将对映射范围内目标表重新执行全量拷贝（覆盖写入），安全网备份自动开启。确认发起？',
    positiveText: '发起全量重刷',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await taskApi.fullResync(t.id, null)
        message.success('全量重刷已发起（安全网备份自动开启）')
        router.push({ name: 'task-progress', params: { id: t.id } })
      } catch (e) {
        message.error(errMsg(e))
      }
    },
  })
}

async function doPause(t: SyncTask) {
  pendingId.value = t.id
  try { await taskApi.pause(t.id); await load(true) } catch (e) { message.error(errMsg(e)) } finally { pendingId.value = null }
}
async function doResume(t: SyncTask) {
  pendingId.value = t.id
  try { await taskApi.resume(t.id); await load(true) } catch (e) { message.error(errMsg(e)) } finally { pendingId.value = null }
}
async function doRecover(t: SyncTask) {
  pendingId.value = t.id
  try {
    await taskApi.recover(t.id)
    message.success('任务已恢复为空闲：可重新发起全量/备份，或等待下次调度')
    await load(true)
  } catch (e) { message.error(errMsg(e)) } finally { pendingId.value = null }
}

function doCancel(t: SyncTask) {
  dialog.warning({
    title: `取消运行 · ${t.name}`,
    content: '将请求取消当前运行（断点已落库的进度保留）。确认取消？',
    positiveText: '取消运行',
    negativeText: '继续等待',
    onPositiveClick: async () => {
      try { await taskApi.cancel(t.id); await load(true) } catch (e) { message.error(errMsg(e)) }
    },
  })
}

function doDelete(t: SyncTask) {
  dialog.error({
    title: '删除任务',
    content: `确定删除任务「${t.name}」？任务与同步位点数据将被移除。`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try { await taskApi.remove(t.id); await load(true) } catch (e) { message.error(errMsg(e)) }
    },
  })
}

function pct(t: SyncTask): number | null {
  if (!t.progress || !t.progress.rows_total) return null
  return Math.min(100, Math.round((t.progress.rows_done / t.progress.rows_total) * 100))
}
</script>

<template>
  <div style="padding: 24px; max-width: 1200px">
    <div style="display: flex; align-items: center; margin-bottom: 16px">
      <h2 style="margin: 0; font-size: var(--Font-Size-font-h2)">同步任务</h2>
      <div style="flex: 1" />
      <n-button type="primary" @click="router.push({ name: 'task-wizard' })">＋ 新建任务</n-button>
    </div>

    <n-spin :show="loading">
      <n-empty v-if="!tasks.length && !loading" description="暂无任务，点击右上角新建" style="padding: 64px 0" />
      <div style="display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 16px">
        <n-card v-for="t in tasks" :key="t.id" style="border-radius: var(--Radius-radius-lg)">
          <template #header>
            <span style="font-size: var(--Font-Size-font-h3)">{{ t.name }}</span>
          </template>
          <template #header-extra>
            <StatusPill :state="t.state" />
          </template>
          <div style="font-size: var(--Font-Size-font-caption); color: var(--Semantic-Color-text-secondary); margin-bottom: 12px">
            <span class="mono">{{ t.mode_label }}</span>
            · 源 #{{ t.source_id }} → 目标 #{{ t.target_id }}
            <template v-if="t.mapping?.target_database"> · 库映射 <span class="mono">{{ t.mapping.target_database }}</span></template>
          </div>
          <div v-if="t.progress && RUNNING.includes(t.state)" style="margin-bottom: 12px">
            <n-progress
              type="line" :percentage="pct(t) ?? 0" indicator-placement="inside"
              :height="10" :border-radius="6" processing />
            <div style="font-size: var(--Font-Size-font-micro); color: var(--Semantic-Color-text-tertiary); margin-top: 4px">
              阶段 <span class="mono">{{ t.progress.phase }}</span>
              · 表 <span class="mono">{{ t.progress.tables_done }}/{{ t.progress.tables_total }}</span>
              · <span class="mono">{{ t.progress.speed_rows_per_sec }}</span> 行/s
              <template v-if="t.progress.eta_seconds != null"> · 剩余约 <span class="mono">{{ t.progress.eta_seconds }}s</span></template>
            </div>
          </div>
          <n-space size="small">
            <n-button v-if="t.state === 'draft'" size="small" type="primary" @click="doInit(t)">发起初始化</n-button>
            <n-button v-if="t.state === 'draft'" size="small" type="error" secondary @click="doInitRebuild(t)">清空重建</n-button>
            <n-button v-if="t.state === 'error' && t.mode !== 'init_plus_cdc'" size="small" type="primary" secondary
              :disabled="pendingId === t.id" @click="doRecover(t)">人工恢复</n-button>
            <n-button v-if="!BUSY.includes(t.state) && t.mode !== 'backup_only'" size="small" secondary
              :disabled="t.state === 'error' || t.state === 'needs_reinit' || pendingId === t.id" @click="doResync(t)">全量重刷</n-button>
            <n-button v-if="RUNNING.includes(t.state) && !t.progress?.paused" size="small" secondary
              :disabled="pendingId === t.id" @click="doPause(t)">暂停</n-button>
            <n-button v-if="t.state === 'paused' || t.progress?.paused" size="small" secondary type="primary"
              :disabled="pendingId === t.id" @click="doResume(t)">恢复</n-button>
            <n-button v-if="BUSY.includes(t.state)" size="small" secondary type="error"
              :disabled="pendingId === t.id" @click="doCancel(t)">取消</n-button>
            <n-button size="small" quaternary @click="router.push({ name: 'task-detail', params: { id: t.id } })">详情</n-button>
            <n-button size="small" quaternary @click="router.push({ name: 'task-progress', params: { id: t.id } })">进度</n-button>
            <n-popconfirm v-if="!BUSY.includes(t.state)" @positive-click="doDelete(t)">
              <template #trigger><n-button size="small" quaternary type="error" :disabled="pendingId === t.id">删除</n-button></template>
              确认删除该任务？
            </n-popconfirm>
          </n-space>
        </n-card>
      </div>
    </n-spin>
  </div>
</template>
