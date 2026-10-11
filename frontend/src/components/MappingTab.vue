<script setup lang="ts">
/**
 * 任务详情 · 表映射页签（T4.6 / §6.2 页面 4「表映射」页签 / §4.2.4）：
 * 勾选表格 + 重命名 / 列过滤 / 行过滤编辑抽屉（条件构建器）；无主键红标与降级选择；
 * 触发器迁移开关及红色风险警示。映射口径：全量/增量/校验/备份共用同一份 mapping_json。
 */
import { computed, onMounted, ref } from 'vue'
import {
  NAlert, NButton, NCard, NCheckbox, NDrawer, NDrawerContent, NForm, NFormItem, NInput,
  NInputNumber, NRadioButton, NRadioGroup, NSelect, NSwitch, NTag, useDialog, useMessage,
} from 'naive-ui'
import {
  dsTableMeta, taskApi, type RowFilterCond, type SyncTask, type TableMeta,
} from '../api'
import { errMsg } from '../api/client'

const props = defineProps<{ taskId: number; task: SyncTask | null }>()
const emit = defineEmits<{ (e: 'saved'): void }>()
const message = useMessage()
const dialog = useDialog()

const busy = ref(false)
const loading = ref(false)
const tables = ref<TableMeta[]>([])
const schema = ref<string | null>(null)
const mapping = ref<NonNullable<SyncTask['mapping']>>({})
const migrateTriggers = ref(false)

const OP_OPTIONS = [
  { label: '= 等于', value: 'eq' }, { label: '≠ 不等于', value: 'ne' },
  { label: '> 大于', value: 'gt' }, { label: '≥ 大于等于', value: 'ge' },
  { label: '< 小于', value: 'lt' }, { label: '≤ 小于等于', value: 'le' },
  { label: 'IN 包含', value: 'in' },
]

onMounted(async () => {
  const t = props.task
  if (!t) return
  mapping.value = JSON.parse(JSON.stringify(t.mapping ?? {}))
  migrateTriggers.value = t.migrate_triggers
  if (!mapping.value.column_filter) mapping.value.column_filter = {}
  if (!mapping.value.row_filters) mapping.value.row_filters = {}
  if (!mapping.value.pk_mode) mapping.value.pk_mode = {}
  if (!mapping.value.rename) mapping.value.rename = {}
  loading.value = true
  try {
    const meta = await dsTableMeta(t.source_id, mapping.value.source_database)
    tables.value = meta.tables
    schema.value = meta.schema
  } catch (e) {
    message.warning(`源库表元数据获取失败：${errMsg(e)}（可先编辑其他项）`)
  } finally {
    loading.value = false
  }
})

const subsetTables = computed(() => Object.keys(mapping.value.row_filters ?? {}))

async function save() {
  busy.value = true
  try {
    const body: Record<string, unknown> = {
      mapping: { ...mapping.value, source_database: mapping.value.source_database ?? schema.value },
      migrate_triggers: migrateTriggers.value,
    }
    await taskApi.update(props.taskId, body)
    message.success('表映射已保存（全量 / 增量 / 校验 / 备份共用同一份映射配置）')
    emit('saved')
  } catch (e) {
    message.error(errMsg(e))
  } finally {
    busy.value = false
  }
}

// ---- 表勾选与改名 ----
const selectedSet = computed(() => new Set(mapping.value.tables ?? []))

function toggleTable(name: string, checked: boolean) {
  const arr = new Set(mapping.value.tables ?? [])
  if (checked) arr.add(name)
  else arr.delete(name)
  mapping.value.tables = [...arr]
  mapping.value.scope = 'selected'
}

// ---- 编辑抽屉 ----
const showDrawer = ref(false)
const editTable = ref<TableMeta | null>(null)
const editRename = ref('')
const editExcluded = ref<string[]>([])
const editConds = ref<RowFilterCond[]>([])
const editCombinator = ref<'and' | 'or'>('and')
const editPkMode = ref<'cdc' | 'degraded'>('cdc')

const columnOptions = computed(() =>
  (editTable.value?.columns ?? []).map((c) => ({
    label: `${c.name}${c.column_key === 'PRI' ? '（主键）' : c.column_key === 'UNI' ? '（唯一键）' : ''}${!c.nullable && c.column_default == null ? '（NOT NULL 无默认）' : ''}`,
    value: c.name,
    disabled: c.column_key === 'PRI' || c.column_key === 'UNI'
      || (!c.nullable && c.column_default == null),
  })))

const condColOptions = computed(() =>
  (editTable.value?.columns ?? []).map((c) => ({ label: c.name, value: c.name })))

function openEdit(t: TableMeta) {
  editTable.value = t
  editRename.value = mapping.value.rename?.[t.name] ?? ''
  editExcluded.value = mapping.value.column_filter?.[t.name] ?? []
  editPkMode.value = mapping.value.pk_mode?.[t.name] ?? 'cdc'
  const pred = mapping.value.row_filters?.[t.name]
  const conds = parsePred(pred)
  editConds.value = conds.list
  editCombinator.value = conds.combinator
  showDrawer.value = true
}

function parsePred(pred: Record<string, unknown> | undefined):
  { list: RowFilterCond[]; combinator: 'and' | 'or' } {
  if (!pred) return { list: [], combinator: 'and' }
  if ('and' in pred || 'or' in pred) {
    const key = 'and' in pred ? 'and' : 'or'
    const list = (pred[key] as Record<string, unknown>[]).map(simplifyCond)
    return { list, combinator: key as 'and' | 'or' }
  }
  return { list: [simplifyCond(pred)], combinator: 'and' }
}

function simplifyCond(p: Record<string, unknown>): RowFilterCond {
  return { col: String(p.col ?? ''), op: String(p.op ?? 'eq'),
    value: p.value as RowFilterCond['value'],
    values: Array.isArray(p.values) ? (p.values as unknown[]) : undefined }
}

function addCond() {
  editConds.value.push({ col: editTable.value?.columns?.[0]?.name ?? '', op: 'eq', value: '' })
}

function removeCond(i: number) {
  editConds.value.splice(i, 1)
}

function condValueModel(cond: RowFilterCond) {
  return computed({
    get: () => cond.op === 'in'
      ? (cond.values ?? []).join(',')
      : String(cond.value ?? ''),
    set: (v: string) => {
      if (cond.op === 'in') {
        cond.values = v.split(',').map((s) => coerce(s.trim())).filter((x) => x !== '')
      } else {
        cond.value = coerce(v)
      }
    },
  })
}

function coerce(s: string): unknown {
  if (s === 'null') return null
  if (s !== '' && !Number.isNaN(Number(s))) return Number(s)
  return s
}

function buildPred(): Record<string, unknown> | null {
  const conds = editConds.value.filter((c) => c.col)
  if (!conds.length) return null
  const plain = conds.map((c) => c.op === 'in'
    ? { col: c.col, op: 'in', values: c.values ?? [] }
    : { col: c.col, op: c.op, value: c.value })
  return plain.length === 1 ? plain[0] : { [editCombinator.value]: plain }
}

function applyEdit() {
  const name = editTable.value!.name
  if (editRename.value && editRename.value !== name) mapping.value.rename![name] = editRename.value
  else delete mapping.value.rename![name]
  if (editExcluded.value.length) mapping.value.column_filter![name] = [...editExcluded.value]
  else delete mapping.value.column_filter![name]
  const pred = buildPred()
  if (pred) mapping.value.row_filters![name] = pred
  else delete mapping.value.row_filters![name]
  if (!editTable.value!.has_pk) {
    if (editPkMode.value === 'degraded') mapping.value.pk_mode![name] = 'degraded'
    else delete mapping.value.pk_mode![name]
  }
  showDrawer.value = false
}

// ---- 触发器迁移开关（危险操作确认规范 §6.1） ----
function onTriggerSwitch(v: boolean) {
  if (!v) {
    migrateTriggers.value = false
    return
  }
  dialog.error({
    title: '开启触发器迁移（危险）',
    content: '目标库将补建源库触发器：同步写入可能再次触发目标端触发器，导致变更重复执行或业务逻辑重复触达。'
      + '请逐表确认目标端触发器副作用。确认开启？',
    positiveText: '我已了解风险，开启', negativeText: '取消',
    onPositiveClick: () => { migrateTriggers.value = true },
    onNegativeClick: () => { migrateTriggers.value = false },
  })
}

const pkLabel = (t: TableMeta) => (t.has_pk ? t.primary_key.join('+') : '无主键')
</script>

<template>
  <n-card title="表映射（全量 / 增量 / 校验 / 备份共用同一份映射配置）"
    style="border-radius: var(--Radius-radius-lg)">
    <template #header-extra>
      <n-button size="small" type="primary" :loading="busy" @click="save">保存映射</n-button>
    </template>

    <n-alert v-if="subsetTables.length" type="warning" :bordered="false" style="margin-bottom: 12px">
      <b>子集同步模式</b>：以下表启用了行过滤 —— {{ subsetTables.join('、') }}。
      目标库仅保留满足条件的行；孤儿行清理对这些表自动禁用；行退出过滤范围时将自动转为目标端 DELETE。
    </n-alert>

    <div style="display: flex; gap: 16px; align-items: center; margin-bottom: 12px; flex-wrap: wrap">
      <n-radio-group :value="mapping.scope ?? 'all'" size="small"
        @update:value="(v: string) => (mapping.scope = v as 'all' | 'selected')">
        <n-radio-button value="all">整库同步</n-radio-button>
        <n-radio-button value="selected">勾选表</n-radio-button>
      </n-radio-group>
      <span class="hint">源库：<span class="mono">{{ schema ?? '—' }}</span></span>
      <n-form-item label="目标库" label-placement="left" size="small" style="margin: 0">
        <n-input :value="mapping.target_database ?? ''" size="small" style="width: 150px" class="mono"
          placeholder="留空 = 目标源默认库"
          @update:value="(v: string) => (mapping.target_database = v || undefined)" />
      </n-form-item>
      <div style="flex: 1" />
      <n-form-item label="目标表名前缀" label-placement="left" size="small" style="margin: 0">
        <n-input :value="mapping.prefix ?? ''" size="small" style="width: 120px" class="mono"
          placeholder="如 bak_" @update:value="(v: string) => (mapping.prefix = v)" />
      </n-form-item>
      <n-form-item label="后缀" label-placement="left" size="small" style="margin: 0">
        <n-input :value="mapping.suffix ?? ''" size="small" style="width: 120px" class="mono"
          @update:value="(v: string) => (mapping.suffix = v)" />
      </n-form-item>
    </div>

    <div class="tbl-list">
      <div v-for="t in tables" :key="t.name" class="tbl-row">
        <n-checkbox v-if="(mapping.scope ?? 'all') === 'selected'"
          :checked="selectedSet.has(t.name)" size="small"
          @update:checked="(v: boolean) => toggleTable(t.name, v)" />
        <div style="min-width: 0; flex: 1">
          <div style="display: flex; align-items: center; gap: 8px">
            <span class="mono tbl-name">{{ t.name }}</span>
            <n-tag v-if="!t.has_pk" size="small" type="error" :bordered="false">无主键</n-tag>
            <n-tag v-else size="small" :bordered="false" class="mono">{{ pkLabel(t) }}</n-tag>
            <n-tag v-if="mapping.row_filters?.[t.name]" size="small" type="warning" :bordered="false">
              行过滤</n-tag>
            <n-tag v-if="mapping.column_filter?.[t.name]?.length" size="small" :bordered="false">
              列过滤 ×{{ mapping.column_filter[t.name].length }}</n-tag>
            <n-tag v-if="mapping.pk_mode?.[t.name] === 'degraded'" size="small" type="warning"
              :bordered="false">降级比对</n-tag>
            <n-tag v-if="mapping.rename?.[t.name]" size="small" :bordered="false" class="mono">
              → {{ mapping.rename[t.name] }}</n-tag>
          </div>
          <div class="stat-sub" style="margin-top: 2px">
            约 {{ t.rows_est.toLocaleString() }} 行 · {{ t.columns.length }} 列
          </div>
        </div>
        <n-button size="tiny" quaternary @click="openEdit(t)">编辑</n-button>
      </div>
      <div v-if="!tables.length && !loading" class="stat-sub" style="padding: 16px">
        源库元数据未加载（需源库在线）。
      </div>
    </div>

    <n-alert type="error" :bordered="false" style="margin-top: 12px">
      <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap">
        <n-switch :value="migrateTriggers" size="small" @update:value="onTriggerSwitch" />
        <div>
          <b>迁移触发器</b>
          <div class="stat-sub">
            开启后目标库补建源库触发器 —— 同步写入可能再次触发，造成变更重复执行（需逐表确认副作用）。
            默认关闭：目标库作为只读副本不建触发器。
          </div>
        </div>
      </div>
    </n-alert>

    <n-drawer v-model:show="showDrawer" :width="560">
      <n-drawer-content :title="`表映射编辑：${editTable?.name ?? ''}`" closable>
        <n-form label-placement="top" size="small">
          <n-form-item label="目标表重命名（留空 = 同名）">
            <n-input v-model:value="editRename" class="mono" placeholder="与源表同名"
              :disabled="!!(mapping.prefix || mapping.suffix)" />
            <div v-if="mapping.prefix || mapping.suffix" class="stat-sub" style="margin-top: 4px">
              已设置前后缀 {{ mapping.prefix }}*{{ mapping.suffix }}，改名不可用
            </div>
          </n-form-item>

          <n-form-item label="列过滤（排除列；主键/唯一键/NOT NULL 无默认值列不可排除）">
            <n-select v-model:value="editExcluded" :options="columnOptions" multiple clearable
              placeholder="选择需排除的列（如大字段/敏感列）" max-tag-count="responsive" />
          </n-form-item>

          <n-form-item v-if="editTable && !editTable.has_pk" label="无主键表同步方式">
            <n-radio-group v-model:value="editPkMode">
              <n-radio-button value="cdc">补主键走 CDC（需先在源库补建主键）</n-radio-button>
              <n-radio-button value="degraded">降级：定时 checksum 比对 + 差异覆盖</n-radio-button>
            </n-radio-group>
            <n-alert v-if="editPkMode === 'degraded'" type="warning" :bordered="false"
              style="margin-top: 8px; width: 100%">
              降级模式无法捕获物理删除，且仅能整表级定位差异（无行级主键可依）。
            </n-alert>
          </n-form-item>

          <n-form-item label="行过滤条件（仅支持 列+比较/IN+字面量 的 AND/OR 组合）">
            <div style="width: 100%">
              <n-radio-group v-if="editConds.length > 1" v-model:value="editCombinator" size="small"
                style="margin-bottom: 8px">
                <n-radio-button value="and">AND（同时满足）</n-radio-button>
                <n-radio-button value="or">OR（任一满足）</n-radio-button>
              </n-radio-group>
              <div v-for="(c, i) in editConds" :key="i"
                style="display: flex; gap: 6px; margin-bottom: 6px; align-items: center">
                <n-select v-model:value="c.col" :options="condColOptions" size="small"
                  style="width: 150px" />
                <n-select v-model:value="c.op" :options="OP_OPTIONS" size="small"
                  style="width: 140px" />
                <n-input v-if="c.op !== 'in'" size="small" class="mono"
                  placeholder="字面量（null / 数字 / 文本）"
                  :model-value="(c.value ?? '') as string"
                  @update:value="(v: string) => (c.value = v === 'null' ? null : v)" />
                <n-input v-else size="small" class="mono" placeholder="逗号分隔多个值"
                  :model-value="(c.values ?? []).join(',')"
                  @update:value="(v: string) => (c.values = v.split(',').map((s) => s.trim()))" />
                <n-button size="tiny" quaternary type="error" @click="removeCond(i)">删</n-button>
              </div>
              <n-button size="tiny" dashed @click="addCond">+ 添加条件</n-button>
            </div>
          </n-form-item>
        </n-form>
        <template #footer>
          <n-button type="primary" @click="applyEdit">应用（保存映射后生效）</n-button>
        </template>
      </n-drawer-content>
    </n-drawer>
  </n-card>
</template>

<style scoped>
.tbl-list {
  border: 1px solid var(--Semantic-Color-border);
  border-radius: var(--Radius-radius-lg);
  max-height: 380px;
  overflow: auto;
}
.tbl-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--Semantic-Color-border);
}
.tbl-row:last-child {
  border-bottom: none;
}
.tbl-name {
  font-size: var(--Font-Size-font-body);
  color: var(--Semantic-Color-text-primary);
}
.stat-sub {
  font-size: var(--Font-Size-font-caption);
  color: var(--Semantic-Color-text-tertiary);
}
.hint {
  font-size: var(--Font-Size-font-caption);
  color: var(--Semantic-Color-text-tertiary);
}
.mono {
  font-family: 'JetBrains Mono', Consolas, monospace;
}
</style>
