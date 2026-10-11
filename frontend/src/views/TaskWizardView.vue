<script setup lang="ts">
/** 任务创建向导（T2.7 / §6.2 页面 5；M6 多库批量）：
 * ① 数据源（内嵌预检）→ ② 库选择与映射（源库多选，目标库自动/手动对应 + 探测）
 * → ③ 模式与参数（自动初始化 / 并发 / 低性能建议）→ ④ 确认与批量创建结果 */
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import {
  NAlert, NButton, NCard, NCheckbox, NCheckboxGroup, NDescriptions, NDescriptionsItem, NForm,
  NFormItem, NInput, NInputNumber, NRadioGroup, NRadioButton, NSelect, NSteps, NStep,
  NSpin, NSwitch, NTag, useMessage,
} from 'naive-ui'
import {
  dsApi, dsTableMeta, taskApi, type BatchCreateResult, type Datasource, type PrecheckItem,
  type ProbeTargetItem, type TargetDbState,
} from '../api'
import { errMsg } from '../api/client'
import PrecheckPanel from '../components/PrecheckPanel.vue'

const router = useRouter()
const message = useMessage()

const step = ref(1)
const dsList = ref<Datasource[]>([])
const sourceId = ref<number | null>(null)
const targetId = ref<number | null>(null)

const form = ref({
  name: '',
  mapping: { scope: 'all' as 'all' | 'selected', tables: [] as string[], rename: {} as Record<string, string> },
  mode: 'init_plus_cdc' as 'backup_only' | 'init_plus_cdc' | 'scheduled_full',
  batch_size: 5000,
  rate_limit_rows: 0,
  safety_net_backup: true,
  orphan_cleanup: true,
  migrate_triggers: false,
  // M6：多库批量参数
  auto_start_init: true,
  max_parallel: 2,
})

// ---- 步骤 1：数据源 + 内嵌预检 ----
const srcPre = ref<{ overall: string; items: PrecheckItem[] } | null>(null)
const tgtPre = ref<{ overall: string; items: PrecheckItem[] } | null>(null)
const preloading = ref(false)

async function loadDs() {
  if (dsList.value.length) return
  try {
    dsList.value = await dsApi.list()
  } catch (e) {
    message.error(errMsg(e))
  }
}
loadDs()

async function precheck(role: 'source' | 'target') {
  const id = role === 'source' ? sourceId.value : targetId.value
  if (!id) return
  preloading.value = true
  try {
    const r = await dsApi.precheck(id, role)
    if (role === 'source') srcPre.value = { overall: r.overall, items: r.items }
    else tgtPre.value = { overall: r.overall, items: r.items }
  } catch (e) {
    message.error(errMsg(e))
  } finally {
    preloading.value = false
  }
}

function dsOk(role: 'source' | 'target'): boolean {
  const pre = role === 'source' ? srcPre.value : tgtPre.value
  if (!pre) return false
  return !pre.items.some((i) => i.level === 'fail')
}

// ---- 步骤 2：库选择与映射 ----
interface TableMeta { name: string; rows_est?: number }
const srcTables = ref<TableMeta[]>([])
const srcSchema = ref<string | null>(null)
const tablesLoading = ref(false)

// M6：源库多选（单选 = 保留完整映射 UI；多选 = 简化为整库同名 + 前后缀）
const srcDbs = ref<string[]>([])
const multiDb = computed(() => srcDbs.value.length > 1)
const srcDbOptions = ref<string[]>([])
const srcDbLoading = ref(false)

async function loadSchemas() {
  if (!sourceId.value) return
  srcDbLoading.value = true
  try {
    const r = await dsApi.schemas(sourceId.value)
    const list = [...r.schemas]
    const ds = dsList.value.find((d) => d.id === sourceId.value)
    const fallback = ds?.default_schema ?? null
    if (fallback && !list.includes(fallback)) list.unshift(fallback)
    srcDbOptions.value = list
  } catch (e) {
    message.error(`源库清单加载失败：${errMsg(e)}`)
  } finally {
    srcDbLoading.value = false
  }
}

function onSrcDbsChange(vals: string[]) {
  form.value.mapping.tables = []
  if (vals.length === 1) {
    void loadTables(vals[0])
  } else {
    srcTables.value = []
    srcSchema.value = null
  }
  scheduleProbe()
}

function selectAllSrcDbs() {
  srcDbs.value = [...srcDbOptions.value]
  onSrcDbsChange(srcDbs.value)
}

function clearSrcDbs() {
  srcDbs.value = []
  onSrcDbsChange([])
}

// 目标库对应：自动同名（默认）/ 手动逐个指定
const targetMode = ref<'auto' | 'manual'>('auto')
const manualTargets = ref<Record<string, string>>({})
const prefixText = ref('')
const suffixText = ref('')

function onManualTarget(src: string, val: string | null) {
  manualTargets.value[src] = (val ?? '').trim()
  scheduleProbe()
}

interface PairRow { source: string; target: string }
const pairs = computed<PairRow[]>(() =>
  srcDbs.value.map((s) => ({
    source: s,
    target: (targetMode.value === 'auto' ? s : (manualTargets.value[s] || '')).trim(),
  })))

const dupTargets = computed(() => {
  const cnt: Record<string, number> = {}
  for (const p of pairs.value) if (p.target) cnt[p.target] = (cnt[p.target] || 0) + 1
  return Object.keys(cnt).filter((k) => cnt[k] > 1)
})

// ---- 目标库存在性探测（后端单连接聚合查询，低性能库友好） ----
const probeResults = ref<Record<string, ProbeTargetItem>>({})
const probeLoading = ref(false)
let probeTimer: number | null = null

function scheduleProbe() {
  if (probeTimer) window.clearTimeout(probeTimer)
  probeTimer = window.setTimeout(() => void runProbe(), 500)
}

async function runProbe() {
  if (!targetId.value) return
  const names = [...new Set(pairs.value.map((p) => p.target).filter((t) => !!t))]
  if (!names.length) {
    probeResults.value = {}
    return
  }
  probeLoading.value = true
  try {
    const r = await taskApi.probeBatchTargets(targetId.value, names)
    const map: Record<string, ProbeTargetItem> = {}
    for (const it of r.items) map[it.target_database] = it
    probeResults.value = map
  } catch (e) {
    message.error(`目标库探测失败：${errMsg(e)}`)
  } finally {
    probeLoading.value = false
  }
}

watch(() => pairs.value.map((p) => p.target).join('\n'), () => scheduleProbe())

interface PairRowView extends PairRow { pill: { text: string; color: string } | null; dup: boolean }
const pairRows = computed<PairRowView[]>(() =>
  pairs.value.map((p) => ({
    ...p,
    pill: probePill(p.target),
    dup: !!p.target && dupTargets.value.includes(p.target),
  })))

function probePill(target: string): { text: string; color: string } | null {
  if (!target) return null
  const it = probeResults.value[target]
  if (!it) return null
  if (it.state === 'missing') return { text: '不存在 · 将新建', color: 'var(--Semantic-Color-success)' }
  if (it.state === 'empty') return { text: '存在 · 空', color: 'var(--Semantic-Color-info)' }
  return { text: `存在 · 非空（${it.tables} 表）`, color: 'var(--Semantic-Color-warning)' }
}

const autoInitCount = computed(() => pairRows.value.filter(
  (p) => !p.pill || p.pill.text.includes('将新建') || p.pill.text === '存在 · 空').length)

// 单库/多库共用：目标库手动模式下拉选项（tag 可手输不存在的库名）
const tgtDbOptions = ref<string[]>([])

async function loadTgtSchemas() {
  if (!targetId.value) return
  try {
    const r = await dsApi.schemas(targetId.value)
    const list = [...r.schemas]
    const ds = dsList.value.find((d) => d.id === targetId.value)
    const fallback = ds?.default_schema ?? null
    if (fallback && !list.includes(fallback)) list.unshift(fallback)
    tgtDbOptions.value = list
  } catch (e) {
    message.error(`目标库清单加载失败：${errMsg(e)}`)
  }
}

function onTargetChange() {
  tgtPre.value = null
  tgtDbOptions.value = []
  probeResults.value = {}
  void precheck('target')
}

async function loadTables(schemaOverride?: string) {
  if (!sourceId.value) return
  tablesLoading.value = true
  try {
    const ds = dsList.value.find((d) => d.id === sourceId.value)
    const schema = schemaOverride ?? srcDbs.value[0] ?? ds?.default_schema ?? undefined
    const r = await dsTableMeta(sourceId.value, schema)
    const seen = new Set<string>()
    srcTables.value = r.tables
      .filter((t) => (seen.has(t.name) ? false : (seen.add(t.name), true)))
      .map((t) => ({ name: t.name, rows_est: t.rows_est }))
    srcSchema.value = r.schema ?? schema ?? null
    const valid = new Set(srcTables.value.map((t) => t.name))
    form.value.mapping.tables = form.value.mapping.tables.filter((n) => valid.has(n))
    if (!srcTables.value.length) {
      message.warning(`源库「${srcSchema.value ?? '未知'}」未发现基础表（BASE TABLE），请确认库名与账号权限`)
    }
  } catch (e) {
    message.error(`源库表清单加载失败：${errMsg(e)}`)
  } finally {
    tablesLoading.value = false
  }
}

function onSourceChange() {
  srcPre.value = null
  srcTables.value = []
  srcSchema.value = null
  srcDbs.value = []
  srcDbOptions.value = []
  manualTargets.value = {}
  probeResults.value = {}
  form.value.mapping.tables = []
  void precheck('source')
}

function setAllTables(all: boolean) {
  form.value.mapping.tables = all ? srcTables.value.map((t) => t.name) : []
}

const renameText = ref('')
function parseRename(): Record<string, string> {
  const out: Record<string, string> = {}
  for (const line of renameText.value.split('\n')) {
    const [src, dst] = line.split(/[:=，,]/).map((s) => s.trim())
    if (src && dst) out[src] = dst
  }
  return out
}

// ---- 步骤 3 / 4：模式、汇总、提交 ----
const MODE_OPTIONS = [
  { value: 'backup_only', label: '仅备份（导出 .sql，不做数据同步）' },
  { value: 'init_plus_cdc', label: '初始化 + 持续增量（推荐）' },
  { value: 'scheduled_full', label: '仅定时全量（保存即就绪，触发时按目标库状态路由）' },
]

const step1Valid = computed(() => !!(sourceId.value && targetId.value && sourceId.value !== targetId.value
  && dsOk('source') && dsOk('target')))

const step2Valid = computed(() => {
  if (!form.value.name.trim() || !pairs.value.length) return false
  if (multiDb.value) {
    return pairs.value.every((p) => !!p.target) && dupTargets.value.length === 0
  }
  return form.value.mapping.scope === 'all' || form.value.mapping.tables.length > 0
})

const submitting = ref(false)
const batchResult = ref<BatchCreateResult | null>(null)

function buildMappingBase(): Record<string, unknown> {
  const base: Record<string, unknown> = { scope: 'all', tables: [], rename: {} }
  if (prefixText.value.trim()) base.prefix = prefixText.value.trim()
  if (suffixText.value.trim()) base.suffix = suffixText.value.trim()
  return base
}

async function submit() {
  if (multiDb.value) return submitBatch()
  return submitSingle()
}

async function submitSingle() {
  // 单库同样走「目标库对应」机制：目标库 = 自动同名（缺省）/ 手动指定，恒有值；
  // 目标库不存在时由初始化自动建库（引擎已支持）
  if (!pairs.value.length) {
    message.error('请先选择源库')
    return
  }
  const mapping: Record<string, unknown> = {
    scope: form.value.mapping.scope,
    tables: form.value.mapping.tables,
    rename: parseRename(),
    source_database: pairs.value[0].source,
    target_database: pairs.value[0].target,
  }
  submitting.value = true
  try {
    const t = await taskApi.create({
      name: form.value.name,
      source_id: sourceId.value,
      target_id: targetId.value,
      mode: form.value.mode,
      mapping,
      batch_size: form.value.batch_size,
      rate_limit_rows: form.value.rate_limit_rows,
      safety_net_backup: form.value.safety_net_backup,
      orphan_cleanup: form.value.orphan_cleanup,
      migrate_triggers: form.value.migrate_triggers,
    })
    message.success(`任务「${t.name}」已创建（${t.mode === 'init_plus_cdc' ? '草稿，需发起初始化' : '已就绪'}）`)
    router.push({ name: 'tasks' })
  } catch (e) {
    message.error(errMsg(e))
  } finally {
    submitting.value = false
  }
}

async function submitBatch() {
  submitting.value = true
  try {
    const r = await taskApi.batchCreate({
      name: form.value.name,
      source_id: sourceId.value,
      target_id: targetId.value,
      mode: form.value.mode,
      db_pairs: pairs.value.map((p) => ({ source_database: p.source, target_database: p.target })),
      mapping_base: buildMappingBase(),
      auto_start_init: form.value.auto_start_init && form.value.mode === 'init_plus_cdc',
      max_parallel: form.value.max_parallel,
      batch_size: form.value.batch_size,
      rate_limit_rows: form.value.rate_limit_rows,
      safety_net_backup: form.value.safety_net_backup,
      orphan_cleanup: form.value.orphan_cleanup,
      migrate_triggers: form.value.migrate_triggers,
    })
    batchResult.value = r
    const n = r.created.length
    const f = r.failed.length
    message.success(`批量创建完成：成功 ${n} 个${f ? `，失败 ${f} 个` : ''}`
      + (r.init_enqueued.length ? `，已排队初始化 ${r.init_enqueued.length} 个` : ''))
  } catch (e) {
    message.error(errMsg(e))
  } finally {
    submitting.value = false
  }
}

function stateLabel(s: TargetDbState): string {
  if (s === 'missing') return '不存在（将新建）'
  if (s === 'empty') return '存在 · 空'
  return '存在 · 非空'
}

const dsOptions = computed(() =>
  dsList.value.map((d) => ({ label: `${d.name}（${d.host}:${d.port}）`, value: d.id })))
</script>

<template>
  <div style="padding: 24px; max-width: 900px; margin: 0 auto">
    <h2 style="font-size: var(--Font-Size-font-h2); margin: 0 0 16px">新建同步任务</h2>
    <n-card style="border-radius: var(--Radius-radius-lg)">
      <n-steps :current="step" size="small" style="margin-bottom: 24px">
        <n-step title="数据源" description="源 / 目标 + 预检" />
        <n-step title="库与映射" description="源库多选 / 目标库对应" />
        <n-step title="模式" description="备份 / 初始化+增量 / 定时全量" />
        <n-step title="参数与确认" description="确认信息并创建任务" />
      </n-steps>

      <!-- 步骤 1 -->
      <div v-if="step === 1">
        <n-form label-placement="left" label-width="110">
          <n-form-item label="源数据源">
            <n-select v-model:value="sourceId" :options="dsOptions" placeholder="选择源 MySQL"
              @update:value="onSourceChange" />
          </n-form-item>
          <n-form-item label="目标数据源">
            <n-select v-model:value="targetId" :options="dsOptions" placeholder="选择目标 MySQL"
              @update:value="onTargetChange" />
          </n-form-item>
        </n-form>
        <n-alert v-if="sourceId && targetId && sourceId === targetId" type="error" style="margin-top: 8px">
          源与目标不能为同一数据源
        </n-alert>
        <n-spin v-if="preloading" size="small" style="margin: 16px 0" />
        <template v-if="srcPre">
          <n-tag :type="dsOk('source') ? 'success' : 'error'" size="small" style="margin: 12px 0 4px">源库预检</n-tag>
          <PrecheckPanel :items="srcPre.items" :overall="srcPre.overall" />
        </template>
        <template v-if="tgtPre">
          <n-tag :type="dsOk('target') ? 'success' : 'error'" size="small" style="margin: 12px 0 4px">目标库预检</n-tag>
          <PrecheckPanel :items="tgtPre.items" :overall="tgtPre.overall" />
        </template>
      </div>

      <!-- 步骤 2 -->
      <div v-else-if="step === 2">
        <n-form label-placement="left" label-width="110">
          <n-form-item label="任务名称">
            <n-input v-model:value="form.name" placeholder="如：订单库 → 容灾库（多库时作为任务名前缀）" maxlength="128" />
          </n-form-item>
          <n-form-item label="源库">
            <div style="width: 100%">
              <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 6px">
                <n-select v-model:value="srcDbs"
                  :options="srcDbOptions.map((s) => ({ label: s, value: s }))"
                  :loading="srcDbLoading" multiple filterable clearable
                  placeholder="选择源库（可多选 / 全选；多选将按每库一个任务批量创建）"
                  @update:value="onSrcDbsChange" />
                <n-button size="small" :disabled="!srcDbOptions.length" @click="selectAllSrcDbs">全选</n-button>
                <n-button size="small" :disabled="!srcDbs.length" @click="clearSrcDbs">清空</n-button>
              </div>
              <span v-if="srcDbs.length > 1"
                style="font-size: var(--Font-Size-font-caption); color: var(--Semantic-Color-text-tertiary)">
                已选 {{ srcDbs.length }} 个源库，将创建 {{ srcDbs.length }} 个独立任务（每库一个，便于独立监控延迟与状态）
              </span>
            </div>
          </n-form-item>

          <!-- 目标库对应（单库/多库共用）：自动同名（默认）/ 手动指定 + 存在性探测 -->
          <n-form-item label="目标库对应">
            <div style="width: 100%">
              <n-radio-group v-model:value="targetMode" size="small">
                <n-radio-button value="auto">自动同名对应</n-radio-button>
                <n-radio-button value="manual">手动指定</n-radio-button>
              </n-radio-group>
              <n-spin v-if="probeLoading" size="small" style="margin-top: 8px" />
              <div style="display: flex; flex-direction: column; gap: 6px; margin-top: 8px">
                <div v-for="p in pairRows" :key="p.source"
                  style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap">
                  <span class="mono" style="min-width: 150px">{{ p.source }}</span>
                  <span style="color: var(--Semantic-Color-text-tertiary)">→</span>
                  <span v-if="targetMode === 'auto'" class="mono" style="min-width: 150px">{{ p.target }}</span>
                  <n-select v-else :value="manualTargets[p.source] || null"
                    :options="tgtDbOptions.map((s) => ({ label: s, value: s }))"
                    filterable tag clearable style="width: 200px"
                    placeholder="输入或选择目标库名"
                    @update:value="(v) => onManualTarget(p.source, v)" />
                  <span v-if="p.pill"
                    :style="{ color: p.pill.color, fontSize: 'var(--Font-Size-font-caption)' }">
                    ● {{ p.pill.text }}
                  </span>
                  <span v-if="p.dup" style="color: var(--Semantic-Color-error); font-size: var(--Font-Size-font-caption)">
                    目标库重复（多源不能写同一库）
                  </span>
                </div>
              </div>
            </div>
          </n-form-item>

          <!-- 多库：表名前后缀 -->
          <n-form-item v-if="multiDb" label="表名前后缀">
            <div style="display: flex; gap: 8px; align-items: center">
              <n-input v-model:value="prefixText" placeholder="前缀（可选）" style="width: 160px" />
              <n-input v-model:value="suffixText" placeholder="后缀（可选）" style="width: 160px" />
              <span style="color: var(--Semantic-Color-text-tertiary); font-size: var(--Font-Size-font-caption)">
                应用于所有库的表名（如前缀 bak_ → bak_表名）
              </span>
            </div>
          </n-form-item>

          <!-- 单库：完整映射 UI（范围 / 勾选表 / 重命名） -->
          <template v-else>
            <n-form-item label="映射范围">
              <n-radio-group v-model:value="form.mapping.scope">
                <n-radio-button value="all">同名整库映射</n-radio-button>
                <n-radio-button value="selected">勾选表</n-radio-button>
              </n-radio-group>
            </n-form-item>
            <n-form-item v-if="form.mapping.scope === 'selected'" label="选择表">
              <div style="width: 100%">
                <n-spin v-if="tablesLoading" size="small" />
                <template v-else-if="srcTables.length">
                  <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 8px">
                    <span style="font-size: var(--Font-Size-font-caption); color: var(--Semantic-Color-text-tertiary)">
                      源库 <span class="mono">{{ srcSchema ?? '—' }}</span> · 共 {{ srcTables.length }} 张表 ·
                      已选 {{ form.mapping.tables.length }}
                    </span>
                    <div style="flex: 1" />
                    <n-button size="tiny" @click="setAllTables(true)">全选</n-button>
                    <n-button size="tiny" @click="setAllTables(false)">清空</n-button>
                  </div>
                  <n-checkbox-group v-model:value="form.mapping.tables">
                    <div style="display: flex; flex-direction: column; gap: 4px; max-height: 220px; overflow: auto">
                      <n-checkbox v-for="t in srcTables" :key="t.name" :value="t.name"
                        :label="t.rows_est != null ? `${t.name}（约 ${t.rows_est.toLocaleString()} 行）` : t.name" />
                    </div>
                  </n-checkbox-group>
                </template>
                <n-alert v-else-if="sourceId && srcDbs.length" type="info" :bordered="false">
                  源库表清单未加载。
                  <n-button text type="primary" @click="() => loadTables()">点击加载</n-button>
                </n-alert>
              </div>
            </n-form-item>
            <n-form-item label="表重命名">
              <n-input v-model:value="renameText" type="textarea" :rows="3"
                placeholder="每行一条：源表名:目标表名（一对一改名）" />
            </n-form-item>
            <n-form-item label="列/行过滤">
              <n-tag size="small" :bordered="false">列过滤 / 行过滤可在创建后的任务详情页「表映射」页签中编辑</n-tag>
            </n-form-item>
          </template>
        </n-form>
      </div>

      <!-- 步骤 3 -->
      <div v-else-if="step === 3">
        <n-alert v-if="multiDb && form.mode === 'init_plus_cdc' && form.auto_start_init" type="info" :bordered="false"
          style="margin-bottom: 16px">
          创建后将自动对目标库「不存在或为空」的 {{ autoInitCount }} 个库发起初始化
          （自动建库 → 迁移结构 → 拷贝数据 → 进入持续增量），按并发上限排队执行。
        </n-alert>
        <n-alert v-if="multiDb && form.mode === 'scheduled_full'" type="info" :bordered="false"
          style="margin-bottom: 16px">
          定时触发时自动判断：目标库不存在或为空 → 先建库并执行一次全量初始化；已有数据 → 执行全量重刷。
        </n-alert>
        <n-alert v-if="multiDb" type="warning" :bordered="false" style="margin-bottom: 16px">
          多库 / 低性能库建议：任务级限流 ≤ 5000 行/s（全部任务共享全局 20000 行/s 总闸）；
          初始化并发建议 1~2（同时初始化 2 个库的峰值连接约 20 条）；低配源库可将批大小降至 2000。
        </n-alert>
        <n-form label-placement="left" label-width="110">
          <n-form-item label="同步模式">
            <n-select v-model:value="form.mode" :options="MODE_OPTIONS" />
          </n-form-item>
          <template v-if="multiDb && form.mode === 'init_plus_cdc'">
            <n-form-item label="自动初始化">
              <n-switch v-model:value="form.auto_start_init" />
              <span style="margin-left: 8px; color: var(--Semantic-Color-text-tertiary); font-size: var(--Font-Size-font-caption)">
                创建后自动排队初始化（关闭则保持草稿，可在任务列表手动发起或批量初始化）
              </span>
            </n-form-item>
            <n-form-item v-if="form.auto_start_init" label="初始化并发">
              <n-input-number v-model:value="form.max_parallel" :min="1" :max="8" style="width: 160px" />
              <span style="margin-left: 8px; color: var(--Semantic-Color-text-tertiary); font-size: var(--Font-Size-font-caption)">
                同时初始化的库数量；低性能源库建议 1~2
              </span>
            </n-form-item>
          </template>
          <n-form-item label="批大小">
            <n-input-number v-model:value="form.batch_size" :min="100" :max="100000" :step="500" style="width: 200px" />
            <span style="margin-left: 8px; color: var(--Semantic-Color-text-tertiary); font-size: var(--Font-Size-font-caption)">行/批（默认 5000）</span>
          </n-form-item>
          <n-form-item label="任务级限流">
            <n-input-number v-model:value="form.rate_limit_rows" :min="0" :max="1000000" :step="1000" style="width: 200px" />
            <span style="margin-left: 8px; color: var(--Semantic-Color-text-tertiary); font-size: var(--Font-Size-font-caption)">行/秒，0 = 不限</span>
          </n-form-item>
          <n-form-item label="安全网备份">
            <n-switch v-model:value="form.safety_net_backup" />
            <span style="margin-left: 8px; color: var(--Semantic-Color-text-tertiary); font-size: var(--Font-Size-font-caption)">重刷前自动备份目标库涉及表</span>
          </n-form-item>
          <n-form-item label="孤儿行清理">
            <n-switch v-model:value="form.orphan_cleanup" />
            <span style="margin-left: 8px; color: var(--Semantic-Color-text-tertiary); font-size: var(--Font-Size-font-caption)">重刷后删除「目标有源无」的行</span>
          </n-form-item>
        </n-form>
      </div>

      <!-- 步骤 4 -->
      <div v-else>
        <template v-if="batchResult">
          <n-alert :type="batchResult.failed.length ? 'warning' : 'success'" style="margin-bottom: 16px">
            批量创建完成：成功 {{ batchResult.created.length }} 个
            <template v-if="batchResult.failed.length">，失败 {{ batchResult.failed.length }} 个</template>
            <template v-if="batchResult.init_enqueued.length">，已排队自动初始化 {{ batchResult.init_enqueued.length }} 个</template>
          </n-alert>
          <div v-if="batchResult.created.length" style="margin-bottom: 16px">
            <n-tag size="small" style="margin-bottom: 8px">已创建任务</n-tag>
            <div style="display: flex; flex-direction: column; gap: 4px">
              <div v-for="c in batchResult.created" :key="c.task_id"
                style="display: flex; gap: 10px; align-items: center">
                <span class="mono">{{ c.name }}</span>
                <span style="color: var(--Semantic-Color-text-tertiary); font-size: var(--Font-Size-font-caption)">
                  {{ c.source_database }} → {{ c.target_database }} · {{ stateLabel(c.target_state) }}
                </span>
              </div>
            </div>
          </div>
          <div v-if="batchResult.failed.length" style="margin-bottom: 16px">
            <n-tag size="small" type="error" :bordered="false" style="margin-bottom: 8px">失败明细</n-tag>
            <div style="display: flex; flex-direction: column; gap: 4px">
              <div v-for="(f, i) in batchResult.failed" :key="i"
                style="color: var(--Semantic-Color-error); font-size: var(--Font-Size-font-caption)">
                {{ f.source_database }} → {{ f.target_database }}：{{ f.reason }}
              </div>
            </div>
          </div>
          <n-button type="primary" @click="router.push({ name: 'tasks' })">前往任务列表</n-button>
        </template>
        <template v-else>
          <n-alert type="info" :bordered="false" style="margin-bottom: 16px">
            调度计划（cron / 时区 / 未来 5 次预览）与告警通道在任务详情页「调度」页签配置。
          </n-alert>
          <n-descriptions label-placement="left" bordered :column="1" size="small">
            <n-descriptions-item label="任务名称">{{ form.name }}<template v-if="multiDb">-{目标库名}</template></n-descriptions-item>
            <n-descriptions-item label="源 / 目标">#{{ sourceId }} → #{{ targetId }}（预检均无失败项）</n-descriptions-item>
            <n-descriptions-item v-if="!multiDb" label="映射范围">
              {{ form.mapping.scope === 'all' ? `同名整库（源库 ${srcSchema ?? '—'} 全部表）` : `源库 ${srcSchema ?? '—'} · 勾选 ${form.mapping.tables.length} 张表` }}
              <template v-if="Object.keys(parseRename()).length"> · 重命名 {{ Object.keys(parseRename()).length }} 项</template>
              <template v-if="pairRows[0]">
                · 库对应 <span class="mono">{{ pairRows[0].source }} → {{ pairRows[0].target }}</span>
                <span v-if="pairRows[0].pill" :style="{ color: pairRows[0].pill.color }">（{{ pairRows[0].pill.text }}）</span>
              </template>
            </n-descriptions-item>
            <n-descriptions-item v-else :label="`库对应（${pairs.length} 个任务）`">
              <div style="display: flex; flex-direction: column; gap: 2px; max-height: 160px; overflow: auto">
                <span v-for="p in pairRows" :key="p.source" class="mono" style="font-size: var(--Font-Size-font-caption)">
                  {{ p.source }} → {{ p.target }}
                  <span v-if="p.pill" :style="{ color: p.pill.color }">（{{ p.pill.text }}）</span>
                </span>
              </div>
            </n-descriptions-item>
            <n-descriptions-item label="模式">{{ MODE_OPTIONS.find((m) => m.value === form.mode)?.label }}</n-descriptions-item>
            <n-descriptions-item v-if="multiDb && form.mode === 'init_plus_cdc' && form.auto_start_init" label="自动初始化">
              开 · 并发 {{ form.max_parallel }}
            </n-descriptions-item>
            <n-descriptions-item label="批大小 / 限流"><span class="mono">{{ form.batch_size }}</span> 行/批 · <span class="mono">{{ form.rate_limit_rows }}</span> 行/s</n-descriptions-item>
            <n-descriptions-item label="安全网 / 孤儿清理">{{ form.safety_net_backup ? '开' : '关' }} / {{ form.orphan_cleanup ? '开' : '关' }}</n-descriptions-item>
          </n-descriptions>
        </template>
      </div>

      <div style="display: flex; justify-content: space-between; margin-top: 24px">
        <n-button v-if="step > 1 && !batchResult" @click="step -= 1">上一步</n-button>
        <div style="flex: 1" />
        <n-button v-if="step === 1" type="primary" :disabled="!step1Valid"
          @click="() => { step = 2; loadSchemas(); loadTgtSchemas(); loadTables(); }">下一步</n-button>
        <n-button v-else-if="step === 2" type="primary" :disabled="!step2Valid" @click="step = 3">下一步</n-button>
        <n-button v-else-if="step === 3" type="primary" @click="step = 4">下一步</n-button>
        <n-button v-else-if="!batchResult" type="primary" :loading="submitting" @click="submit">
          {{ multiDb ? `批量创建 ${pairs.length} 个任务` : '创建任务' }}
        </n-button>
      </div>
    </n-card>
  </div>
</template>
