<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api, ApiError } from '../api'

const props = defineProps<{ existingIds: string[] }>()
const emit = defineEmits<{ close: []; created: [count: number]; failed: [message: string] }>()

type Row = { row: number; name: string; id: string; rtsp_url: string; status: string; valid: boolean }
const text = ref('')
const submitting = ref(false)
const serverErrors = ref<Record<number, string>>({})
const idPattern = /^[A-Za-z0-9_-]+$/
const sourcePattern = /^(rtsp|rtsps|rtmp|https?|file):\/\//i

function isHeader(columns: string[]): boolean {
  if (columns.length < 2) return false
  const normalized = columns.map(value => value.replace(/\s+/g, '').toLowerCase())
  const nameHeader = ['名称', '摄像头名称', '显示名称', 'name'].includes(normalized[0])
  const idHeader = ['id', '摄像头id', 'cameraid'].includes(normalized[1])
  const sourceHeader = !normalized[2] || ['视频流地址', '视频流路径', 'rtsp地址', '视频源', '地址', 'source'].includes(normalized[2])
  return nameHeader && idHeader && sourceHeader
}

const rows = computed<Row[]>(() => {
  const rawRows = text.value.split(/\r?\n/).filter(line => line.trim())
  const parsed = rawRows.map(line => line.split('\t').map(value => value.trim()))
  if (parsed.length && isHeader(parsed[0])) parsed.shift()
  const counts = new Map<string, number>()
  parsed.forEach(columns => {
    const id = columns[1] || ''
    if (id) counts.set(id, (counts.get(id) || 0) + 1)
  })
  const existing = new Set(props.existingIds)
  return parsed.map((columns, index) => {
    const [name = '', id = '', rtsp_url = ''] = columns
    let status = '可导入'
    if (!name) status = '名称为空'
    else if (!id) status = '摄像头 ID 为空'
    else if (!idPattern.test(id)) status = 'ID 格式错误'
    else if ((counts.get(id) || 0) > 1) status = 'ID 重复'
    else if (existing.has(id)) status = '数据库中已存在该 ID'
    else if (!rtsp_url) status = '视频流地址为空'
    else if (!sourcePattern.test(rtsp_url)) status = '视频流地址格式异常'
    if (serverErrors.value[index + 1]) status = serverErrors.value[index + 1]
    return { row: index + 1, name, id, rtsp_url, status, valid: status === '可导入' }
  })
})

watch(text, () => { serverErrors.value = {} })
const validCount = computed(() => rows.value.filter(row => row.valid).length)
const invalidCount = computed(() => rows.value.length - validCount.value)
const canSubmit = computed(() => rows.value.length > 0 && rows.value.length <= 500 && invalidCount.value === 0 && !submitting.value)

function maskedSource(source: string): string {
  return source.replace(/(\/\/[^/:@]+:)[^@]*(?=@)/, '$1****')
}

async function submit() {
  if (!canSubmit.value) return
  submitting.value = true
  try {
    const result = await api<{ created: number }>('/api/cameras/batch', {
      method: 'POST',
      body: JSON.stringify({ items: rows.value.map(({ id, name, rtsp_url }) => ({ id, name, rtsp_url })) }),
    })
    emit('created', result.created)
  } catch (error) {
    if (error instanceof ApiError && error.detail && typeof error.detail === 'object') {
      const errors = (error.detail as { errors?: Array<{ row: number; message: string }> }).errors || []
      serverErrors.value = Object.fromEntries(errors.filter(item => item.row > 0).map(item => [item.row, item.message]))
    }
    emit('failed', error instanceof Error ? error.message : '批量创建失败')
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="modal batch-modal-wrap" role="dialog" aria-modal="true" @click.self="emit('close')">
    <section class="batch-modal">
      <header class="modal-head"><div><h2>批量添加视频源</h2><p>可直接从 Excel 复制“名称、ID、视频流地址”三列数据粘贴到下方。</p></div><button type="button" aria-label="关闭" @click="emit('close')">×</button></header>
      <textarea v-model="text" class="batch-input" spellcheck="false" placeholder="摄像头名称&#9;摄像头ID&#9;视频流地址&#10;员工工位01&#9;camera001&#9;rtsp://admin:password@192.168.1.101:554/xxx&#10;员工工位02&#9;camera002&#9;rtsp://admin:password@192.168.1.102:554/xxx"></textarea>
      <div class="batch-summary"><span>共解析 <b>{{ rows.length }}</b> 条</span><span class="green">可创建 <b>{{ validCount }}</b> 条</span><span :class="{ 'error-text': invalidCount }">异常 <b>{{ invalidCount }}</b> 条</span><span v-if="rows.length > 500" class="error-text">单次最多 500 条</span></div>
      <div class="batch-table-wrap">
        <table><thead><tr><th>序号</th><th>名称</th><th>摄像头 ID</th><th>视频流地址</th><th>状态</th></tr></thead><tbody><tr v-for="row in rows" :key="row.row" :class="{ 'batch-invalid': !row.valid }"><td>{{ row.row }}</td><td>{{ row.name || '—' }}</td><td>{{ row.id || '—' }}</td><td class="batch-source" :title="row.rtsp_url">{{ maskedSource(row.rtsp_url) || '—' }}</td><td><span :class="row.valid ? 'batch-ok' : 'error-text'">{{ row.status }}</span></td></tr></tbody></table>
        <div v-if="!rows.length" class="empty">粘贴 Excel 数据后将在这里预览</div>
      </div>
      <footer class="batch-actions"><button class="ghost" :disabled="submitting" @click="emit('close')">取消</button><button class="primary" :disabled="!canSubmit" @click="submit">{{ submitting ? '正在创建…' : '批量创建' }}</button></footer>
    </section>
  </div>
</template>
