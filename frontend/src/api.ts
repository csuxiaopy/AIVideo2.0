export async function api<T = any>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) }
  })
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`
    try { message = (await response.json()).detail || message } catch {}
    throw new Error(message)
  }
  if (response.status === 204) return undefined as T
  return response.json()
}

