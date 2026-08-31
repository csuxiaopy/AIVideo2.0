<script setup lang="ts">
import { computed } from 'vue'
import { ICONS } from '../icons'

const props = withDefaults(defineProps<{ name: string; size?: number }>(), { size: 16 })

const body = computed(() => ICONS[props.name] ?? ICONS.search)
</script>

<template>
  <svg
    class="tech-icon"
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.8"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
    v-html="body"
  />
</template>

<style scoped>
.tech-icon{flex:none;display:inline-block;vertical-align:-.15em}
</style>
