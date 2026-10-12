<script setup lang="ts">
/**
 * cron 周期配置共享组件（T4.6 / 向导步骤 3 复用）：
 * 图形周期配置（每天/每周/每月/每小时/固定间隔/手输双模式，自动拼表达式）
 * + 时区选择 + 未来 5 次触发预览（400ms 防抖）。
 * 不含名称/动作/策略等字段——由调用方（ScheduleTab 抽屉 / TaskWizardView）决定。
 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NForm, NFormItem, NInput, NInputNumber, NSelect } from 'naive-ui'
import { scheduleApi } from '../api'
import { errMsg } from '../api/client'

const props = defineProps<{ cron: string; timezone: string }>()
const emit = defineEmits<{ 'update:cron': [v: string]; 'update:timezone': [v: string] }>()

type Cycle = 'daily' | 'weekly' | 'monthly' | 'hourly' | 'interval' | 'manual'

const CYCLE_OPTIONS: { label: string; value: Cycle }[] = [
  { label: '每天（指定时刻）', value: 'daily' },
  { label: '每周（指定星期 + 时刻）', value: 'weekly' },
  { label: '每月（指定日期 + 时刻）', value: 'monthly' },
  { label: '每小时（指定分钟）', value: 'hourly' },
  { label: '固定间隔（每 N 分钟）', value: 'interval' },
  { label: '手输 cron 表达式', value: 'manual' },
]

const TZ_OPTIONS = ['Asia/Shanghai', 'UTC', 'Asia/Tokyo', 'Asia/Singapore', 'Europe/London',
  'America/New_York'].map((t) => ({ label: t, value: t }))

const pad = (n: number) => String(n).padStart(2, '0')
const HOUR_OPTIONS = Array.from({ length: 24 }, (_, i) => ({ label: pad(i), value: i }))
const MINUTE_OPTIONS = Array.from({ length: 60 }, (_, i) => ({ label: pad(i), value: i }))
const WEEKDAY_OPTIONS = [
  { label: '周日', value: 0 }, { label: '周一', value: 1 }, { label: '周二', value: 2 },
  { label: '周三', value: 3 }, { label: '周四', value: 4 }, { label: '周五', value: 5 },
  { label: '周六', value: 6 },
]
const MONTH_DAY_OPTIONS = Array.from({ length: 31 }, (_, i) => ({ label: `${i + 1} 日`, value: i + 1 }))

const cycle = ref<Cycle>('daily')
const hour = ref(3)
const minute = ref(0)
const weekdays = ref<number[]>([1])
const monthDay = ref(1)
const intervalMin = ref(30)

const preview = ref<string[]>([])
const cronError = ref('')

function buildCronFromCycle(): string {
  switch (cycle.value) {
    case 'daily': return `${minute.value} ${hour.value} * * *`
    case 'weekly': {
      const w = weekdays.value.length
        ? [...weekdays.value].sort((a, b) => a - b).join(',') : '*'
      return `${minute.value} ${hour.value} * * ${w}`
    }
    case 'monthly': return `${minute.value} ${hour.value} ${monthDay.value} * *`
    case 'hourly': return `${minute.value} * * * *`
    case 'interval': return `*/${Math.min(59, Math.max(1, Math.round(intervalMin.value || 30)))} * * * *`
    default: return props.cron
  }
}

/** 反向解析：cron → 图形参数；不匹配返回 null（回退手输模式）。 */
function parseCron(cron: string): { cycle: Cycle; hour?: number; minute?: number;
  weekdays?: number[]; monthDay?: number; intervalMin?: number } | null {
  const parts = cron.trim().split(/\s+/)
  if (parts.length !== 5) return null
  const [m, h, d, mon, w] = parts
  if (mon !== '*') return null
  const num = (s: string) => (/^\d+$/.test(s) ? Number(s) : null)
  const iv = /^\*\/(\d+)$/.exec(m)
  if (iv && h === '*' && d === '*' && w === '*') {
    const n = Number(iv[1])
    return n >= 1 && n <= 59 ? { cycle: 'interval', intervalMin: n } : null
  }
  const mi = num(m)
  if (mi === null) return null
  if (h === '*' && d === '*' && w === '*') return { cycle: 'hourly', minute: mi }
  const hi = num(h)
  if (hi === null) return null
  if (d === '*' && w === '*') return { cycle: 'daily', hour: hi, minute: mi }
  if (d === '*' && w !== '*') {
    const ws = w.split(',').map(num)
    if (ws.some((x) => x === null || x! > 6)) return null
    return { cycle: 'weekly', hour: hi, minute: mi,
      weekdays: [...new Set(ws as number[])].sort((a, b) => a - b) }
  }
  const di = num(d)
  if (w === '*' && di !== null) return { cycle: 'monthly', hour: hi, minute: mi, monthDay: di }
  return null
}

/** 用外部传入的 cron 回填图形参数（不匹配回退 manual）。 */
function applyCron(cron: string) {
  const p = parseCron(cron)
  if (p) {
    cycle.value = p.cycle
    if (p.hour !== undefined) hour.value = p.hour
    if (p.minute !== undefined) minute.value = p.minute
    if (p.weekdays !== undefined) weekdays.value = p.weekdays
    if (p.monthDay !== undefined) monthDay.value = p.monthDay
    if (p.intervalMin !== undefined) intervalMin.value = p.intervalMin
  } else {
    cycle.value = 'manual'
  }
}

// ---------- 外部 cron 变更 → 回填（lastEmittedCron 防回环） ----------
let lastEmittedCron: string | null = null

watch(() => props.cron, (v) => {
  if (v === lastEmittedCron) return
  applyCron(v)
  schedulePreview()
})

function onCycleChange() {
  if (cycle.value === 'manual') return
  const c = buildCronFromCycle()
  lastEmittedCron = c
  emit('update:cron', c)
  schedulePreview()
}

function onCronInput(v: string) {
  cycle.value = 'manual'
  lastEmittedCron = v
  emit('update:cron', v)
  schedulePreview()
}

function onTzChange(v: string) {
  emit('update:timezone', v)
  schedulePreview()
}

// ---------- 触发预览（400ms 防抖） ----------
let previewTimer: ReturnType<typeof setTimeout> | undefined
function schedulePreview() {
  if (previewTimer) clearTimeout(previewTimer)
  previewTimer = setTimeout(() => void refreshPreview(), 400)
}

async function refreshPreview() {
  cronError.value = ''
  if (!props.cron.trim()) {
    preview.value = []
    return
  }
  try {
    preview.value = (await scheduleApi.preview(props.cron, props.timezone, 5)).next
  } catch (e) {
    preview.value = []
    cronError.value = errMsg(e)
  }
}

/** 本地校验（weekly 未选星期等），返回错误文案或 null。 */
function validate(): string | null {
  if (cycle.value === 'weekly' && !weekdays.value.length) {
    return '每周模式请至少勾选一个星期'
  }
  return null
}

onMounted(() => {
  applyCron(props.cron)
  void refreshPreview()
})

onBeforeUnmount(() => { if (previewTimer) clearTimeout(previewTimer) })

defineExpose({ validate, refreshPreview, cronError })
</script>

<template>
  <n-form label-placement="top" size="small">
    <n-form-item label="cron 周期（图形配置 / 手输双模式）">
      <div style="width: 100%; display: flex; gap: 8px; flex-direction: column">
        <n-select v-model:value="cycle" :options="CYCLE_OPTIONS" @update:value="onCycleChange" />
        <div v-if="cycle === 'daily'" class="cycle-row">
          <span class="cycle-label">每天</span>
          <n-select v-model:value="hour" :options="HOUR_OPTIONS" class="time-sel"
            @update:value="onCycleChange" />
          <span class="cycle-label">点</span>
          <n-select v-model:value="minute" :options="MINUTE_OPTIONS" class="time-sel"
            @update:value="onCycleChange" />
          <span class="cycle-label">分</span>
        </div>
        <div v-else-if="cycle === 'weekly'" class="cycle-row">
          <span class="cycle-label">每周</span>
          <n-select v-model:value="weekdays" :options="WEEKDAY_OPTIONS" multiple
            class="week-sel" placeholder="选择星期…" @update:value="onCycleChange" />
          <n-select v-model:value="hour" :options="HOUR_OPTIONS" class="time-sel"
            @update:value="onCycleChange" />
          <span class="cycle-label">点</span>
          <n-select v-model:value="minute" :options="MINUTE_OPTIONS" class="time-sel"
            @update:value="onCycleChange" />
          <span class="cycle-label">分</span>
        </div>
        <div v-else-if="cycle === 'monthly'" class="cycle-row">
          <span class="cycle-label">每月</span>
          <n-select v-model:value="monthDay" :options="MONTH_DAY_OPTIONS" class="time-sel"
            @update:value="onCycleChange" />
          <n-select v-model:value="hour" :options="HOUR_OPTIONS" class="time-sel"
            @update:value="onCycleChange" />
          <span class="cycle-label">点</span>
          <n-select v-model:value="minute" :options="MINUTE_OPTIONS" class="time-sel"
            @update:value="onCycleChange" />
          <span class="cycle-label">分</span>
        </div>
        <div v-else-if="cycle === 'hourly'" class="cycle-row">
          <span class="cycle-label">每小时第</span>
          <n-select v-model:value="minute" :options="MINUTE_OPTIONS" class="time-sel"
            @update:value="onCycleChange" />
          <span class="cycle-label">分</span>
        </div>
        <div v-else-if="cycle === 'interval'" class="cycle-row">
          <span class="cycle-label">每</span>
          <n-input-number v-model:value="intervalMin" :min="1" :max="59" :precision="0"
            class="time-sel" @update:value="onCycleChange" />
          <span class="cycle-label">分钟（按小时内取点）</span>
        </div>
        <n-input v-else :value="cron" class="mono" placeholder="分 时 日 月 周，如 0 3 * * *"
          @update:value="onCronInput" />
        <n-input v-if="cycle !== 'manual'" :value="cron" class="mono" readonly
          placeholder="生成的 cron 表达式" />
      </div>
    </n-form-item>
    <n-form-item label="时区">
      <n-select :value="timezone" :options="TZ_OPTIONS" tag filterable @update:value="onTzChange" />
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
  </n-form>
</template>

<style scoped>
.mono {
  font-family: 'JetBrains Mono', Consolas, monospace;
}
.cycle-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.cycle-label {
  color: var(--Semantic-Color-text-secondary, inherit);
  white-space: nowrap;
}
.time-sel {
  width: 88px;
}
.week-sel {
  min-width: 180px;
  flex: 1;
}
</style>
