<script setup lang="ts">
/** 系统设置页（§6.2 页面 11）：主密钥状态 + 全局默认参数只读 + M4 告警通道配置/测试 */
import { onMounted, reactive, ref } from 'vue'
import { NAlert, NButton, NCard, NDataTable, NForm, NFormItem, NInput, NInputNumber,
  NSwitch, NTag, useMessage } from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import { settingsApi } from '../api'
import { errMsg } from '../api/client'

const message = useMessage()
const loading = ref(false)
const defaults = ref<Record<string, number>>({})
const masterKey = ref<{ fingerprint: string; path: string; hint: string } | null>(null)

// ---- M4：告警通道 ----
const chBusy = ref(false)
const testResults = ref<Array<{ channel: string; sent: boolean; reason?: string }>>([])
const webhookUrl = ref('')
const silenceMinutes = ref<number | null>(30)
const smtp = reactive({
  host: '', port: 465, username: '', password: '',
  use_ssl: true, use_tls: false, from: '', to: '',
})

const PARAM_NAMES: Record<string, string> = {
  full_table_concurrency: '全量表级并发数（1~16）',
  full_batch_size: '全量批大小（行/批）',
  cdc_commit_batch_rows: '增量提交批（条）',
  cdc_commit_interval_ms: '增量提交间隔（毫秒）',
  task_rate_limit_rows: '任务级限流（行/秒，0=不限）',
  global_rate_limit_rows: '全局限流总闸（行/秒）',
  queue_capacity: '内存队列容量（事件）',
  big_tx_threshold_rows: '大事务阈值（行）',
  lag_alert_seconds: '延迟告警阈值（秒）',
  reconnect_backoff_max_seconds: '重连退避上限（秒）',
  checksum_chunk_rows: 'checksum 分块（行/块）',
  ftwrl_lock_wait_timeout: '全局锁（FTWRL）等待超时（秒）',
  binlog_retention_warn_hours: 'binlog 保留警告线（小时）',
  alert_silence_minutes: '告警静默期（分钟）',
  log_retention_days: '日志保留（天）',
}

const columns: DataTableColumns<{ key: string; name: string; value: number }> = [
  { title: '参数', key: 'name' },
  { title: '默认值', key: 'value', width: 140, className: 'mono' },
]

onMounted(async () => {
  loading.value = true
  try {
    const s = await settingsApi.get()
    defaults.value = s.global_defaults
    masterKey.value = s.master_key
    // M4：回填告警通道（密码掩码显示）
    webhookUrl.value = s.alert_channels?.webhook_url ?? ''
    silenceMinutes.value = s.alert_channels?.silence_minutes ?? 30
    const conf = (s.alert_channels?.smtp ?? {}) as Record<string, unknown>
    smtp.host = String(conf.host ?? '')
    smtp.port = Number(conf.port ?? 465)
    smtp.username = String(conf.username ?? '')
    smtp.password = conf.password ? '******' : ''
    smtp.use_ssl = Boolean(conf.use_ssl)
    smtp.use_tls = Boolean(conf.use_tls)
    smtp.from = String(conf.from ?? '')
    smtp.to = Array.isArray(conf.to) ? (conf.to as string[]).join(',') : String(conf.to ?? '')
  } catch (e) {
    message.error(errMsg(e))
  } finally {
    loading.value = false
  }
})

async function saveChannels() {
  chBusy.value = true
  try {
    const smtpBody: Record<string, unknown> = {
      host: smtp.host || undefined, port: smtp.port, username: smtp.username || undefined,
      password: smtp.password && smtp.password !== '******' ? smtp.password : undefined,
      use_ssl: smtp.use_ssl, use_tls: smtp.use_tls,
      from: smtp.from || undefined,
      to: smtp.to.split(',').map((s) => s.trim()).filter(Boolean),
    }
    await settingsApi.updateAlertChannels({
      webhook_url: webhookUrl.value,
      smtp: smtp.host ? smtpBody : undefined,
      silence_minutes: silenceMinutes.value ?? undefined,
    })
    message.success('告警通道配置已保存')
    await doTest()
  } catch (e) {
    message.error(errMsg(e))
  } finally {
    chBusy.value = false
  }
}

async function doTest() {
  try {
    const r = await settingsApi.testAlert()
    testResults.value = r.results
    if (r.results.some((x) => x.sent)) message.success('测试发送成功，请查收')
    else message.warning('未触达：请检查通道配置（未配置的通道显示「未配置」）')
  } catch (e) {
    message.error(errMsg(e))
  }
}

const rows = () => Object.entries(defaults.value).map(([key, value]) => ({ key, name: PARAM_NAMES[key] || key, value }))
</script>

<style scoped>
.hint {
  margin-left: 8px;
  font-size: var(--Font-Size-font-caption);
  color: var(--Semantic-Color-text-tertiary);
}
</style>

<template>
  <div class="page">
    <h1 class="page-title">系统设置</h1>

    <n-card title="主密钥状态" style="margin-bottom: 16px">
      <template #header-extra>
        <n-tag size="small" type="success" round :bordered="false">正常</n-tag>
      </template>
      <template v-if="masterKey">
        <div class="mono" style="margin-bottom: 8px">
          指纹：<span style="color: var(--Semantic-Color-info)">{{ masterKey.fingerprint }}</span>
        </div>
        <div class="mono" style="margin-bottom: 8px; color: var(--Semantic-Color-text-secondary)">
          位置：{{ masterKey.path }}
        </div>
        <n-alert type="warning" :bordered="false" style="margin-top: 12px">
          {{ masterKey.hint }}（AES-256-GCM；主密钥丢失将无法解密已存凭据，请务必随数据一并备份）
        </n-alert>
      </template>
    </n-card>

    <n-card title="全局默认参数（只读，任务级可覆盖）" style="margin-bottom: 16px">
      <n-data-table :columns="columns" :data="rows()" :loading="loading" size="small" :bordered="false" :max-height="360" />
    </n-card>

    <n-card title="告警通道（Webhook / 邮件）" style="margin-bottom: 16px">
      <n-form label-placement="left" label-width="150" size="small" style="max-width: 680px">
        <n-form-item label="Webhook 地址">
          <n-input v-model:value="webhookUrl" placeholder="钉钉/企业微信机器人 Webhook URL"
            class="mono" />
        </n-form-item>
        <n-form-item label="SMTP 主机">
          <n-input v-model:value="smtp.host" placeholder="如 smtp.exmail.com（留空=不启用邮件）" />
        </n-form-item>
        <n-form-item label="SMTP 端口">
          <n-input-number v-model:value="smtp.port" :min="1" :max="65535" style="width: 160px" />
          <n-switch v-model:value="smtp.use_ssl" size="small" style="margin-left: 12px" />
          <span class="hint">SSL（465）</span>
          <n-switch v-model:value="smtp.use_tls" size="small" :disabled="smtp.use_ssl"
            style="margin-left: 12px" />
          <span class="hint">STARTTLS（587，与 SSL 互斥）</span>
        </n-form-item>
        <n-form-item label="SMTP 账号">
          <n-input v-model:value="smtp.username" placeholder="发件账号" />
        </n-form-item>
        <n-form-item label="SMTP 密码/授权码">
          <n-input v-model:value="smtp.password" type="password" show-password-on="click"
            placeholder="留空掩码 = 不变更" />
        </n-form-item>
        <n-form-item label="发件人">
          <n-input v-model:value="smtp.from" placeholder="与账号一致通常即可" />
        </n-form-item>
        <n-form-item label="收件人（逗号分隔）">
          <n-input v-model:value="smtp.to" placeholder="ops@example.com, admin@example.com" />
        </n-form-item>
        <n-form-item label="告警静默期（分钟）">
          <n-input-number v-model:value="silenceMinutes" :min="1" :max="10080"
            style="width: 160px" />
          <span class="hint">同告警项在静默期内且未解除时不重发（降噪）</span>
        </n-form-item>
      </n-form>
      <div style="display: flex; gap: 8px; margin-top: 8px">
        <n-button type="primary" size="small" :loading="chBusy" @click="saveChannels">
          保存并测试发送</n-button>
        <n-button size="small" secondary @click="doTest">仅测试发送</n-button>
      </div>
      <n-alert v-for="(r, i) in testResults" :key="i" :type="r.sent ? 'success' : 'warning'"
        :bordered="false" style="margin-top: 8px">
        {{ r.channel.toUpperCase() }}：{{ r.sent ? '发送成功' : `未发送（${r.reason ?? '失败'}）` }}
      </n-alert>
      <n-alert type="info" :bordered="false" style="margin-top: 8px">
        告警项全集：任务失败/自动停止、延迟超阈值、同步位点失效、DDL 待决策、校验不一致超标、备份失败、
        磁盘不足、应用重启恢复、全局锁（FTWRL）取位点超时、备份恢复失败。全部落告警历史（可于顶栏铃铛查看）。
      </n-alert>
    </n-card>
  </div>
</template>
