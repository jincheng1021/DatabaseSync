<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterView, useRoute, useRouter } from 'vue-router'
import { NAlert, NBadge, NButton, NDropdown, NIcon, NLayout, NLayoutContent, NLayoutHeader, NLayoutSider, NMenu, NPopover, NTag, useDialog, useMessage } from 'naive-ui'
import { useSessionStore } from '../stores/session'
import { useThemeStore } from '../stores/theme'
import { useWsEvent, useWsStatus } from '../composables/wsBus'
import { settingsApi, type AlertLog } from '../api'
import { errMsg } from '../api/client'

const route = useRoute()
const router = useRouter()
const session = useSessionStore()
const theme = useThemeStore()
const dialog = useDialog()
const message = useMessage()

// ---- M4：告警铃铛（未读数 + 最近告警下拉） ----
const alerts = ref<AlertLog[]>([])
const unread = ref(0)
const bellLoading = ref(false)

async function loadAlerts() {
  bellLoading.value = true
  try {
    const r = await settingsApi.alerts(30)
    alerts.value = r.items
    unread.value = r.items.filter((a) => !a.resolved_at).length
  } catch { /* 非阻塞 */ } finally {
    bellLoading.value = false
  }
}
onMounted(loadAlerts)

async function doResolveAlert(a: AlertLog) {
  try {
    await settingsApi.resolveAlert(a.alert_key)
    message.success('已标记处理')
    await loadAlerts()
  } catch (e) {
    message.error(errMsg(e))
  }
}

useWsEvent(['alert.fired', 'alert.resolved'], (env) => {
  void loadAlerts()
  if (env.type === 'alert.fired') message.warning('收到新告警，请查看铃铛通知')
})

function levelTagType(level: string): 'error' | 'warning' | 'info' {
  return level === 'error' ? 'error' : level === 'info' ? 'info' : 'warning'
}


// WS 通道状态（M1：顶栏断线横幅 + 重连中提示）。F2：统一走 wsBus 单例，不再另开连接
const { status: wsStatus } = useWsStatus()
const wsClosed = computed(() => wsStatus.value !== 'open')

const menuOptions = [
  { label: '仪表盘', key: 'dashboard' },
  { label: '数据源管理', key: 'datasources' },
  { label: '同步任务', key: 'tasks' },
  { label: '备份管理', key: 'backups' },
  { label: '执行历史', key: 'runs' },
  { label: '校验报告', key: 'verify' },
  { label: '系统设置', key: 'settings' },
]

const activeKey = computed(() => (route.name as string) || 'dashboard')

const userOptions = [
  { label: '退出登录', key: 'logout' },
]

function onMenuSelect(key: string) {
  router.push({ name: key })
}

function onUserSelect(key: string) {
  if (key === 'logout') {
    dialog.warning({
      title: '退出登录',
      content: '确定要退出当前会话吗？',
      positiveText: '退出',
      negativeText: '取消',
      onPositiveClick: async () => {
        await session.logout()
        router.push({ name: 'login' })
      },
    })
  }
}

function toggleTheme() {
  theme.toggle()
  message.info(theme.dark ? '已切换到暗色模式' : '已切换到亮色模式')
}

const collapsed = ref(false)
</script>

<template>
  <n-layout style="height: 100vh">
    <n-layout-header bordered style="height: 56px; display: flex; align-items: center; padding: 0 24px; gap: 16px; background: var(--Semantic-Color-bg-card)">
      <div style="font-weight: 600; font-size: var(--Font-Size-font-h3); color: var(--Semantic-Color-accent)">
        数据库同步工具
      </div>
      <div style="flex: 1" />
      <!-- 告警铃铛（未读数 + 最近告警列表） -->
      <n-popover trigger="click" placement="bottom-end" style="max-width: 420px"
        @update:show="(v: boolean) => v && loadAlerts()">
        <template #trigger>
          <n-badge :value="unread" :max="99" :offset="[-4, 4]">
            <n-button quaternary size="small" :loading="bellLoading" title="告警通知">🔔</n-button>
          </n-badge>
        </template>
        <div style="max-height: 380px; overflow: auto; min-width: 360px">
          <div style="font-weight: 600; margin-bottom: 8px; color: var(--Semantic-Color-text-primary)">
            告警通知</div>
          <div v-if="!alerts.length" style="color: var(--Semantic-Color-text-tertiary); padding: 8px 0">
            暂无告警记录</div>
          <div v-for="a in alerts" :key="a.id"
            style="padding: 8px 0; border-bottom: 1px solid var(--Semantic-Color-border)">
            <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 2px">
              <n-tag size="tiny" :bordered="false" :type="levelTagType(a.level)">
                {{ a.level === 'error' ? '错误' : a.level === 'info' ? '提示' : '警告' }}</n-tag>
              <span class="mono" style="font-size: var(--Font-Size-font-micro); color: var(--Semantic-Color-text-tertiary)">
                {{ a.fired_at }}</span>
              <span v-if="!a.resolved_at" style="font-size: var(--Font-Size-font-micro); color: var(--Semantic-Color-warning)">未解除</span>
              <span v-else style="font-size: var(--Font-Size-font-micro); color: var(--Semantic-Color-success)">已解除</span>
              <n-button v-if="!a.resolved_at" quaternary size="tiny" type="primary"
                style="margin-left: auto" @click="doResolveAlert(a)">标记已处理</n-button>
            </div>
            <div style="font-size: var(--Font-Size-font-caption); color: var(--Semantic-Color-text-secondary)">
              {{ a.message }}</div>
          </div>
        </div>
      </n-popover>
      <n-button quaternary size="small" @click="toggleTheme">
        {{ theme.dark ? '☀ 亮色' : '☾ 暗色' }}
      </n-button>
      <n-dropdown :options="userOptions" @select="onUserSelect">
        <n-button quaternary size="small">
          {{ session.username || '未登录' }} ▾
        </n-button>
      </n-dropdown>
    </n-layout-header>
    <n-layout has-sider style="height: calc(100vh - 56px)">
      <n-layout-sider
        v-model:collapsed="collapsed"
        collapse-mode="width"
        :collapsed-width="64"
        :width="200"
        bordered
        :native-scrollbar="false"
      >
        <n-menu :options="menuOptions" :value="activeKey" :collapsed="collapsed" :collapsed-width="64" @update:value="onMenuSelect" />
      </n-layout-sider>
      <n-layout-content :native-scrollbar="false">
        <n-alert v-if="wsClosed" type="warning" style="margin: 12px 24px 0" :bordered="false">
          实时连接已断开，正在重连…（页面数据可能不是最新）
        </n-alert>
        <router-view />
      </n-layout-content>
    </n-layout>
  </n-layout>
</template>
