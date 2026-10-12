<script setup lang="ts">
/**
 * 校验报告页（T4.6 / §6.2 页面 9）：报告列表 → 逐表结果 → 不一致块行级差异明细
 * （差异列高亮、两端值对照）→「以源库覆盖修复选中差异」带二次确认。
 */
import { computed, h, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import {
  NAlert, NButton, NCard, NDataTable, NDrawer, NDrawerContent, NPagination, NSelect,
  NSpace, NTag, useDialog, useMessage,
} from 'naive-ui'
import type { DataTableColumns, SelectOption } from 'naive-ui'
import {
  verifyApi, type VerifyDiff, type VerifyResult, type VerifyResultDetail,
  type VerifyTableResult,
} from '../api'
import { errMsg } from '../api/client'
import StatusPill from '../components/StatusPill.vue'

const route = useRoute()
const message = useMessage()
const dialog = useDialog()

const items = ref<VerifyResult[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = 20
const taskFilter = ref<number | null>(route.query.task_id ? Number(route.query.task_id) : null)
const taskOptions = ref<SelectOption[]>([])
const loading = ref(false)

// ---- 详情抽屉 ----
const showDetail = ref(false)
const detail = ref<VerifyResultDetail | null>(null)
const detailLoading = ref(false)
const drillTable = ref<string | null>(null)

// 修复范围模式：diff = 按报告记录差异行（每表 ≤100 条）；full = 整表对账覆盖（不受上限约束）
const repairMode = ref<'diff' | 'full'>('diff')
const repairModeOptions: SelectOption[] = [
  { label: '按报告差异行修复', value: 'diff' },
  { label: '整表对账覆盖（不受差异条数上限约束）', value: 'full' },
]
watch(drillTable, () => { repairMode.value = 'diff' })

async function load() {
  loading.value = true
  try {
    const r = await verifyApi.list({
      task_id: taskFilter.value ?? undefined, page: page.value, page_size: pageSize })
    items.value = r.items
    total.value = r.total
  } catch (e) {
    message.error(errMsg(e))
  } finally {
    loading.value = false
  }
}

async function loadTasks() {
  try {
    const { taskApi } = await import('../api')
    const r = await taskApi.list()
    taskOptions.value = [{ label: '全部任务', value: '-1' }]
      .concat(r.tasks.map((t) => ({ label: `#${t.id} ${t.name}`, value: String(t.id) })))
  } catch { /* 非阻塞 */ }
}

onMounted(() => {
  void load()
  void loadTasks()
})

function onFilterChange(v: string | null) {
  taskFilter.value = v && v !== '-1' ? Number(v) : null
  page.value = 1
  void load()
}

const METHOD_LABEL: Record<string, string> = { row_count: '行数', checksum: 'checksum' }

const columns: DataTableColumns<VerifyResult> = [
  { title: 'ID', key: 'id', width: 70, className: 'mono' },
  { title: '任务', key: 'task_id', width: 80, className: 'mono' },
  { title: '方式', key: 'method', width: 100,
    render: (r) => METHOD_LABEL[r.method] ?? r.method },
  { title: '触发', key: 'trigger', width: 110,
    render: (r) => ({ manual: '手动', cron: '定时', auto: '重刷后自动' }[r.trigger] ?? r.trigger) },
  { title: '状态', key: 'state', width: 110,
    render: (r) => h(StatusPill, { state: r.state === 'mismatch' ? 'failed' : r.state,
      label: r.state === 'mismatch' ? '不一致' : undefined }) },
  { title: '校验表数', key: 'tc', width: 100, className: 'mono',
    render: (r) => `${r.tables_checked}/${r.tables_total}` },
  { title: '不一致表', key: 'mt', width: 90, className: 'mono',
    render: (r) => r.mismatch_tables > 0
      ? h('span', { style: 'color: var(--Semantic-Color-error); font-weight: 600' },
          String(r.mismatch_tables))
      : '0' },
  { title: '开始时间', key: 'started_at', width: 170, className: 'mono',
    render: (r) => (r.started_at || '').replace('T', ' ').slice(0, 19) },
  { title: '操作', key: 'ops', width: 100,
    render: (r) => h(NButton, { size: 'tiny', quaternary: true,
      onClick: () => openDetail(r) }, { default: () => '查看报告' }) },
]

async function openDetail(r: VerifyResult) {
  showDetail.value = true
  drillTable.value = null
  detailLoading.value = true
  try {
    detail.value = await verifyApi.get(r.id, true)
  } catch (e) {
    message.error(errMsg(e))
  } finally {
    detailLoading.value = false
  }
}

// ---- 逐表结果 ----
const tableRows = computed<Array<{ name: string; result: VerifyTableResult }>>(() => {
  const tables = detail.value?.detail?.tables ?? {}
  return Object.entries(tables).map(([name, result]) => ({ name, result }))
})

const drillColumns: DataTableColumns<{ name: string; result: VerifyTableResult }> = [
  { title: '表名', key: 'name', className: 'mono',
    render: (r) => h('span', null, [
      r.result.filtered
        ? h(NTag, { size: 'tiny', bordered: false, type: 'info',
            style: 'margin-right: 6px' }, { default: () => '子集口径' })
        : null,
      r.name,
    ]) },
  { title: '状态', key: 'status', width: 100,
    render: (r) => h(NTag, { size: 'small', bordered: false,
      type: r.result.status === 'match' ? 'success' : 'error' },
      { default: () => (r.result.status === 'match' ? '一致' : '不一致') }) },
  { title: '源行数', key: 'src', width: 110, className: 'mono',
    render: (r) => (r.result.src_count ?? '—').toLocaleString() },
  { title: '目标行数', key: 'tgt', width: 110, className: 'mono',
    render: (r) => (r.result.tgt_count ?? '—').toLocaleString() },
  { title: '差异行', key: 'diff', width: 90, className: 'mono',
    render: (r) => r.result.diff_count
      ? h('span', { style: 'color: var(--Semantic-Color-error); font-weight: 600' },
          String(r.result.diff_count))
      : '0' },
  { title: '说明', key: 'warn' ,
    render: (r) => r.result.warning ?? '—' },
  { title: '操作', key: 'ops', width: 110,
    render: (r) => {
      const n = r.result.diffs?.length ?? 0
      return n
        ? h(NButton, { size: 'tiny', quaternary: true, type: 'warning',
            onClick: () => { drillTable.value = r.name } },
            { default: () => `查看 ${n} 条差异` })
        : '—'
    } },
]

const drillDiffs = computed<VerifyDiff[]>(() =>
  drillTable.value ? (detail.value?.detail?.tables?.[drillTable.value]?.diffs ?? []) : [])

const tableRowKey = (r: { name: string }) => r.name
const diffRowKey = (r: VerifyDiff) => JSON.stringify(r.pk)

const diffColumns: DataTableColumns<VerifyDiff> = [
  { title: '类型', key: 'type', width: 110,
    render: (r) => h(NTag, { size: 'small', bordered: false,
      type: r.type === 'mismatch' ? 'warning' : 'error' },
      { default: () => ({ mismatch: '值不一致', target_missing: '目标缺行',
        target_extra: '目标多余' }[r.type] ?? r.type) }) },
  { title: '主键', key: 'pk', width: 170, className: 'mono',
    render: (r) => (r.pk ?? []).join(' + ') },
  { title: '差异明细（差异列高亮 · 两端值对照）', key: 'detail',
    render: (r) => renderDiffDetail(r) },
]

function renderDiffDetail(r: VerifyDiff): ReturnType<typeof h> {
  const cell = (col: string, val: unknown, cls: string) =>
    h('div', { class: 'diff-cell' }, [
      h('span', { class: 'col-name' }, col + '：'),
      h('span', { class: cls }, JSON.stringify(val)),
    ])
  if (r.type === 'mismatch' && r.columns) {
    return h('div', { class: 'mono diff-cells' },
      Object.entries(r.columns).map(([col, v]) =>
        h('div', { class: 'diff-cell highlight' }, [
          h('span', { class: 'col-name' }, col + '：'),
          h('span', { class: 'val-src' }, `源=${JSON.stringify(v.source)}`),
          h('span', { class: 'val-tgt' }, ` 目标=${JSON.stringify(v.target)}`),
        ])))
  }
  if (r.type === 'target_missing' && r.source) {
    return h('div', { class: 'mono diff-cells' },
      Object.entries(r.source).map(([col, v]) => cell(col, v, 'val-src')))
  }
  if (r.type === 'target_extra' && r.target) {
    return h('div', { class: 'mono diff-cells' },
      Object.entries(r.target).map(([col, v]) => cell(col, v, 'val-tgt')))
  }
  return h('span', { style: 'color: var(--Semantic-Color-text-tertiary)' }, '—')
}

function doRepair() {
  const d = detail.value
  if (!d) return
  const fullMode = repairMode.value === 'full' && drillTable.value != null
  dialog.error({
    title: '以源库覆盖修复选中差异',
    content: fullMode
      ? `将对表 ${drillTable.value} 做整表对账覆盖：按源库当前值全表比对并修复（不受报告差异条数上限约束，差异行全量 REPLACE / 目标多余删除）。此操作直接修改目标库数据，确认执行？`
      : `将按源库当前值覆盖目标库差异行（REPLACE 幂等写），目标多余行删除`
        + `（行过滤子集表的删除类修复会被自动跳过）${drillTable.value ? `，范围：表 ${drillTable.value}` : '，范围：全部差异表'}。`
        + `此操作直接修改目标库数据，确认执行？`,
    positiveText: '确认修复', negativeText: '取消',
    onPositiveClick: async () => {
      try {
        let body: Record<string, unknown> = {}
        if (fullMode) {
          body = { tables: [drillTable.value], full_tables: [drillTable.value] }
        } else if (drillTable.value) {
          body = { tables: [drillTable.value] }
        }
        const r = await verifyApi.repair(d.id, body)
        message.success(`修复完成：覆盖 ${r.replaced} 行，删除 ${r.deleted} 行，跳过 ${r.skipped} 行`)
        await openDetail(d)
      } catch (e) {
        message.error(errMsg(e))
      }
    },
  })
}
</script>

<template>
  <div style="padding: 24px; max-width: 1200px">
    <h1 style="font-size: var(--Font-Size-font-h2); margin: 0 0 16px">校验报告</h1>

    <n-card style="border-radius: var(--Radius-radius-lg); margin-bottom: 12px">
      <div style="display: flex; gap: 12px; align-items: center">
        <n-select :value="taskFilter ? String(taskFilter) : '-1'" :options="taskOptions"
          size="small" style="width: 260px" @update:value="onFilterChange" />
        <span class="stat-sub">行数校验在全量/重刷完成后自动执行；checksum 校验支持行级定位与以源库覆盖修复。</span>
      </div>
    </n-card>

    <n-card style="border-radius: var(--Radius-radius-lg)">
      <n-data-table :columns="columns" :data="items" :loading="loading" size="small"
        :bordered="false" :row-key="(r: VerifyResult) => r.id" />
      <div style="display: flex; justify-content: flex-end; margin-top: 12px">
        <n-pagination v-model:page="page" :page-size="pageSize" :item-count="total"
          @update:page="load" />
      </div>
    </n-card>

    <n-drawer v-model:show="showDetail" :width="860">
      <n-drawer-content
        :title="`校验报告 #${detail?.id ?? ''}（${METHOD_LABEL[detail?.method ?? ''] ?? ''}）`"
        closable>
        <div v-if="detailLoading" class="stat-sub" style="padding: 24px">加载中…</div>
        <template v-else-if="detail">
          <n-space size="small" style="margin-bottom: 12px">
            <StatusPill :state="detail.state === 'mismatch' ? 'failed' : detail.state"
              :label="detail.state === 'mismatch' ? '不一致' : undefined" />
            <n-tag size="small" :bordered="false" class="mono">
              校验 {{ detail.tables_checked }}/{{ detail.tables_total }} 表</n-tag>
            <n-tag size="small" :bordered="false" :type="detail.mismatch_tables ? 'error' : 'default'"
              class="mono">不一致 {{ detail.mismatch_tables }} 表</n-tag>
            <n-tag v-if="detail.summary?.mismatch_rows" size="small" type="error" :bordered="false"
              class="mono">差异行 {{ detail.summary.mismatch_rows }}</n-tag>
          </n-space>

          <n-alert v-for="(w, i) in detail.detail?.warnings ?? []" :key="i" type="warning"
            :bordered="false" style="margin-bottom: 8px">{{ w }}</n-alert>
          <n-alert v-if="detail.summary?.error" type="error" style="margin-bottom: 8px">
            {{ detail.summary.error }}</n-alert>

          <n-data-table :columns="drillColumns" :data="tableRows" size="small" :bordered="false"
            :row-key="tableRowKey" style="margin-bottom: 12px" />

          <n-card v-if="drillTable" size="small"
            style="border-radius: var(--Radius-radius-lg); margin-bottom: 12px">
            <template #header>
              <span style="font-size: var(--Font-Size-font-h3)">行级差异：{{ drillTable }}</span>
            </template>
            <template #header-extra>
              <n-button size="tiny" quaternary @click="drillTable = null">收起</n-button>
            </template>
            <n-data-table :columns="diffColumns" :data="drillDiffs" size="small" :bordered="false"
              :max-height="320" :row-key="diffRowKey" />
          </n-card>

          <div style="display: flex; gap: 12px; align-items: center">
            <n-button type="error" size="small" :disabled="!detail.mismatch_tables"
              @click="doRepair">以源库覆盖修复选中差异…</n-button>
            <n-select v-if="drillTable" v-model:value="repairMode" :options="repairModeOptions"
              size="small" style="width: 300px" />
          </div>
          <p class="stat-sub" style="margin-top: 8px">
            修复语义：差异行按源库当前值 REPLACE（幂等）；「目标多余」行删除（行过滤子集表自动跳过此类修复）；
            支持复合主键表；钻取单表时可切换「整表对账覆盖」全量修复该表差异（不受报告差异条数上限约束）。
            敏感列（password/secret/token 等）已脱敏展示与存储。
          </p>
        </template>
      </n-drawer-content>
    </n-drawer>
  </div>
</template>

<style scoped>
.stat-sub {
  font-size: var(--Font-Size-font-caption);
  color: var(--Semantic-Color-text-tertiary);
}
.mono {
  font-family: 'JetBrains Mono', Consolas, monospace;
}
.diff-cells {
  font-size: var(--Font-Size-font-mono-body);
  line-height: 1.7;
}
.diff-cell.highlight {
  background: color-mix(in srgb, var(--Semantic-Color-warning) 12%, transparent);
  border-radius: var(--Radius-radius-sm);
  padding: 0 6px;
}
.col-name {
  color: var(--Semantic-Color-text-secondary);
}
.val-src {
  color: var(--Semantic-Color-success);
}
.val-tgt {
  color: var(--Semantic-Color-error);
}
</style>
