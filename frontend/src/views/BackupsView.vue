<script setup lang="ts">
/** 备份页（T2.11 / §6.2 页面 8）：文件表格 + 手动备份抽屉 + 文件头详情 + 恢复三步向导（T2.10） */
import { computed, h, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  NAlert, NButton, NCard, NCheckbox, NDataTable, NDrawer, NDrawerContent, NForm, NFormItem,
  NInput, NModal, NSelect, NSpace, NSteps, NStep, NSwitch, NTag, useDialog, useMessage,
} from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import { backupApi, dsApi, taskApi, runApi, type BackupFile, type Datasource, type RestorePrecheck } from '../api'
import { errMsg } from '../api/client'

const message = useMessage()
const dialog = useDialog()
const router = useRouter()

const backups = ref<BackupFile[]>([])
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    backups.value = (await backupApi.list()).backups
  } catch (e) {
    message.error(errMsg(e))
  } finally {
    loading.value = false
  }
}
onMounted(load)

function fmtSize(n: number): string {
  if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(2)} GB`
  if (n >= 1024 ** 2) return `${(n / 1024 ** 2).toFixed(2)} MB`
  return `${(n / 1024).toFixed(1)} KB`
}

// ---- 手动备份抽屉 ----
const drawerShow = ref(false)
const dsList = ref<Datasource[]>([])
const dsOptions = ref<Array<{ label: string; value: number }>>([])
const bkForm = ref({ datasource_id: null as number | null, schema: '', tables: null as string[] | null, structure_only: false })
const schemaOptions = ref<string[]>([])
const tableOptions = ref<Array<{ label: string; value: string }>>([])
const schemaSelectOptions = computed(() => schemaOptions.value.map((s) => ({ label: s, value: s })))
// /tables 全量返回（跨业务库）：库名 + 表名 对，供按库过滤
const allTables = ref<Array<{ schema: string; name: string }>>([])
const submitting = ref(false)

async function ensureDsList() {
  if (dsList.value.length) return
  try {
    dsList.value = await dsApi.list()
    dsOptions.value = dsList.value.map((d) => ({ label: `${d.name}（${d.host}:${d.port}）`, value: d.id }))
  } catch (e) { message.error(errMsg(e)) }
}

async function openDrawer() {
  drawerShow.value = true
  await ensureDsList()
}

async function onDsChange(id: number) {
  const ds = dsList.value.find((d) => d.id === id)
  schemaOptions.value = []
  tableOptions.value = []
  allTables.value = []
  bkForm.value.tables = null
  if (id) {
    try {
      const r = await dsApi.tables(id)
      // /tables 返回 schema/table（list_tables 口径），收集真实库名列表
      const seen = new Set<string>()
      allTables.value = (r.tables as Array<Record<string, unknown>>)
        .map((t) => ({ schema: String(t.schema ?? ''), name: String(t.table ?? t.name ?? t.TABLE_NAME ?? '') }))
        .filter((t) => t.name && (seen.has(`${t.schema}.${t.name}`) ? false : (seen.add(`${t.schema}.${t.name}`), true)))
      schemaOptions.value = [...new Set(allTables.value.map((t) => t.schema).filter(Boolean))]
      // 预选：default_schema 优先（须真实存在），否则第一个业务库
      const prefer = ds?.default_schema ?? ''
      bkForm.value.schema = schemaOptions.value.includes(prefer) ? prefer : (schemaOptions.value[0] ?? '')
      applySchemaTables()
    } catch { /* 预检不通过时允许留空 = 整库 */ }
  }
}

function applySchemaTables() {
  const s = bkForm.value.schema
  const rows = s ? allTables.value.filter((t) => t.schema === s) : allTables.value
  const seen = new Set<string>()
  tableOptions.value = rows
    .filter((t) => (seen.has(t.name) ? false : (seen.add(t.name), true)))
    .map((t) => ({ label: t.name, value: t.name }))
  // 已选表按新库对齐：剔除不属于当前库的表
  if (bkForm.value.tables) {
    const valid = new Set(tableOptions.value.map((o) => o.value))
    bkForm.value.tables = bkForm.value.tables.filter((n) => valid.has(n))
  }
}

async function submitBackup() {
  if (!bkForm.value.datasource_id || !bkForm.value.schema) {
    message.error('请选择数据源并填写库名')
    return
  }
  submitting.value = true
  try {
    const r = await backupApi.create({
      datasource_id: bkForm.value.datasource_id,
      schema: bkForm.value.schema,
      tables: bkForm.value.tables,
      structure_only: bkForm.value.structure_only,
    })
    message.success('备份导出完成（gzip + SHA-256 + 文件头环境声明）')
    drawerShow.value = false
    await load()
    if (r.backup) showHeader(r.backup)
  } catch (e) {
    message.error(errMsg(e))
  } finally {
    submitting.value = false
  }
}

// ---- 文件头详情 ----
const headerShow = ref(false)
const headerInfo = ref<{ file_name: string; sha256: string; header: string; replay_notes: string[] } | null>(null)

async function showHeader(row: BackupFile) {
  try {
    headerInfo.value = await backupApi.header(row.id)
    headerShow.value = true
  } catch (e) {
    message.error(errMsg(e))
  }
}

function copySha(sha: string) {
  navigator.clipboard?.writeText(sha)
  message.success('SHA-256 已复制')
}

function doDelete(row: BackupFile) {
  dialog.error({
    title: '删除备份',
    content: `确定删除「${row.file_name}」？文件将从磁盘移除，不可恢复。`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try { await backupApi.remove(row.id); await load() } catch (e) { message.error(errMsg(e)) }
    },
  })
}

// ---- 恢复三步向导（§4.2.5） ----
const restoreShow = ref(false)
const restoreStep = ref(1)
const restoreTarget = ref<BackupFile | null>(null)
const rs = ref({ target_datasource_id: null as number | null, target_db: '', safety_net: true, auto_pause_related: true, drop_extra_tables: true })
const precheck = ref<RestorePrecheck | null>(null)
const prechecking = ref(false)
const restoring = ref(false)

function openRestore(row: BackupFile) {
  restoreTarget.value = row
  restoreStep.value = 1
  precheck.value = null
  rs.value = { target_datasource_id: null, target_db: '', safety_net: true, auto_pause_related: true, drop_extra_tables: true }
  rsSchemaOptions.value = []
  restoreShow.value = true
  // 恢复向导可能先于手动备份抽屉打开：目标数据源列表需在此懒加载
  void ensureDsList()
}

const rsSchemaOptions = ref<string[]>([])
const rsLoading = ref(false)
const rsSchemaSelectOptions = computed(() => rsSchemaOptions.value.map((s) => ({ label: s, value: s })))

async function onRestoreDsChange(id: number) {
  rsSchemaOptions.value = []
  rs.value.target_db = ''
  if (!id) return
  rsLoading.value = true
  try {
    const r = await dsApi.tables(id)
    rsSchemaOptions.value = [...new Set((r.tables as Array<Record<string, unknown>>)
      .map((t) => String(t.schema ?? '')).filter(Boolean))]
  } catch { /* 目标源连接失败时允许手输库名 */ } finally {
    rsLoading.value = false
  }
  // 预填：目标源 default_schema 优先，其次备份源库名，否则第一个业务库
  const ds = dsList.value.find((d) => d.id === id)
  const prefer = ds?.default_schema ?? restoreTarget.value?.scope?.schema ?? ''
  rs.value.target_db = rsSchemaOptions.value.includes(prefer)
    ? prefer : (rsSchemaOptions.value[0] ?? prefer)
}

const precheckFails = () => (precheck.value?.items ?? []).filter((i) => i.level === 'fail')

async function runPrecheck() {
  if (!rs.value.target_datasource_id || !rs.value.target_db) {
    message.error('请选择目标数据源并填写目标库名')
    return
  }
  prechecking.value = true
  try {
    precheck.value = await backupApi.restorePrecheck(
      restoreTarget.value!.id, rs.value.target_datasource_id, rs.value.target_db)
  } catch (e) {
    message.error(errMsg(e))
  } finally {
    prechecking.value = false
  }
}

async function submitRestore() {
  restoring.value = true
  try {
    const r = await backupApi.restore(restoreTarget.value!.id, {
      target_datasource_id: rs.value.target_datasource_id,
      target_db: rs.value.target_db,
      safety_net: rs.value.safety_net,
      auto_pause_related: rs.value.auto_pause_related,
      drop_extra_tables: rs.value.drop_extra_tables,
    })
    message.success(`恢复已发起（运行 #${r.run_id}），完成后将按备份时的行数基线自动校验`)
    restoreShow.value = false
    router.push({ name: 'runs' })
  } catch (e) {
    message.error(errMsg(e))
  } finally {
    restoring.value = false
  }
}

// F5：覆盖恢复强确认——手动输入目标库名且严格一致方可执行（对齐清空重建范式）
function confirmRestore() {
  const targetDb = rs.value.target_db
  let name = ''
  dialog.error({
    title: '覆盖恢复 · 最终确认',
    content: () =>
      h('div', [
        h('p', { style: 'margin:0 0 8px' }, '恢复将同名对象先删后建、数据被备份内容覆盖，操作不可逆。'),
        h('p', { style: 'margin:0 0 8px; color: var(--Semantic-Color-error)' },
          `请手动输入目标库名「${targetDb}」以确认：`),
        h('input', {
          style: 'width:100%;padding:6px 10px;border:1px solid var(--Semantic-Color-border-strong);border-radius:6px;background:var(--Semantic-Color-bg-base);color:var(--Semantic-Color-text-primary)',
          placeholder: '目标库名',
          onInput: (ev: Event) => { name = (ev.target as HTMLInputElement).value },
        }),
      ]),
    positiveText: '确认执行恢复',
    negativeText: '取消',
    onPositiveClick: () => {
      if (name !== targetDb) {
        message.error('输入与目标库名不一致')
        return false // 阻止关闭，要求重新输入
      }
      submitRestore()
    },
  })
}

const LEVEL_TAG: Record<string, 'success' | 'warning' | 'error'> = { pass: 'success', warn: 'warning', fail: 'error' }

const columns: DataTableColumns<BackupFile> = [
  { title: '文件名', key: 'file_name', minWidth: 260, render: (r) => h('span', { class: 'mono', style: 'font-size:12px' }, r.file_name) },
  { title: '大小', key: 'size_bytes', width: 90, render: (r) => h('span', { class: 'mono' }, fmtSize(r.size_bytes)) },
  { title: '范围', key: 'scope', width: 180, render: (r) =>
    h('span', { style: 'font-size:12px' },
      `${r.scope?.schema ?? ''} · ${r.scope?.structure_only ? '仅结构' : (r.scope?.tables?.length ? `${r.scope.tables.length} 表` : '整库')}`) },
  { title: '方式', key: 'created_by', width: 80, render: (r) =>
    h(NTag, { size: 'small', bordered: false }, { default: () => r.created_by === 'manual' ? '手动' : r.created_by === 'auto' ? '自动' : r.created_by }) },
  { title: 'SHA-256', key: 'sha256', width: 150, render: (r) =>
    h('span', { style: 'display:inline-flex;align-items:center;gap:6px' }, [
      h('span', { class: 'mono', style: 'font-size:11px;color:var(--Semantic-Color-text-secondary)' }, r.sha256.slice(0, 12) + '…'),
      h('a', { style: 'cursor:pointer;font-size:12px', onClick: () => copySha(r.sha256) }, '复制'),
    ]) },
  { title: '时间', key: 'created_at', width: 160, render: (r) => h('span', { class: 'mono', style: 'font-size:12px' }, r.created_at) },
  { title: '操作', key: 'actions', width: 300, render: (r) =>
    h(NSpace, { size: 'small' }, {
      default: () => [
        h('a', { href: `/api/backups/${r.id}/download`, style: 'font-size:13px' }, '下载'),
        h('a', { style: 'cursor:pointer;font-size:13px', onClick: () => showHeader(r) }, '详情'),
        h('a', { style: 'cursor:pointer;font-size:13px;color:var(--Semantic-Color-accent)', onClick: () => openRestore(r) }, '恢复'),
        h('a', { style: 'cursor:pointer;font-size:13px;color:var(--Semantic-Color-error)', onClick: () => doDelete(r) }, '删除'),
      ],
    }) },
]
</script>

<template>
  <div style="padding: 24px; max-width: 1400px">
    <div style="display: flex; align-items: center; margin-bottom: 16px">
      <h2 style="margin: 0; font-size: var(--Font-Size-font-h2)">备份管理</h2>
      <n-tag size="small" :bordered="false" style="margin-left: 12px">保留最近 20 份滚动清理 · 磁盘余量低于阈值拒绝新备份</n-tag>
      <div style="flex: 1" />
      <n-button type="primary" @click="openDrawer">手动备份</n-button>
    </div>

    <n-card style="border-radius: var(--Radius-radius-lg)">
      <n-data-table :columns="columns" :data="backups" :loading="loading" :row-key="(r: BackupFile) => r.id" size="small" />
    </n-card>

    <!-- 手动备份抽屉 -->
    <n-drawer v-model:show="drawerShow" :width="480" placement="right">
      <n-drawer-content title="手动备份" closable>
        <n-form label-placement="left" label-width="100">
          <n-form-item label="数据源">
            <n-select v-model:value="bkForm.datasource_id" :options="dsOptions" placeholder="选择数据源" @update:value="onDsChange" />
          </n-form-item>
          <n-form-item label="库名">
            <n-select v-model:value="bkForm.schema" :options="schemaSelectOptions" filterable tag
              placeholder="选择库名（支持输入）" @update:value="applySchemaTables" />
          </n-form-item>
          <n-form-item label="范围">
            <n-select v-model:value="bkForm.tables" :options="tableOptions" multiple clearable
              placeholder="留空 = 整库备份" :max-tag-count="3" />
          </n-form-item>
          <n-form-item label="仅结构">
            <n-switch v-model:value="bkForm.structure_only" />
          </n-form-item>
        </n-form>
        <n-alert type="info" :bordered="false">
          产物为 .sql + gzip + SHA-256，文件头含会话环境声明（SET NAMES / sql_mode / time_zone /
          FOREIGN_KEY_CHECKS=0 / UNIQUE_CHECKS=0），可直接用 mysql 客户端回放。
        </n-alert>
        <template #footer>
          <n-button type="primary" :loading="submitting" @click="submitBackup">开始导出</n-button>
        </template>
      </n-drawer-content>
    </n-drawer>

    <!-- 文件头详情 -->
    <n-modal v-model:show="headerShow" preset="card" title="备份文件详情" style="width: 720px">
      <template v-if="headerInfo">
        <n-alert type="warning" :bordered="false" style="margin-bottom: 12px">
          <div v-for="(n, i) in headerInfo.replay_notes" :key="i">· {{ n }}</div>
        </n-alert>
        <p style="font-size: var(--Font-Size-font-caption); color: var(--Semantic-Color-text-secondary)">
          文件头环境声明（<span class="mono">{{ headerInfo.file_name }}</span>）：
        </p>
        <pre class="mono" style="background: var(--Semantic-Color-bg-base); border: 1px solid var(--Semantic-Color-border); border-radius: var(--Radius-radius-md); padding: 12px; font-size: 12px; overflow: auto">{{ headerInfo.header }}</pre>
      </template>
    </n-modal>

    <!-- 恢复三步向导 -->
    <n-modal v-model:show="restoreShow" preset="card" title="备份恢复向导" style="width: 760px">
      <n-steps :current="restoreStep" size="small" style="margin-bottom: 20px">
        <n-step title="确认备份文件" />
        <n-step title="选目标与预检" />
        <n-step title="确认执行" />
      </n-steps>

      <template v-if="restoreStep === 1 && restoreTarget">
        <n-descriptions label-placement="left" bordered :column="2" size="small">
          <n-descriptions-item label="文件"><span class="mono">{{ restoreTarget.file_name }}</span></n-descriptions-item>
          <n-descriptions-item label="生成时间"><span class="mono">{{ restoreTarget.created_at }}</span></n-descriptions-item>
          <n-descriptions-item label="源库版本"><span class="mono">{{ restoreTarget.scope?.source_version || '未知' }}</span></n-descriptions-item>
          <n-descriptions-item label="SHA-256"><span class="mono">{{ restoreTarget.sha256.slice(0, 24) }}…</span></n-descriptions-item>
        </n-descriptions>
        <p style="margin: 12px 0 6px; font-size: var(--Font-Size-font-caption)">各表行数基线（rows_json，恢复后校验基准）：</p>
        <div class="mono" style="font-size: 12px; max-height: 180px; overflow: auto; background: var(--Semantic-Color-bg-base); border: 1px solid var(--Semantic-Color-border); border-radius: 6px; padding: 10px">
          <div v-for="(v, k) in restoreTarget.rows_baseline ?? {}" :key="k">{{ k }} → rows={{ v.rows }} auto_increment={{ v.auto_increment ?? '—' }}</div>
        </div>
        <div style="display: flex; justify-content: flex-end; margin-top: 16px">
          <n-button type="primary" @click="restoreStep = 2">下一步：选目标</n-button>
        </div>
      </template>

      <template v-else-if="restoreStep === 2">
        <n-form label-placement="left" label-width="110">
          <n-form-item label="目标数据源">
            <n-select v-model:value="rs.target_datasource_id" :options="dsOptions"
              placeholder="选择目标 MySQL" @update:value="onRestoreDsChange" />
          </n-form-item>
          <n-form-item label="目标库">
            <n-select v-model:value="rs.target_db" :options="rsSchemaSelectOptions" filterable tag
              :loading="rsLoading" placeholder="选择或输入恢复到哪个库" />
          </n-form-item>
        </n-form>
        <n-button size="small" secondary type="primary" :loading="prechecking" @click="runPrecheck">执行预检</n-button>
        <template v-if="precheck">
          <div style="margin-top: 12px; display: flex; flex-direction: column; gap: 6px">
            <n-alert v-for="item in precheck.items" :key="item.key" :type="LEVEL_TAG[item.level]" :bordered="false">
              <b>{{ item.key }}</b>：{{ item.message }}
              <div v-if="item.hint" style="font-size: var(--Font-Size-font-caption); margin-top: 2px">{{ item.hint }}</div>
            </n-alert>
          </div>
          <n-alert v-if="precheck.affected_tasks.length" type="warning" style="margin-top: 10px">
            关联任务（目标端命中）：{{ precheck.affected_tasks.map((t) => `#${t.task_id} ${t.name}(${t.state})`).join('、') }}
          </n-alert>
        </template>
        <div style="display: flex; justify-content: space-between; margin-top: 16px">
          <n-button @click="restoreStep = 1">上一步</n-button>
          <n-button type="primary" :disabled="precheckFails().length > 0 || !precheck" @click="restoreStep = 3">
            下一步（预检失败项阻断）
          </n-button>
        </div>
      </template>

      <template v-else-if="restoreStep === 3 && restoreTarget">
        <n-alert type="warning" :bordered="false" style="margin-bottom: 12px">
          <b>完整恢复警示</b>：目标库将完整恢复为备份时刻状态，与备份现状无关——
          同名对象先删后建，数据被备份内容覆盖。
        </n-alert>
        <div style="display: flex; flex-direction: column; gap: 10px; margin-bottom: 12px">
          <n-checkbox :checked="rs.drop_extra_tables" @update:checked="(v: boolean) => rs.drop_extra_tables = v">
            清理目标库中备份之外的表，使目标库与备份完全一致（仅整库备份生效）
          </n-checkbox>
          <n-checkbox :checked="rs.safety_net" @update:checked="(v: boolean) => rs.safety_net = v">
            恢复前自动备份目标库涉及表（安全网，强烈建议开启）
          </n-checkbox>
          <n-checkbox :checked="rs.auto_pause_related" @update:checked="(v: boolean) => rs.auto_pause_related = v">
            恢复期间自动暂停关联任务、完成后自动恢复（失败时保持暂停并提示人工确认）
          </n-checkbox>
        </div>
        <n-alert type="info" :bordered="false">
          恢复按原语句顺序逐条回放（保证执行顺序），并受全局限流约束；中断后目标库会标记「恢复未完成」，当前版本不支持断点续传，需整体重跑。
        </n-alert>
        <div style="display: flex; justify-content: space-between; margin-top: 16px">
          <n-button @click="restoreStep = 2">上一步</n-button>
          <n-button type="error" :loading="restoring" @click="confirmRestore">
            确认执行恢复（需输入目标库名）
          </n-button>
        </div>
      </template>
    </n-modal>
  </div>
</template>
