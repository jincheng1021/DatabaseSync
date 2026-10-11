<script setup lang="ts">
/**
 * StatusPill（设计系统核心组件）：状态 = 色点 + 文字 胶囊。
 * 禁止仅用颜色区分（色盲友好）；信号色按设计系统语义映射。
 * 紫 = 操作/焦点（不用作状态）；青 = 数据链路活动；成功/警告/错误 = 信号色。
 */
import { computed } from 'vue'

const props = defineProps<{ state: string; label?: string }>()

const MAP: Record<string, { color: string; text: string }> = {
  draft: { color: 'var(--Semantic-Color-text-tertiary)', text: '草稿' },
  idle: { color: 'var(--Semantic-Color-text-secondary)', text: '空闲' },
  initializing: { color: 'var(--Semantic-Color-info)', text: '初始化中' },
  full_resyncing: { color: 'var(--Semantic-Color-info)', text: '重刷中' },
  backing_up: { color: 'var(--Semantic-Color-info)', text: '备份中' },
  cdc_running: { color: 'var(--Semantic-Color-info)', text: '增量运行中' },
  paused: { color: 'var(--Semantic-Color-warning)', text: '已暂停' },
  ddl_pending: { color: 'var(--Semantic-Color-warning)', text: 'DDL 待决策' },
  error: { color: 'var(--Semantic-Color-error)', text: '异常' },
  needs_reinit: { color: 'var(--Semantic-Color-error)', text: '需重新初始化' },
  // run / 表级状态
  running: { color: 'var(--Semantic-Color-info)', text: '运行中' },
  success: { color: 'var(--Semantic-Color-success)', text: '成功' },
  success_with_mismatch: { color: 'var(--Semantic-Color-warning)', text: '成功（校验不一致）' },
  mismatch: { color: 'var(--Semantic-Color-warning)', text: '校验不一致' },
  cancelled: { color: 'var(--Semantic-Color-text-tertiary)', text: '已取消' },
  failed: { color: 'var(--Semantic-Color-error)', text: '失败' },
  copying: { color: 'var(--Semantic-Color-info)', text: '拷贝中' },
  structure_done: { color: 'var(--Semantic-Color-info)', text: '结构完成' },
  done: { color: 'var(--Semantic-Color-success)', text: '完成' },
  ddl_blocked: { color: 'var(--Semantic-Color-warning)', text: 'DDL 中止' },
  pending: { color: 'var(--Semantic-Color-text-tertiary)', text: '等待' },
  skipped: { color: 'var(--Semantic-Color-text-tertiary)', text: '跳过' },
}

const info = computed(() => MAP[props.state] ?? { color: 'var(--Semantic-Color-text-tertiary)', text: props.state })
const text = computed(() => props.label ?? info.value.text)
</script>

<template>
  <span class="status-pill">
    <span class="dot" :style="{ background: info.color }" />
    <span class="txt">{{ text }}</span>
  </span>
</template>

<style scoped>
.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 2px 10px;
  border-radius: 9999px;
  border: 1px solid var(--Semantic-Color-border);
  background: var(--Semantic-Color-bg-elevated);
}
.dot {
  width: 6px;
  height: 6px;
  border-radius: 9999px;
  flex: none;
}
.txt {
  font-size: var(--Font-Size-font-micro);
  color: var(--Semantic-Color-text-primary);
  white-space: nowrap;
}
</style>
