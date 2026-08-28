<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'

const props = defineProps<{ src?: string | null; alt?: string }>()

const MIN_SCALE = 0.5
const MAX_SCALE = 5

const open = ref(false)
const scale = ref(1)
const translateX = ref(0)
const translateY = ref(0)
const thumbError = ref(false)
const lightboxError = ref(false)
const dragging = ref(false)
let dragStartX = 0
let dragStartY = 0
let originX = 0
let originY = 0

const openLightbox = (event: MouseEvent) => {
  event.stopPropagation()
  if (!props.src || thumbError.value) return
  scale.value = 1
  translateX.value = 0
  translateY.value = 0
  lightboxError.value = false
  open.value = true
}

const close = () => {
  open.value = false
}

const onKeydown = (event: KeyboardEvent) => {
  if (event.key === 'Escape') close()
}

watch(open, (value) => {
  if (value) {
    document.body.style.overflow = 'hidden'
    window.addEventListener('keydown', onKeydown)
  } else {
    document.body.style.overflow = ''
    window.removeEventListener('keydown', onKeydown)
  }
})

onBeforeUnmount(() => {
  if (open.value) document.body.style.overflow = ''
  window.removeEventListener('keydown', onKeydown)
})

const clampScale = (value: number) => Math.min(MAX_SCALE, Math.max(MIN_SCALE, value))
const zoomBy = (factor: number) => {
  scale.value = clampScale(scale.value * factor)
}
const resetView = () => {
  scale.value = 1
  translateX.value = 0
  translateY.value = 0
}

const onWheel = (event: WheelEvent) => {
  zoomBy(event.deltaY < 0 ? 1.15 : 1 / 1.15)
}

const onPointerDown = (event: PointerEvent) => {
  dragging.value = true
  dragStartX = event.clientX
  dragStartY = event.clientY
  originX = translateX.value
  originY = translateY.value
  const target = event.currentTarget as HTMLElement | null
  target?.setPointerCapture?.(event.pointerId)
}
const onPointerMove = (event: PointerEvent) => {
  if (!dragging.value) return
  translateX.value = originX + event.clientX - dragStartX
  translateY.value = originY + event.clientY - dragStartY
}
const onPointerUp = () => {
  dragging.value = false
}
</script>

<template>
  <span class="evidence-cell" @click.stop>
    <button
      v-if="src && !thumbError"
      type="button"
      class="evidence-thumb"
      :aria-label="alt || '查看证据大图'"
      :title="alt || '点击查看大图'"
      @click="openLightbox"
    >
      <img
        class="evidence-thumb-img"
        :src="src"
        :alt="alt || '告警证据'"
        loading="lazy"
        draggable="false"
        @error="thumbError = true"
      >
    </button>
    <span v-else class="evidence-none">暂无证据</span>

    <Teleport to="body">
      <div v-if="open" class="lightbox" role="dialog" aria-modal="true" @click.self="close">
        <div class="lightbox-toolbar">
          <span class="lightbox-scale">{{ Math.round(scale * 100) }}%</span>
          <button type="button" aria-label="放大" @click.stop="zoomBy(1.25)">＋</button>
          <button type="button" aria-label="缩小" @click.stop="zoomBy(1 / 1.25)">－</button>
          <button type="button" class="reset" @click.stop="resetView">100%</button>
          <button type="button" class="close" aria-label="关闭预览" @click.stop="close">×</button>
        </div>
        <img
          v-if="src && !lightboxError"
          class="lightbox-img"
          :class="{ dragging }"
          :src="src"
          :alt="alt || '告警证据'"
          :style="{ transform: `translate(${translateX}px, ${translateY}px) scale(${scale})` }"
          draggable="false"
          @wheel.prevent="onWheel"
          @pointerdown="onPointerDown"
          @pointermove="onPointerMove"
          @pointerup="onPointerUp"
          @pointercancel="onPointerUp"
          @error="lightboxError = true"
        >
        <div v-else class="lightbox-error">
          <b>证据图片加载失败</b>
          <p>文件可能已过期清理，可关闭后刷新列表重试。</p>
        </div>
        <p class="lightbox-tip">滚轮缩放 · 拖动查看 · 按 ESC 或点击空白处关闭</p>
      </div>
    </Teleport>
  </span>
</template>

<style scoped>
.evidence-cell{display:inline-flex;align-items:center}
.evidence-thumb{display:block;width:80px;height:50px;padding:0;overflow:hidden;cursor:pointer;background:#f6f9fc;border:1px solid var(--line);border-radius:6px;transition:transform .15s,box-shadow .15s,border-color .15s}
.evidence-thumb:hover{transform:translateY(-1px);border-color:#9bc9ed;box-shadow:0 5px 14px #24587833}
.evidence-thumb-img{width:100%;height:100%;object-fit:cover;display:block;pointer-events:none}
.evidence-none{color:var(--muted);font-size:10px}

.lightbox{position:fixed;inset:0;z-index:60;background:#0b2f50b3;backdrop-filter:blur(6px);display:grid;place-items:center;padding:24px}
.lightbox-img{max-width:90vw;max-height:90vh;object-fit:contain;border-radius:6px;box-shadow:0 25px 75px #0007;user-select:none;-webkit-user-drag:none;touch-action:none;cursor:grab}
.lightbox-img.dragging{cursor:grabbing;transition:none}
.lightbox-toolbar{position:fixed;top:18px;right:20px;z-index:3;display:flex;gap:6px;align-items:center}
.lightbox-toolbar button{background:#ffffffe6;color:#17324d;border:1px solid #cbdcea;padding:6px 10px;font-size:13px;border-radius:7px;line-height:1}
.lightbox-toolbar button:hover{filter:brightness(.96);border-color:#9bc9ed}
.lightbox-toolbar .reset{font-size:11px;font-variant-numeric:tabular-nums}
.lightbox-toolbar .close{font-size:17px;padding:6px 12px;color:#fff;background:#e34d59;border-color:#e34d59}
.lightbox-scale{background:#ffffffe6;color:#17324d;border:1px solid #cbdcea;padding:6px 10px;border-radius:7px;font-size:11px;font-variant-numeric:tabular-nums;min-width:46px;text-align:center}
.lightbox-error{background:#fff;border:1px solid #edb8bd;color:#98343c;border-radius:9px;padding:22px 30px;text-align:center}
.lightbox-error b{font-size:14px}
.lightbox-error p{margin:8px 0 0;font-size:12px;color:#7d91a4}
.lightbox-tip{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);margin:0;color:#eaf3fc;background:#062c4dcc;border:1px solid #1786d7;padding:6px 13px;border-radius:20px;font-size:11px;pointer-events:none}
</style>
