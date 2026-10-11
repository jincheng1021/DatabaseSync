<script setup lang="ts">
/**
 * 仪表盘 · 最近告警时间线（§6.2：最近告警与最近运行时间线）。
 * 数据：GET /api/alerts(10)；WS alert.fired 置顶 / alert.resolved 原地标记。
 * level → n-timeline 类型映射；未恢复告警高亮「未解除」。
 */
import { computed, onMounted, ref } from 'vue'
import { NButton, NTag, NTimeline, NTimelineItem, useMessage } from 'naive-ui'
import { settingsApi, type AlertLog } from '../../api'
import { errMsg } from '../../api/client'
import { useWsEvent } from '../../composables/wsBus'

const alerts = ref<AlertLog[]>([])

async function load() {
  try {
    const r = await settingsApi.alerts(10)
    alerts.value = r.items
  } catch { /* 降级：保持空态，不阻塞仪表盘 */ }
}
onMounted(load)

useWsEvent(['alert.fired', 'alert.resolved'], (env) => {
  if (env.type === 'alert.fired') {
    const p = env.payload as { id?: number; alert_key?: string; level?: string;
      task_id?: number | null; message?: string; timestamp?: number } | undefined
    if (!p) return
    // fired 载荷是告警字段（非完整行），构造行结构置顶；避免重复（快照重拉兜底）
    if (alerts.value.some((a) => p.id != null && a.id === p.id)) return
    alerts.value.unshift({
      id: p.id ?? 0,
      alert_key: p.alert_key ?? 'unknown',
      level: p.level ?? 'warning',
      task_id: p.task_id ?? null,
      message: p.message ?? '',
      fired_at: new Date(p.timestamp ?? Date.now()).toLocaleString(),
      resolved_at: null,
    })
    if (alerts.value.length > 10) alerts.value.pop()
  } else {
    const p = env.payload as { alert_key?: string } | undefined
    if (!p?.alert_key) return
    const hit = alerts.value.find((a) => a.alert_key === p.alert_key && !a.resolved_at)
    if (hit) hit.resolved_at = new Date().toLocaleString()
  }
})

const typeMap: Record<string, 'error' | 'warning' | 'info' | 'success' | 'default'> = {
  error: 'error',
  critical: 'error',
  warning: 'warning',
  info: 'info',
  success: 'success',
}

function fmtTime(s: string): string {
  const d = new Date(s)
  return Number.isNaN(d.getTime()) ? s : d.toLocaleString()
}

const items = computed(() => alerts.value.map((a) => ({
  a,
  type: typeMap[a.level] ?? 'default' as const,
})))

const message = useMessage()

async function doResolve(a: AlertLog) {
  try {
    await settingsApi.resolveAlert(a.alert_key)
    message.success('已标记处理')
    // WS alert.resolved 会原地标记；本地兜底立即更新
    alerts.value.forEach((x) => {
      if (x.alert_key === a.alert_key && !x.resolved_at) x.resolved_at = new Date().toLocaleString()
    })
  } catch (e) {
    message.error(errMsg(e))
  }
}
</script>

<template>
  <div v-if="items.length === 0" style="color: var(--Semantic-Color-text-tertiary); padding: 8px 0">
    暂无告警记录
  </div>
  <n-timeline v-else>
    <n-timeline-item v-for="it in items" :key="it.a.id" :type="it.type">
      <template #header>
        <span style="font-size: var(--Font-Size-font-micro); color: var(--Semantic-Color-text-tertiary)">
          {{ fmtTime(it.a.fired_at) }}
        </span>
        <n-tag v-if="it.a.resolved_at" size="tiny" :bordered="false" style="margin-left: 8px">
          已解除
        </n-tag>
        <n-tag v-else size="tiny" type="warning" :bordered="false" style="margin-left: 8px">
          未解除
        </n-tag>
        <n-button v-if="!it.a.resolved_at" quaternary size="tiny" type="primary"
          style="margin-left: 8px" @click="doResolve(it.a)">标记已处理</n-button>
      </template>
      <span style="font-size: var(--Font-Size-font-caption)">{{ it.a.message }}</span>
    </n-timeline-item>
  </n-timeline>
</template>
