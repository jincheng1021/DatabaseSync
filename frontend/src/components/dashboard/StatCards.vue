<script setup lang="ts">
/**
 * 仪表盘 · 统计卡（§6.2：任务总数/运行中/异常/今日事件量）。
 * 数据：REST /api/dashboard/summary 初始化 + WS 增量修正：
 * - task.state_changed → 按最新任务状态重算（父组件下发 tasks 或事件携带 state）
 * - alert.fired / alert.resolved → 未解除告警数 ±1
 * - run.finished → 节流重拉 summary（今日事件量兜底）
 * 数字一律 .mono（JetBrains Mono）；卡片可点击跳转。
 */
import { computed } from 'vue'
import { NCard } from 'naive-ui'
import { useRouter } from 'vue-router'
import type { DashboardSummary } from '../../api'

const props = defineProps<{ summary: DashboardSummary | null }>()

const router = useRouter()

const cards = computed(() => {
  const c = props.summary?.task_counts
  const t = props.summary?.today_events
  return [
    { label: '任务总数', value: c?.total, to: { name: 'tasks' }, hint: '全部同步任务', accent: false },
    { label: '运行中', value: c?.running, to: { name: 'tasks' }, hint: '初始化 / 重刷 / 增量 / 备份', accent: true },
    { label: '异常', value: c?.error, to: { name: 'tasks' }, hint: '异常 + 需重新初始化', accent: false, extra: c?.needs_reinit ?? 0 },
    { label: '今日事件量', value: t?.total, to: { name: 'runs' }, hint: '今日累计应用行数', accent: false },
  ]
})

function fmt(n: number | null | undefined): string {
  return n == null ? '—' : n.toLocaleString()
}

function onClick(to: { name: string }) {
  router.push(to)
}
</script>

<template>
  <div class="stat-grid">
    <n-card v-for="c in cards" :key="c.label" class="stat-card" :class="{ accent: c.accent }"
      size="small" @click="onClick(c.to)">
      <div class="label">{{ c.label }}</div>
      <div class="mono value" :class="{ error: c.label === '异常' && (c.value ?? 0) > 0,
                                        ok: c.label === '运行中' && (c.value ?? 0) > 0 }">
        {{ fmt(c.value) }}
        <span v-if="c.label === '异常' && (c.extra ?? 0) > 0" class="mono sub">+{{ c.extra }} 待重建</span>
      </div>
      <div class="hint">{{ c.hint }}</div>
    </n-card>
  </div>
</template>

<style scoped>
.stat-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: var(--Spacing-space-4, 16px);
  margin-bottom: var(--Spacing-space-4, 16px);
}
.stat-card {
  cursor: pointer;
  transition: border-color 0.15s;
  border-radius: var(--Radius-radius-lg, 8px);
}
.stat-card:hover {
  border-color: var(--Semantic-Color-info);
}
.label {
  font-size: var(--Font-Size-font-caption);
  color: var(--Semantic-Color-text-tertiary);
}
.value {
  font-size: 28px;
  margin-top: var(--Spacing-space-2, 8px);
  color: var(--Semantic-Color-text-primary);
}
.value.error {
  color: var(--Semantic-Color-error);
}
.value.ok {
  color: var(--Semantic-Color-info);
}
.sub {
  font-size: var(--Font-Size-font-micro);
  color: var(--Semantic-Color-text-tertiary);
  margin-left: 8px;
}
.hint {
  font-size: var(--Font-Size-font-micro);
  color: var(--Semantic-Color-text-tertiary);
  margin-top: var(--Spacing-space-1, 4px);
}
</style>
