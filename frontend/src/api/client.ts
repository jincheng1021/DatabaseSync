/** axios 统一封装：401 跳登录、错误结构解析为可读提示 + 修复指引（§2.2 / §5.1） */
import axios from 'axios'

export interface ApiError {
  code: string
  message: string
  hint?: string | null
}

export const http = axios.create({ baseURL: '', withCredentials: true })

let unauthorizedHandler: (() => void) | null = null
export function onUnauthorized(fn: () => void) {
  unauthorizedHandler = fn
}

http.interceptors.response.use(
  (resp) => resp.data,
  (error) => {
    const status = error.response?.status
    const data = error.response?.data
    if (status === 401 && unauthorizedHandler) {
      unauthorizedHandler()
    }
    const apiError: ApiError =
      data?.error ??
      { code: 'NETWORK_ERROR', message: '网络异常或服务不可达', hint: '请确认后端服务已启动（默认 :8080）' }
    return Promise.reject(apiError)
  },
)

export function errMsg(e: unknown): string {
  const err = e as ApiError
  return err?.hint ? `${err.message}（${err.hint}）` : err?.message ?? String(e)
}
