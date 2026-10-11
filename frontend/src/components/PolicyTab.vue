<script setup lang="ts">
/**
 * 任务详情 · 策略页签（T4.6 / §6.2 页面 4「策略」页签）：
 * DDL 策略展示、任务级限流、攒批参数、安全网/孤儿清理开关 + 手动孤儿清理 / 数据校验入口。
 */
import { onMounted, ref } from 'vue'
import {
  NAlert, NButton, NCard, NForm, NFormItem, NInputNumber, NRadioButton, NRadioGroup,
  NSwitch, useDialog, useMessage,
} from 'naive-ui'
import { taskApi, verifyApi, type SyncTask } from '../api'
import { errMsg } from '../api/client'

const props = defineProps<{ taskId: number; task: SyncTask | null }>()
const emit = defineEmits<{ (e: 'saved'): void }>()
const message = useMessage()
const dialog = useDialog()

const busy = ref(false)
const form = ref({
  rate_limit_rows: 0,
  batch_size: 5000,
  batch_interval_ms: 0,
  safety_net_backup: true,
  orphan_cleanup: true,
})

onMounted(() => {
  const t = props.task
  if (t) {
    form.value = {
      rate_limit_rows: t.rate_limit_rows ?? 0,
      batch_size: t.batch_size ?? 5000,
      batch_interval_ms: t.batch_interval_ms ?? 0,
      safety_net_backup: t.safety_net_backup ?? true,
      orphan_cleanup: t.orphan_cleanup ?? true,
    }
  }
})

async function save() {
  busy.value = true
  try {
    await taskApi.update(props.taskId, { ...form.value })
    message.success('策略已保存')
    emit('saved')
  } catch (e) {
    message.error(errMsg(e))
  } finally {
    busy.value = false
  }
}

function doOrphanCleanup() {
  dialog.warning({
    title: '手动孤儿行清理',
    content: '将按主键归并比对删除「目标有源无」行；行过滤子集表自动跳过。确认执行？',
    positiveText: '发起清理', negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await taskApi.orphanCleanup(props.taskId)
        message.success('孤儿行清理已发起，可在「执行历史」页查看进度')
      } catch (e) {
        message.error(errMsg(e))
      }
    },
  })
}

async function doVerify(method: 'row_count' | 'checksum') {
  try {
    await verifyApi.start(props.taskId, method)
    message.success(`数据校验已发起（${method === 'checksum' ? 'checksum 分块' : '行数'}），报告见「校验报告」页`)
  } catch (e) {
    message.error(errMsg(e))
  }
}
</script>

<template>
  <n-card title="运行策略" style="border-radius: var(--Radius-radius-lg)">
    <n-form label-placement="left" label-width="180" size="small" style="max-width: 640px">
      <n-form-item label="任务级限流（行/秒）">
        <n-input-number v-model:value="form.rate_limit_rows" :min="0" :max="1000000" :step="1000"
          style="width: 220px" />
        <span class="hint">0 = 不限；同时受全局限流总闸约束</span>
      </n-form-item>
      <n-form-item label="全量批大小（行/批）">
        <n-input-number v-model:value="form.batch_size" :min="100" :max="100000" :step="500"
          style="width: 220px" />
      </n-form-item>
      <n-form-item label="攒批间隔（毫秒）">
        <n-input-number v-model:value="form.batch_interval_ms" :min="0" :max="10000" :step="100"
          style="width: 220px" />
        <span class="hint">0 = 攒满即提交</span>
      </n-form-item>
      <n-form-item label="安全网（重刷前自动备份）">
        <n-switch v-model:value="form.safety_net_backup" />
        <span class="hint">全量重刷前自动备份目标库涉及表</span>
      </n-form-item>
      <n-form-item label="孤儿行清理">
        <n-switch v-model:value="form.orphan_cleanup" />
        <span class="hint">关闭后手动/定时清理均拒绝执行；行过滤子集表始终自动禁用</span>
      </n-form-item>
      <n-form-item label="DDL 策略">
        <n-radio-group :value="task?.ddl_policy ?? 'intercept'" disabled>
          <n-radio-button value="intercept">拦截待决策</n-radio-button>
          <n-radio-button value="execute">自动执行</n-radio-button>
          <n-radio-button value="skip">自动跳过</n-radio-button>
        </n-radio-group>
        <span class="hint">拦截期间源库 DDL 将进入「DDL 决策」流程，逐条确认执行或跳过</span>
      </n-form-item>
    </n-form>
    <div style="margin-top: 12px; display: flex; gap: 8px">
      <n-button size="small" type="primary" :loading="busy" @click="save">保存策略</n-button>
      <n-button size="small" secondary @click="doOrphanCleanup">手动孤儿行清理</n-button>
      <n-button size="small" secondary @click="doVerify('row_count')">行数校验</n-button>
      <n-button size="small" secondary @click="doVerify('checksum')">checksum 校验</n-button>
    </div>
    <n-alert v-if="form.orphan_cleanup === false" type="warning" :bordered="false" style="margin-top: 12px">
      孤儿行清理已关闭：全量重刷将保留目标库多余行（含已删除数据），定时清理动作也会被拒绝执行。
    </n-alert>
  </n-card>
</template>

<style scoped>
.hint {
  margin-left: 12px;
  font-size: var(--Font-Size-font-caption);
  color: var(--Semantic-Color-text-tertiary);
}
</style>
