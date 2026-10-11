<script setup lang="ts">
/** 数据源页（§6.2 页面 2）：列表 + 新建/编辑抽屉 + 预检面板 + 克隆/删除 */
import { computed, h, onMounted, ref } from 'vue'
import { NButton, NCard, NDataTable, NModal, NPopconfirm, NSpace, NSpin, NTag, useDialog, useMessage } from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import { dsApi, type Datasource, type PrecheckItem } from '../api'
import { errMsg } from '../api/client'
import DataSourceDrawer from '../components/DataSourceDrawer.vue'
import PrecheckPanel from '../components/PrecheckPanel.vue'

const message = useMessage()
const dialog = useDialog()

const list = ref<Datasource[]>([])
const loading = ref(false)

const drawerShow = ref(false)
const editing = ref<Datasource | null>(null)

const precheckShow = ref(false)
const precheckTarget = ref<Datasource | null>(null)
const prechecking = ref(false)
const precheckResult = ref<{ overall: string; items: PrecheckItem[]; checkedAt?: string }>({ overall: 'pass', items: [] })

async function load() {
  loading.value = true
  try {
    list.value = await dsApi.list()
  } catch (e) {
    message.error(errMsg(e))
  } finally {
    loading.value = false
  }
}
onMounted(load)

function openCreate() {
  editing.value = null
  drawerShow.value = true
}
function openEdit(row: Datasource) {
  editing.value = row
  drawerShow.value = true
}

async function doClone(row: Datasource) {
  try {
    const c = await dsApi.clone(row.id)
    message.success(`已克隆为「${c.name}」（凭据独立加密存储）`)
    await load()
  } catch (e) {
    message.error(errMsg(e))
  }
}

function doDelete(row: Datasource) {
  dialog.error({
    title: '删除数据源',
    content: `确定删除「${row.name}」（${row.host}:${row.port}）？该操作不可恢复。`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await dsApi.remove(row.id)
        message.success('已删除')
        await load()
      } catch (e) {
        message.error(errMsg(e))
      }
    },
  })
}

async function openPrecheck(row: Datasource) {
  precheckTarget.value = row
  precheckShow.value = true
  prechecking.value = true
  try {
    const r = await dsApi.precheck(row.id, 'both')
    precheckResult.value = { overall: r.overall, items: r.items, checkedAt: new Date().toLocaleString() }
    await load()
  } catch (e) {
    message.error(errMsg(e))
    precheckShow.value = false
  } finally {
    prechecking.value = false
  }
}

function precheckCell(row: Datasource) {
  if (!row.precheck) {
    return h(NTag, { size: 'small', bordered: false }, { default: () => '未预检' })
  }
  const map: Record<string, 'success' | 'warning' | 'error' | 'default'> = { pass: 'success', warn: 'warning', fail: 'error' }
  const text: Record<string, string> = { pass: '通过', warn: '警告', fail: '失败' }
  return h(NTag, { size: 'small', type: map[row.precheck.overall] || 'default', bordered: false, round: true },
    { default: () => text[row.precheck!.overall] || row.precheck!.overall })
}

const columns = computed<DataTableColumns<Datasource>>(() => [
  { title: '名称', key: 'name', width: 160 },
  { title: '主机', key: 'host', width: 150 },
  { title: '端口', key: 'port', width: 70, className: 'mono' },
  { title: '用户', key: 'username', width: 110 },
  { title: 'SSL', key: 'ssl_mode', width: 90 },
  { title: '预检状态', key: 'precheck', width: 100, render: precheckCell },
  { title: '被引用任务', key: 'referenced_tasks', width: 100, className: 'mono',
    render: (row) => String(row.referenced_tasks ?? 0) },
  { title: '操作', key: 'actions', width: 300,
    render: (row) => h(NSpace, { size: 'small' }, {
      default: () => [
        h(NButton, { size: 'tiny', type: 'primary', secondary: true, onClick: () => openPrecheck(row) }, { default: () => '测试连接' }),
        h(NButton, { size: 'tiny', secondary: true, onClick: () => openEdit(row) }, { default: () => '编辑' }),
        h(NButton, { size: 'tiny', secondary: true, onClick: () => doClone(row) }, { default: () => '克隆' }),
        h(NPopconfirm, { onPositiveClick: () => doDelete(row) }, {
          trigger: () => h(NButton, { size: 'tiny', type: 'error', secondary: true }, { default: () => '删除' }),
          default: () => `确定删除「${row.name}」？`,
        }),
      ],
    }) },
])
</script>

<template>
  <div class="page">
    <div style="display: flex; align-items: center; justify-content: space-between">
      <h1 class="page-title" style="margin-bottom: 16px">数据源管理</h1>
      <n-button type="primary" @click="openCreate" style="margin-bottom: 16px">＋ 新建数据源</n-button>
    </div>
    <n-card>
      <n-data-table :columns="columns" :data="list" :loading="loading" :bordered="false" size="small" />
    </n-card>

    <DataSourceDrawer v-model:show="drawerShow" :editing="editing" @saved="load" />

    <n-modal v-model:show="precheckShow" style="width: 640px" :title="`预检 · ${precheckTarget?.name ?? ''}`"
             preset="card" :bordered="true">
      <n-spin :show="prechecking">
        <PrecheckPanel :items="precheckResult.items" :overall="precheckResult.overall"
                       :checked-at="precheckResult.checkedAt" />
      </n-spin>
      <template #footer>
        <div style="font-size: var(--Font-Size-font-caption); color: var(--Semantic-Color-text-tertiary)">
          预检结果已持久化；最近一次结果显示在列表「预检状态」列。
        </div>
      </template>
    </n-modal>
  </div>
</template>
