/** 主题 store：暗色默认，明暗切换（切换即改 <html data-theme>，tokens.css 变量自动生效） */
import { defineStore } from 'pinia'

export const useThemeStore = defineStore('theme', {
  state: () => ({ dark: localStorage.getItem('dsync-theme') !== 'light' }),
  actions: {
    apply() {
      document.documentElement.setAttribute('data-theme', this.dark ? 'dark' : 'light')
      localStorage.setItem('dsync-theme', this.dark ? 'dark' : 'light')
    },
    toggle() {
      this.dark = !this.dark
      this.apply()
    },
  },
})
