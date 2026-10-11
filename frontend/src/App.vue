<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { NConfigProvider, NDialogProvider, NGlobalStyle, NMessageProvider, darkTheme } from 'naive-ui'
import { useThemeStore } from './stores/theme'
import { buildThemeOverrides } from './theme'
import { onUnauthorized } from './api/client'
import router from './router'

const themeStore = useThemeStore()
const overrides = ref(buildThemeOverrides())

// 明暗切换后 tokens.css 变量变化 → 重建主题映射
watch(() => themeStore.dark, () => {
  // 等 data-theme 生效后再读取变量
  requestAnimationFrame(() => { overrides.value = buildThemeOverrides() })
})

const naiveTheme = computed(() => (themeStore.dark ? darkTheme : null))

onUnauthorized(() => router.push({ name: 'login' }))
</script>

<template>
  <n-config-provider :theme="naiveTheme" :theme-overrides="overrides" style="height: 100%">
    <n-global-style />
    <n-dialog-provider>
      <n-message-provider>
        <router-view />
      </n-message-provider>
    </n-dialog-provider>
  </n-config-provider>
</template>
