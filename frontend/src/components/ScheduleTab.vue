<script setup lang="ts">
/**
 * 任务详情 · 调度页签（T4.6 / §6.2 页面 4「调度」页签）：
 * schedule 列表 + cron 图形模板/手输双模式 + 时区选择 + 未来 5 次触发预览（E11）。
 */
import { computed, h, onMounted, ref } from 'vue'
import {
  NButton, NCard, NDataTable, NDrawer, NDrawerContent, NForm, NFormItem, NInput,
  NRadioButton, NRadioGroup, NSelect, NSwitch, useDialog, useMessage,
} from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import {
  ACTION_LABELS, scheduleApi, type Schedule, type ScheduleAction,
} from '../api'
import { errMsg } from '../api/client'

const props = defineProps<{ taskId: number; taskState: string }>()
const message = useMessage()
const dialog = useDialog()

const items = ref<Schedule[]>([])
const loading = ref(false)
const showDrawer = ref(false)
const editing = ref<Schedule | null>(null)
const busy = ref(false)

const ACTION_OPTIONS = (Object.keys(ACTION_LABELS) as ScheduleAction[])
  .map((k) => ({ label: ACTION_LABELS[k], value: k }))

const TZ_OPTIONS = ['Asia/Shanghai', 'UTC', 'Asia/Tokyo', 'Asia/Singapore', 'Europe/London',
  'America/New_York'].map((t) => ({ label: t, value: t }))

const form = ref({
  name: '',
  action: 'verify' as ScheduleAction,
  cron: '0 3 * * *',
  cronMode: 'daily',
  timezone: 'Asia/Shanghai',
  enabled: true,
  overlap_policy: 'skip',
  misfire_policy: 'skip',
  method: 'checksum',
})

// cron 图形模板：常用周期一键填充（双模式）
const CRON_TEMPLATES = [
  { label: '每天 03:00', value: 'daily', cron: '0 3 * * *' },
  { label: '每天 02:00 / 14:00', value: 'twice', cron: '0 2,14 * * *' },
  { label: '每小时整点', value: 'hourly', cron: '0 * * * *' },
  { label: '每 30 分钟', value: 'halfhour', cron: '*/30 * * * *' },
  { label: '每周一 02:00', value: 'weekly', cron: '0 2 * * 1' },
  { label: '每月 1 日 02:00', value: 'monthly', cron: '0 2 1 * *' },
]

const preview = ref<string[]>([])
const cronError = ref('')
const previewTz = computed(() => form.value.timezone)

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

async function refreshPreview() {
  cronError.value = ''
  if (!form.value.cron.trim()) {
    preview.value = []
    return
  }
  try {
    preview.value = (await scheduleApi.preview(form.value.cron, form.value.timezone, 5)).next
  } catch (e) {
    preview.value = []
    cronError.value = errMsg(e)
  }
}

function onTemplateChange(v: string) {
  const t = CRON_TEMPLATES.find((x) => x.value === v)
  if (t) {
    form.value.cron = t.cron
    void refreshPreview()
  }
}

function onCronInput() {
  form.value.cronMode = 'manual'
  void refreshPreview()
}

function openCreate() {
  editing.value = null
  form.value = { ...form.value, name: '', action: 'verify', cron: '0 3 * * *',
    cronMode: 'daily', timezone: 'Asia/Shanghai', enabled: true,
    overlap_policy: 'skip', misfire_policy: 'skip', method: 'checksum' }
  preview.value = []
  cronError.value = ''
  showDrawer.value = true
}

function openEdit(s: Schedule) {
  editing.value = s
  form.value = { ...form.value, name: s.name, action: s.action, cron: s.cron,
    cronMode: 'manual', timezone: s.timezone, enabled: s.enabled,
    overlap_policy: s.overlap_policy, misfire_policy: s.misfire_policy,
    method: (s.action_params as { method?: string } | null)?.method || 'checksum' }
  preview.value = []
  cronError.value = ''
  showDrawer.value = true
  void refreshPreview()
}

async function save() {
  if (!form.value.name.trim()) {
    message.warning('请填写计划名称')
    return
  }
  cronError.value = ''
  await refreshPreview()
  if (cronError.value) {
    message.warning('cron 表达式不合法，请修正后再保存')
    return
  }
  busy.value = true
  try {
    const body = {
      name: form.value.name.trim(), action: form.value.action, cron: form.value.cron,
      timezone: form.value.timezone, enabled: form.value.enabled,
      overlap_policy: form.value.overlap_policy, misfire_policy: form.value.misfire_policy,
      action_params: form.value.action === 'verify' ? { method: form.value.method } : {},
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
          <n-form-item label="cron 周期（图形模板 / 手输双模式）">
            <div style="width: 100%; display: flex; gap: 8px; flex-direction: column">
              <n-select :value="form.cronMode" :options="CRON_TEMPLATES"
                placeholder="选择常用周期…" @update:value="onTemplateChange" />
              <n-input :value="form.cron" class="mono" placeholder="分 时 日 月 周，如 0 3 * * *"
                @update:value="onCronInput" />
            </div>
          </n-form-item>
          <n-form-item label="时区">
            <n-select v-model:value="form.timezone" :options="TZ_OPTIONS" tag filterable
              @update:value="refreshPreview" />
          </n-form-item>
          <n-form-item label="未来 5 次触发预览">
            <div class="mono" style="font-size: var(--Font-Size-font-caption); line-height: 1.8">
              <div v-for="(p, i) in preview" :key="i"
                style="color: var(--Semantic-Color-info)">{{ p.replace('T', ' ') }}</div>
              <div v-if="cronError" style="color: var(--Semantic-Color-error)">{{ cronError }}</div>
              <div v-if="!preview.length && !cronError" style="color: var(--Semantic-Color-text-tertiary)">
                输入合法 cron 后自动计算…</div>
            </div>
          </n-form-item>
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
