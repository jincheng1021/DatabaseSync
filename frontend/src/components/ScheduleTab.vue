<script setup lang="ts">
/**
 * 任务详情 · 调度页签（T4.6 / §6.2 页面 4「调度」页签）：
 * schedule 列表 + 新建/编辑抽屉（周期配置复用 CronCycleConfig 共享组件）
 * + 全量重刷参数（表勾选集/安全网/孤儿清理）。
 */
import { computed, h, onMounted, ref } from 'vue'
import {
  NButton, NCard, NDataTable, NDrawer, NDrawerContent, NForm, NFormItem, NInput,
  NRadioButton, NRadioGroup, NSelect, NSwitch, useDialog, useMessage,
} from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import {
  ACTION_LABELS, scheduleApi, type Schedule, type ScheduleAction, type SyncTask,
} from '../api'
import { errMsg } from '../api/client'
import CronCycleConfig from './CronCycleConfig.vue'

const props = defineProps<{ taskId: number; taskState: string; task?: SyncTask | null }>()
const message = useMessage()
const dialog = useDialog()

const items = ref<Schedule[]>([])
const loading = ref(false)
const showDrawer = ref(false)
const editing = ref<Schedule | null>(null)
const busy = ref(false)
const cronCycleRef = ref<InstanceType<typeof CronCycleConfig> | null>(null)

const ACTION_OPTIONS = (Object.keys(ACTION_LABELS) as ScheduleAction[])
  .map((k) => ({ label: ACTION_LABELS[k], value: k }))

const DEFAULT_FORM = () => ({
  name: '',
  action: 'verify' as ScheduleAction,
  cron: '0 3 * * *',
  timezone: 'Asia/Shanghai',
  enabled: true,
  overlap_policy: 'skip',
  misfire_policy: 'skip',
  method: 'checksum',
  resyncTables: [] as string[],
  safetyNet: true,
  orphanCleanup: true,
})

const form = ref(DEFAULT_FORM())

// ---------- 全量重刷参数（表勾选集来自任务映射） ----------
const taskTableOptions = computed(() => {
  const mp = props.task?.mapping
  return mp?.scope === 'selected' && mp.tables?.length
    ? mp.tables.map((t) => ({ label: t, value: t }))
    : []
})

// ---------- 列表加载 ----------
async function load() {
  loading.value = true
  try {
    items.value = (await scheduleApi.listByTask(props.taskId)).schedules
  } catch (e) {
    message.error(errMsg(e))
  } finally {
    loading.value = false
  }
}
onMounted(load)

function openCreate() {
  editing.value = null
  form.value = DEFAULT_FORM()
  showDrawer.value = true
}

function openEdit(s: Schedule) {
  const ap = (s.action_params ?? {}) as {
    method?: string; tables?: string[]; safety_net?: boolean; orphan_cleanup?: boolean
  }
  editing.value = s
  form.value = {
    ...DEFAULT_FORM(),
    name: s.name, action: s.action, cron: s.cron, timezone: s.timezone,
    enabled: s.enabled, overlap_policy: s.overlap_policy, misfire_policy: s.misfire_policy,
    method: ap.method || 'checksum',
    resyncTables: ap.tables ?? [],
    safetyNet: ap.safety_net ?? true,
    orphanCleanup: ap.orphan_cleanup ?? true,
  }
  showDrawer.value = true
}

async function save() {
  const f = form.value
  if (!f.name.trim()) {
    message.warning('请填写计划名称')
    return
  }
  const localErr = cronCycleRef.value?.validate()
  if (localErr) {
    message.warning(localErr)
    return
  }
  await cronCycleRef.value?.refreshPreview()
  if (cronCycleRef.value?.cronError) {
    message.warning('cron 表达式不合法，请修正后再保存')
    return
  }
  let action_params: Record<string, unknown> = {}
  if (f.action === 'verify') action_params = { method: f.method }
  else if (f.action === 'full_resync') {
    action_params = { safety_net: f.safetyNet, orphan_cleanup: f.orphanCleanup }
    if (f.resyncTables.length) action_params.tables = f.resyncTables
  }
  busy.value = true
  try {
    const body = {
      name: f.name.trim(), action: f.action, cron: f.cron,
      timezone: f.timezone, enabled: f.enabled,
      overlap_policy: f.overlap_policy, misfire_policy: f.misfire_policy,
      action_params,
    }
    if (editing.value) await scheduleApi.update(editing.value.id, body)
    else await scheduleApi.create(props.taskId, body)
    message.success(editing.value ? '调度计划已更新' : '调度计划已创建')
    showDrawer.value = false
    await load()
  } catch (e) {
    message.error(errMsg(e))
  } finally {
    busy.value = false
  }
}

async function toggleEnabled(s: Schedule) {
  try {
    await scheduleApi.update(s.id, { enabled: !s.enabled })
    await load()
  } catch (e) {
    message.error(errMsg(e))
  }
}

function doRemove(s: Schedule) {
  dialog.warning({
    title: '删除调度计划',
    content: `确认删除调度计划「${s.name}」？删除后不再自动触发。`,
    positiveText: '删除', negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await scheduleApi.remove(s.id)
        message.success('已删除')
        await load()
      } catch (e) {
        message.error(errMsg(e))
      }
    },
  })
}

const triggeringId = ref<number | null>(null)

function doTrigger(s: Schedule) {
  dialog.warning({
    title: '立即执行',
    content: `确认立即执行调度计划「${s.name}」？将按调度语义执行（受并发策略约束），结果见执行历史。`,
    positiveText: '立即执行', negativeText: '取消',
    onPositiveClick: async () => {
      triggeringId.value = s.id
      try {
        await scheduleApi.trigger(s.id)
        message.success('已提交执行，结果见执行历史')
      } catch (e) {
        message.error(errMsg(e))
      } finally {
        triggeringId.value = null
      }
    },
  })
}

const columnsFinal: DataTableColumns<Schedule> = [
  { title: '名称', key: 'name', width: 150 },
  { title: '动作', key: 'action', width: 100,
    render: (r) => ACTION_LABELS[r.action] ?? r.action },
  { title: 'cron', key: 'cron', width: 120, className: 'mono' },
  { title: '时区', key: 'timezone', width: 120, className: 'mono' },
  { title: '下次触发', key: 'next_fire_at', width: 180, className: 'mono',
    render: (r) => r.next_fire_at ? r.next_fire_at.replace('T', ' ').slice(0, 19) : '—' },
  { title: '并发/错过', key: 'pol', width: 110,
    render: (r) => `${r.overlap_policy === 'queue' ? '排队' : '跳过'} / ${r.misfire_policy === 'run_once' ? '补跑' : '跳过'}` },
  { title: '状态', key: 'enabled', width: 80,
    render: (r) => h(NSwitch, { size: 'small', value: r.enabled,
      onUpdateValue: () => toggleEnabled(r) }) },
  { title: '操作', key: 'ops', width: 250,
    render: (r) => [
      h(NButton, { size: 'tiny', quaternary: true, type: 'primary',
        loading: triggeringId.value === r.id,
        onClick: () => doTrigger(r) }, { default: () => '立即执行' }),
      h(NButton, { size: 'tiny', quaternary: true,
        onClick: () => openEdit(r) }, { default: () => '编辑' }),
      h(NButton, { size: 'tiny', quaternary: true, type: 'error',
        onClick: () => doRemove(r) }, { default: () => '删除' }),
    ] },
]
</script>

<template>
  <n-card title="调度计划" style="border-radius: var(--Radius-radius-lg)">
    <template #header-extra>
      <n-button size="small" type="primary" @click="openCreate">+ 新建调度</n-button>
    </template>
    <n-data-table :columns="columnsFinal" :data="items" :loading="loading" size="small"
      :bordered="false" :row-key="(r: Schedule) => r.id" />
    <p class="stat-sub" style="margin-top: 8px">
      六类动作：启动/停止增量、全量重刷（带表勾选集）、数据校验、备份导出、孤儿行清理；
      同任务同动作同时刻单实例，前次未完成按「并发策略」处理；停机跨过触发点按「错过补偿」处理。
    </p>

    <n-drawer v-model:show="showDrawer" :width="480">
      <n-drawer-content :title="editing ? '编辑调度计划' : '新建调度计划'" closable>
        <n-form label-placement="top" size="small">
          <n-form-item label="计划名称">
            <n-input v-model:value="form.name" placeholder="如：每日凌晨校验" maxlength="128" />
          </n-form-item>
          <n-form-item label="动作">
            <n-select v-model:value="form.action" :options="ACTION_OPTIONS" />
          </n-form-item>
          <n-form-item v-if="form.action === 'verify'" label="校验方式">
            <n-radio-group v-model:value="form.method">
              <n-radio-button value="checksum">checksum 分块比对</n-radio-button>
              <n-radio-button value="row_count">行数比对</n-radio-button>
            </n-radio-group>
          </n-form-item>
          <template v-if="form.action === 'full_resync'">
            <n-form-item label="重刷表范围（不选 = 全部表）">
              <n-select v-model:value="form.resyncTables" multiple clearable
                :options="taskTableOptions" :disabled="!taskTableOptions.length"
                placeholder="默认重刷任务映射中的全部表" />
            </n-form-item>
            <n-form-item label="安全网（重刷前自动备份）">
              <n-switch v-model:value="form.safetyNet" />
            </n-form-item>
            <n-form-item label="孤儿行清理（重刷后清理目标多余行）">
              <n-switch v-model:value="form.orphanCleanup" />
            </n-form-item>
          </template>
          <CronCycleConfig ref="cronCycleRef" v-model:cron="form.cron"
            v-model:timezone="form.timezone" />
          <n-form-item label="并发策略（前次未完成时）">
            <n-radio-group v-model:value="form.overlap_policy">
              <n-radio-button value="skip">跳过并记录</n-radio-button>
              <n-radio-button value="queue">排队等待</n-radio-button>
            </n-radio-group>
          </n-form-item>
          <n-form-item label="错过补偿（停机跨过触发点）">
            <n-radio-group v-model:value="form.misfire_policy">
              <n-radio-button value="skip">跳过</n-radio-button>
              <n-radio-button value="run_once">补跑一次</n-radio-button>
            </n-radio-group>
          </n-form-item>
          <n-form-item label="启用">
            <n-switch v-model:value="form.enabled" />
          </n-form-item>
        </n-form>
        <template #footer>
          <n-button type="primary" :loading="busy" @click="save">保存</n-button>
        </template>
      </n-drawer-content>
    </n-drawer>
  </n-card>
</template>

<style scoped>
.stat-sub {
  font-size: var(--Font-Size-font-caption);
  color: var(--Semantic-Color-text-tertiary);
}
.mono {
  font-family: 'JetBrains Mono', Consolas, monospace;
}
</style>
