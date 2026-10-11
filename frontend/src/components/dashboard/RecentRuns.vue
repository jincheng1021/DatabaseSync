<script setup lang="ts">
/**
 * 仪表盘 · 最近运行时间线（§6.2）。
 * 数据：GET /api/runs?page_size=10；WS run.finished 节流重拉（5s）。
 * 状态用 StatusPill 胶囊（色点+文字）；行点击 → 执行历史。
 */
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { runApi, type RunHistory } from '../../api'
import StatusPill from '../StatusPill.vue'
import { useWsEvent } from '../../composables/wsBus'

const router = useRouter()
const runs = ref<RunHistory[]>([])
const lastReload = ref(0)

async function load() {
  try {
    const r = await runApi.list({ page: 1, page_size: 10 })
    runs.value = r.runs
  } catch { /* 降级：保持空态 */ }
}
onMounted(load)

function fmtTime(s: string | null): string {
  if (!s) return '—'
  const d = new Date(s)
  return Number.isNaN(d.getTime()) ? s : d.toLocaleString()
}

const TYPE_LABELS: Record<string, string> = {
  init: '初始化',
  full_resync: '全量重刷',
  cdc: '增量',
  backup: '备份',
  restore: '恢复',
  state_change: '状态迁移',
  verify: '校验',
  orphan_cleanup: '孤儿行清理',
}

useWsEvent(['run.finished'], (env) => {
  const now = Date.now()
  if (now - lastReload.value < 5000) return
  lastReload.value = now
  load()
})

defineExpose({ reload: load })
</script>

<template>
  <div v-if="runs.length === 0" style="color: var(--Semantic-Color-text-tertiary); padding: 8px 0">
    暂无运行记录
  </div>
  <div v-else class="list">
    <div v-for="r in runs" :key="r.id" class="row" @click="router.push({ name: 'runs' })">
      <span class="mono" style="color: var(--Semantic-Color-text-tertiary); font-size: var(--Font-Size-font-micro)">
        #{{ r.id }}
      </span>
      <span style="min-width: 72px">{{ TYPE_LABELS[r.run_type] ?? r.run_type }}</span>
      <StatusPill :state="r.state" />
      <span class="mono" style="margin-left: auto; font-size: var(--Font-Size-font-micro)">
        {{ fmtTime(r.started_at) }}
      </span>
    </div>
  </div>
</template>

<style scoped>
.list {
  display: flex;
  flex-direction: column;
}
.row {
  display: flex;
  align-items: center;
  gap: var(--Spacing-space-2, 8px);
  padding: var(--Spacing-space-2, 8px) 0;
  border-bottom: 1px solid var(--Semantic-Color-border);
  cursor: pointer;
}
.row:last-child {
  border-bottom: none;
}
.row:hover {
  background: var(--Semantic-Color-bg-elevated);
}
</style>
