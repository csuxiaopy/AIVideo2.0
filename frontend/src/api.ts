const fieldNames: Record<string, string> = {
  id: '摄像头 ID',
  name: '摄像头名称',
  rtsp_url: '视频源',
  frame_interval_seconds: '抽帧频率',
  modes: '检测模式',
  geometry: '检测区域',
  base_url: 'Base URL',
  api_key: 'API Key',
  economy_model: '经济模型',
  enhanced_model: '增强模型'
}

function errorMessage(detail: unknown, fallback: string): string {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    const messages = detail.map((item: any) => {
      const field = Array.isArray(item?.loc)
        ? item.loc.filter((part: unknown) => part !== 'body').map((part: unknown) => fieldNames[String(part)] || String(part)).join(' → ')
        : ''
      const message = typeof item?.msg === 'string' ? item.msg.replace(/^Value error,\s*/i, '') : '内容不符合要求'
      return field ? `${field}：${message}` : message
    }).filter(Boolean)
    return messages.join('；') || fallback
  }
  if (detail && typeof detail === 'object') {
    const errors = (detail as { errors?: unknown }).errors
    if (Array.isArray(errors)) {
      const messages = errors.map((item: any) => `第 ${item.row || '?'} 行：${item.message || '数据错误'}`)
      return messages.join('；') || fallback
    }
    const message = (detail as { message?: unknown }).message
    if (typeof message === 'string') return message
  }
  return fallback
}

export class ApiError extends Error {
  constructor(message: string, public detail: unknown, public status: number) {
    super(message)
  }
}

let csrfToken = ''
let unauthorizedHandler: (() => void) | undefined

export function setCsrfToken(value: string) { csrfToken = value }
export function setUnauthorizedHandler(handler: () => void) { unauthorizedHandler = handler }

export async function api<T = any>(path: string, options: RequestInit = {}): Promise<T> {
  const method = (options.method || 'GET').toUpperCase()
  const headers = new Headers(options.headers)
  if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method) && csrfToken) headers.set('X-CSRF-Token', csrfToken)
  const response = await fetch(path, {
    ...options,
    credentials: 'same-origin',
    headers
  })
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`
    let detail: unknown
    try { detail = (await response.json()).detail; message = errorMessage(detail, message) } catch {}
    if (response.status === 401 && path !== '/api/auth/login') unauthorizedHandler?.()
    throw new ApiError(message, detail, response.status)
  }
  if (response.status === 204) return undefined as T
  return response.json()
}
