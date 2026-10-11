<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NButton, NCard, NForm, NFormItem, NInput, useMessage } from 'naive-ui'
import { useSessionStore } from '../stores/session'
import { errMsg } from '../api/client'

const session = useSessionStore()
const route = useRoute()
const router = useRouter()
const message = useMessage()

// initialized=false → 首启 bootstrap 引导页（§6.2 页面 12）
const mode = computed<'bootstrap' | 'login'>(() => (session.initialized === false ? 'bootstrap' : 'login'))

const username = ref('admin')
const password = ref('')
const confirmPassword = ref('')
const loading = ref(false)

async function submit() {
  if (!username.value || !password.value) {
    message.warning('请输入用户名和密码')
    return
  }
  if (mode.value === 'bootstrap' && password.value.length < 8) {
    message.warning('密码长度至少 8 位')
    return
  }
  if (mode.value === 'bootstrap' && password.value !== confirmPassword.value) {
    message.warning('两次输入的密码不一致')
    return
  }
  loading.value = true
  try {
    if (mode.value === 'bootstrap') {
      await session.bootstrap(username.value, password.value)
      message.success('管理员密码设置完成')
    } else {
      await session.login(username.value, password.value)
    }
    router.push((route.query.redirect as string) || '/')
  } catch (e) {
    message.error(errMsg(e))
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div style="height: 100vh; display: flex; align-items: center; justify-content: center; background: var(--Semantic-Color-bg-base)">
    <n-card style="width: 400px" :bordered="true">
      <template #header>
        <div>
          <div style="font-size: var(--Font-Size-font-h2); font-weight: 600">数据库同步工具</div>
          <div style="font-size: var(--Font-Size-font-caption); color: var(--Semantic-Color-text-tertiary); margin-top: 4px">
            {{ mode === 'bootstrap' ? '首次使用，请设置管理员密码' : '请登录管理后台' }}
          </div>
        </div>
      </template>
      <n-form @keyup.enter="submit">
        <n-form-item label="用户名">
          <n-input v-model:value="username" placeholder="admin" :disabled="mode === 'bootstrap'" />
        </n-form-item>
        <n-form-item :label="mode === 'bootstrap' ? '设置密码（至少 8 位）' : '密码'">
          <n-input v-model:value="password" type="password" show-password-on="click" placeholder="密码" />
        </n-form-item>
        <n-form-item v-if="mode === 'bootstrap'" label="确认密码">
          <n-input v-model:value="confirmPassword" type="password" show-password-on="click" placeholder="再次输入密码" />
        </n-form-item>
        <n-button type="primary" block :loading="loading" attr-type="submit" @click="submit">
          {{ mode === 'bootstrap' ? '完成初始化' : '登 录' }}
        </n-button>
        <div v-if="mode === 'bootstrap'" style="margin-top: 12px; font-size: var(--Font-Size-font-caption); color: var(--Semantic-Color-warning)">
          首启引导仅此一次：密码将以 argon2 哈希存储，请妥善保管。
        </div>
      </n-form>
    </n-card>
  </div>
</template>
