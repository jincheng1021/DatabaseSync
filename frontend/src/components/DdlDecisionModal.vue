<script setup lang="ts">
/**
 * DDL 决策弹窗（T3.9 / §6.2 页面 7 / E12）：
 * DDL 原文（等宽代码块）、来源位点、影响表；执行/跳过 + 「同类 DDL 记住选择」。
 */
import { ref, watch } from 'vue'
import { NButton, NCheckbox, NModal, NSpace, NTag, useMessage } from 'naive-ui'
import type { DdlPendingItem } from '../api'
import { taskApi } from '../api'
import { errMsg } from '../api/client'

const props = defineProps<{
  show: boolean
  taskId: number
  items: DdlPendingItem[]
}>()
const emit = defineEmits<{
  (e: 'update:show', v: boolean): void
  (e: 'decided'): void
}>()

const message = useMessage()
const remember = ref(false)
const busy = ref(false)

watch(() => props.show, (v) => {
  if (v) remember.value = false
})

const first = () => props.items[0] ?? null

async function decide(decision: 'execute' | 'skip') {
  if (!props.taskId || !props.items.length) return
  busy.value = true
  try {
    const r = await taskApi.ddlDecision(props.taskId, decision, remember.value)
    if (decision === 'execute') {
      message.success(r.path === 'cdc'
        ? `已在目标库执行 ${r.applied ?? 0} 条 DDL，增量已恢复`
        : 'DDL 已执行，受影响表重做中')
    } else {
      message.success('已跳过该 DDL，同步继续')
    }
    emit('decided')
    emit('update:show', false)
  } catch (e) {
    message.error(errMsg(e))
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <n-modal :show="show" @update:show="emit('update:show', $event)">
    <div class="ddl-modal">
      <h3 style="margin: 0 0 12px; font-size: var(--Font-Size-font-h3)">DDL 待决策</h3>
      <p class="hint">
        源库检测到 DDL 语句（默认拦截）。请选择在目标库<b>执行</b>或<b>跳过</b>；
        同步将在决策后继续。
      </p>

      <div class="field">
        <div class="label">DDL 原文</div>
        <pre class="code">{{ first()?.ddl_text }}</pre>
      </div>

      <div class="field">
        <div class="label">来源同步位点</div>
        <span class="mono pos">{{ first()?.source_position || '—' }}</span>
      </div>

      <div class="field">
        <div class="label">影响表（{{ items.length }} 条待决策）</div>
        <n-space size="small">
          <n-tag v-for="t in first()?.affected_tables ?? []" :key="t" size="small" :bordered="false">
            <span class="mono">{{ t }}</span>
          </n-tag>
        </n-space>
      </div>

      <n-checkbox v-model:checked="remember" style="margin: 4px 0 16px">
        本任务后续同类 DDL 记住此选择（重启后仍生效）
      </n-checkbox>

      <n-space justify="end">
        <n-button :disabled="busy" @click="emit('update:show', false)">稍后处理</n-button>
        <n-button type="warning" secondary :loading="busy" @click="decide('skip')">
          跳过并继续
        </n-button>
        <n-button type="primary" :loading="busy" @click="decide('execute')">
          目标库执行并继续
        </n-button>
      </n-space>
    </div>
  </n-modal>
</template>

<style scoped>
.ddl-modal {
  width: 640px;
  max-width: calc(100vw - 48px);
  background: var(--Semantic-Color-bg-elevated);
  border: 1px solid var(--Semantic-Color-border);
  border-radius: var(--Radius-radius-xl);
  padding: 24px;
}
.hint {
  margin: 0 0 16px;
  font-size: var(--Font-Size-font-caption);
  color: var(--Semantic-Color-text-secondary);
}
.field { margin-bottom: 14px; }
.label {
  font-size: var(--Font-Size-font-micro);
  color: var(--Semantic-Color-text-tertiary);
  margin-bottom: 6px;
}
.code {
  margin: 0;
  padding: 12px;
  background: var(--Semantic-Color-bg-base);
  border: 1px solid var(--Semantic-Color-border);
  border-radius: var(--Radius-radius-md);
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: var(--Font-Size-font-mono-body);
  color: var(--Semantic-Color-info);   /* 数据链路青：DDL 代码 */
  white-space: pre-wrap;
  word-break: break-all;
}
.pos {
  font-size: var(--Font-Size-font-mono-body);
  color: var(--Semantic-Color-info);
}
</style>
