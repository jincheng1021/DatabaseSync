import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import { useThemeStore } from './stores/theme'
import './styles/tokens.css'
import './styles/global.css'

const app = createApp(App)
app.use(createPinia())

useThemeStore().apply() // 暗色默认，先于挂载设置 data-theme

app.use(router)
app.mount('#app')
