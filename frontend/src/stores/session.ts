/** 会话 store：登录态、首启探测 */
import { defineStore } from 'pinia'
import { authApi } from '../api'

export const useSessionStore = defineStore('session', {
  state: () => ({
    initialized: true as boolean | null,
    username: '' as string | null,
    checked: false,
  }),
  actions: {
    async probe() {
      try {
        const s = await authApi.status()
        this.initialized = s.initialized
        this.username = s.authenticated ? (await authApi.me()).username : null
      } catch {
        this.initialized = null
        this.username = null
      }
      this.checked = true
    },
    async login(username: string, password: string) {
      await authApi.login(username, password)
      this.username = username
    },
    async bootstrap(username: string, password: string) {
      await authApi.bootstrap(username, password)
      this.initialized = true
      this.username = username
    },
    async logout() {
      try { await authApi.logout() } catch { /* 忽略 */ }
      this.username = null
    },
  },
})
