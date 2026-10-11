/**
 * wsBus：全局单例 WS 连接 + 事件分发（M2：task.progress / task.state_changed / run.finished）。
 * 自动重连 + 心跳 + 重连快照对齐（§5.2-5.3）。
 *
 * F2：项目唯一 WS 通道（原 useWebSocket 双通道已收敛至此）。
 * F1：closedByUser 保护——最后订阅者卸载后 onclose 不再触发重连（原实现会无限僵尸重连）。
 */
import { onUnmounted, readonly, ref } from 'vue'

export type WsStatus = 'connecting' | 'open' | 'closed'

export interface WsEnvelope {
  type: string
  timestamp: number
  payload: Record<string, unknown>
}

export type WsHandler = (env: WsEnvelope) => void

const handlers = new Set<WsHandler>()
const status = ref<WsStatus>('connecting')
let ws: WebSocket | null = null
let closedByUser = false
let retry = 0
let pingTimer: number | null = null
let reconnectTimer: number | null = null
let userCount = 0
let lastPongAt = 0
// P2-14：超过 2 个心跳周期无 pong 视为 TCP 半开（服务端假死/静默断链），主动断开触发重连
const PONG_TIMEOUT_MS = 45000

function connect() {
  status.value = 'connecting'
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  ws = new WebSocket(`${proto}://${location.host}/ws/events`)
  ws.onopen = () => {
    retry = 0
    status.value = 'open'
    lastPongAt = Date.now()
    ws?.send(JSON.stringify({ action: 'subscribe', tasks: [] }))
    pingTimer = window.setInterval(() => {
      if (Date.now() - lastPongAt > PONG_TIMEOUT_MS) {
        ws?.close()   // onclose 走重连
        return
      }
      ws?.send(JSON.stringify({ action: 'ping' }))
    }, 20000)
  }
  ws.onmessage = (ev) => {
    try {
      const env = JSON.parse(ev.data) as WsEnvelope
      if (env.type === 'ws.pong') lastPongAt = Date.now()
      handlers.forEach((h) => h(env))
    } catch { /* 忽略非 JSON 帧 */ }
  }
  ws.onclose = (ev) => {
    if (pingTimer) { clearInterval(pingTimer); pingTimer = null }
    status.value = 'closed'
    if (closedByUser) return           // F1：用户主动断开，禁止重连
    if (ev.code === 4401) return  // 未授权：路由守卫处理
    const delay = Math.min(1000 * 2 ** retry, 60000)
    retry += 1
    reconnectTimer = window.setTimeout(connect, delay)
  }
  ws.onerror = () => ws?.close()
}

/**
 * 组件内调用：注册事件订阅（自动去重卸载）；首个订阅者触发连接。
 * types 为空数组时订阅全部事件（仪表盘用）。
 */
export function useWsEvent(types: string[], handler: WsHandler) {
  userCount += 1
  if (userCount === 1) {
    closedByUser = false
    connect()
  }
  const wrapped: WsHandler = (env) => {
    if (types.length === 0 || env.type === 'ws.snapshot' || types.includes(env.type)) handler(env)
  }
  handlers.add(wrapped)
  onUnmounted(() => {
    handlers.delete(wrapped)
    userCount -= 1
    if (userCount === 0) {
      closedByUser = true
      if (pingTimer) { clearInterval(pingTimer); pingTimer = null }
      if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null }
      if (ws) { ws.onclose = null; ws.close(); ws = null }
    }
  })
}

export function useWsStatus() {
  return { status: readonly(status) }
}
