/** 路由：登录页独立，其余挂在主布局下；未登录跳登录 */
import { createRouter, createWebHistory } from 'vue-router'
import { useSessionStore } from '../stores/session'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: () => import('../views/LoginView.vue'), meta: { public: true } },
    {
      path: '/',
      component: () => import('../layouts/AppLayout.vue'),
      children: [
        { path: '', name: 'dashboard', component: () => import('../views/DashboardView.vue') },
        { path: 'datasources', name: 'datasources', component: () => import('../views/DataSourcesView.vue') },
        { path: 'tasks', name: 'tasks', component: () => import('../views/TasksView.vue') },
        { path: 'tasks/new', name: 'task-wizard', component: () => import('../views/TaskWizardView.vue') },
        { path: 'tasks/:id/progress', name: 'task-progress', component: () => import('../views/TaskProgressView.vue') },
        { path: 'tasks/:id/detail', name: 'task-detail', component: () => import('../views/TaskDetailView.vue') },
        { path: 'backups', name: 'backups', component: () => import('../views/BackupsView.vue') },
        { path: 'runs', name: 'runs', component: () => import('../views/RunsView.vue') },
        { path: 'verify', name: 'verify', component: () => import('../views/VerifyView.vue') },
        { path: 'settings', name: 'settings', component: () => import('../views/SettingsView.vue') },
        // M4 陆续挂载：校验报告 / 调度配置
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.beforeEach(async (to) => {
  const session = useSessionStore()
  if (!session.checked) await session.probe()
  if (to.meta.public) {
    // 已登录访问登录页 → 回首页
    if (session.username) return { name: 'dashboard' }
    return true
  }
  if (!session.username) {
    return { name: 'login', query: to.fullPath !== '/' ? { redirect: to.fullPath } : {} }
  }
  return true
})

export default router
