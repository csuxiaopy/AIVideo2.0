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
    const message = (detail as { message?: unknown }).message
    if (typeof message === 'string') return message
  }
  return fallback
}

export async function api<T = any>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) }
  })
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`
    try { message = errorMessage((await response.json()).detail, message) } catch {}
    throw new Error(message)
  }
  if (response.status === 204) return undefined as T
  return response.json()
}
