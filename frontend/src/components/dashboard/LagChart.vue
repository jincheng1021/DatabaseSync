<script setup lang="ts">
/**
 * 仪表盘 · ECharts 多任务延迟趋势曲线（§6.2：WS 增量追加）。
 * 初始：/api/metrics/lag-batch 取运行中 CDC 任务最近 6h 历史（1 分钟粒度）；
 * 增量：WS task.lag 同分钟 bucket 覆盖、否则追加（与 TaskDetailView 同口径）。
 * 最多 8 条线；配色从语义 token 派生，不硬编码色值；多任务不画阈值线。
 */
import { nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import * as echarts from 'echarts'
import { metricsLagBatch, type LagPoint, type SyncTask } from '../../api'
import { useWsEvent } from '../../composables/wsBus'

const props = defineProps<{ tasks: SyncTask[] }>()

const MAX_LINES = 8
const MAX_POINTS = 420
const RANGE_MINUTES = 360

const chartEl = ref<HTMLElement | null>(null)
let chart: echarts.ECharts | null = null
let tracked = new Map<number, string>()          // task_id → 任务名
const series = ref(new Map<number, LagPoint[]>()) // task_id → 点列
const hasData = ref(false)
const truncated = ref(false)

function cssVar(name: string, fallback: string): string {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return v || fallback
}

// 调色板：语义 token 派生（info/success/warning/error + text-secondary），超 4 条用 color-mix 变体
function palette(): string[] {
  const base = [
    cssVar('--Semantic-Color-info', '#21D4ED'),
    cssVar('--Semantic-Color-success', '#0FBA82'),
    cssVar('--Semantic-Color-warning', '#F59E0A'),
    cssVar('--Semantic-Color-error', '#F5405E'),
    cssVar('--Semantic-Color-text-secondary', '#A6A6AB'),
  ]
  const out: string[] = [...base]
  for (let i = 0; i < MAX_LINES - base.length; i++) {
    out.push(`color-mix(in srgb, ${base[i % base.length]} ${60 - i * 10}%, transparent)`)
  }
  return out
}

function pickTasks(): SyncTask[] {
  const running = props.tasks.filter((t) => t.lag_seconds != null)
  truncated.value = running.length > MAX_LINES
  return running.slice(0, MAX_LINES)
}

async function loadHistory() {
  const picked = pickTasks()
  const newTracked = new Map(picked.map((t) => [t.id, t.name]))
  const same = newTracked.size === tracked.size && [...newTracked.keys()].every((k) => tracked.has(k))
  if (same && series.value.size > 0) return
  tracked = newTracked
  series.value = new Map(picked.map((t) => [t.id, []]))
  if (picked.length === 0) {
    hasData.value = false
    render()
    return
  }
  try {
    const r = await metricsLagBatch(picked.map((t) => t.id), RANGE_MINUTES)
    for (const s of r.series) {
      series.value.set(s.task_id, s.points.slice(-MAX_POINTS))
    }
    hasData.value = r.series.some((s) => s.points.length > 0)
    render()
  } catch { /* 指标缺失不阻塞仪表盘 */ }
}

// WS task.lag 增量：仅已追踪任务；同分钟覆盖
useWsEvent(['task.lag'], (env) => {
  const p = env.payload as { task_id?: number; lag_seconds?: number | null; timestamp?: number } | undefined
  if (!p || p.task_id == null || !tracked.has(p.task_id)) return
  if (p.lag_seconds == null || !p.timestamp) return
  const arr = series.value.get(p.task_id) ?? []
  const pt: LagPoint = { ts: p.timestamp, lag_seconds: p.lag_seconds }
  const bucket = Math.floor(pt.ts / 60000)
  const last = arr[arr.length - 1]
  if (last && Math.floor(last.ts / 60000) === bucket) arr[arr.length - 1] = pt
  else arr.push(pt)
  if (arr.length > MAX_POINTS) arr.splice(0, arr.length - MAX_POINTS)
  series.value.set(p.task_id, arr)
  hasData.value = true
  render()
})

function render() {
  if (!chart) return
  if (!hasData.value) {
    chart.clear()
    return
  }
  const colors = palette()
  const lines: echarts.SeriesOption[] = []
  let i = 0
  for (const [tid, pts] of series.value) {
    lines.push({
      type: 'line',
      name: tracked.get(tid) ?? `任务 ${tid}`,
      showSymbol: false,
      smooth: true,
      data: pts.map((p) => [p.ts, Number(p.lag_seconds.toFixed(2))]),
      lineStyle: { color: colors[i % colors.length], width: 1.5 },
      emphasis: { focus: 'series' },
    })
    i += 1
  }
  chart.setOption({
    backgroundColor: 'transparent',
    grid: { left: 48, right: 16, top: 32, bottom: 28 },
    legend: { top: 0, type: 'scroll',
      textStyle: { color: cssVar('--Semantic-Color-text-tertiary', '#75757A'), fontSize: 11 },
      pageIconColor: cssVar('--Semantic-Color-text-tertiary', '#75757A'),
      pageTextStyle: { color: cssVar('--Semantic-Color-text-tertiary', '#75757A') } },
    tooltip: { trigger: 'axis',
      valueFormatter: (v: unknown) => `${v as number}s` },
    xAxis: { type: 'time',
      axisLine: { lineStyle: { color: cssVar('--Semantic-Color-border', '#262629') } },
      axisLabel: { color: cssVar('--Semantic-Color-text-tertiary', '#75757A'), fontSize: 11 },
      splitLine: { show: false } },
    yAxis: { type: 'value', name: '延迟 (s)',
      nameTextStyle: { color: cssVar('--Semantic-Color-text-tertiary', '#75757A') },
      axisLabel: { color: cssVar('--Semantic-Color-text-tertiary', '#75757A'), fontSize: 11 },
      splitLine: { lineStyle: { color: cssVar('--Semantic-Color-border', '#262629') } } },
    series: lines,
  }, { notMerge: true })
}

watch(() => props.tasks, () => { void loadHistory() })

const _onResize = () => chart?.resize()
onMounted(async () => {
  window.addEventListener('resize', _onResize)
  await nextTick()
  if (chartEl.value && !chart) chart = echarts.init(chartEl.value)
  await loadHistory()
})
onUnmounted(() => {
  window.removeEventListener('resize', _onResize)
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div>
    <div v-show="hasData" ref="chartEl" style="width: 100%; height: 260px" />
    <div v-if="!hasData" class="empty">
      <span style="color: var(--Semantic-Color-text-tertiary)">
        暂无运行中的增量任务，启动后此处展示多任务延迟曲线
      </span>
    </div>
    <div v-if="truncated" class="trunc">
      运行中任务较多，仅展示前 {{ MAX_LINES }} 条曲线
    </div>
  </div>
</template>

<style scoped>
.empty {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 160px;
}
.trunc {
  font-size: var(--Font-Size-font-micro);
  color: var(--Semantic-Color-text-tertiary);
  margin-top: var(--Spacing-space-1, 4px);
}
</style>
