<script setup lang="ts">
defineProps<{ model: any; editing?: boolean }>()
const intervals = [1, 5, 10, 20, 30, 60, 120]
</script>

<template>
  <label>摄像头 ID<input v-model.trim="model.id" placeholder="例如 hall-entrance-01"><small class="field-hint">业务 ID 可修改，只能使用英文字母、数字、短横线和下划线。</small></label>
  <label>显示名称<input v-model.trim="model.name" placeholder="例如 营业厅入口"></label>
  <label>RTSP / 本地视频源<input v-model.trim="model.rtsp_url" type="password" :placeholder="editing ? '留空表示保持当前视频源' : 'rtsp://user:password@host/...' "><small class="field-hint">{{ editing ? '为保护凭据，现有地址不回显；填写新地址才会替换。' : '正式环境需由服务器访问该地址；Windows 本机文件路径不能直接使用。' }}</small></label>
  <label>分析子码流<input v-model.trim="model.substream_url" type="password" :placeholder="editing ? '留空表示保持当前子码流' : 'rtsp://user:password@host/substream'"><small class="field-hint">常驻1 FPS分析优先使用子码流；未填写时临时回退主码流。</small></label>
  <label>其他模式检测周期<select v-model.number="model.frame_interval_seconds"><option v-for="seconds in intervals" :key="seconds" :value="seconds">每 {{seconds}} 秒一帧</option></select><small class="field-hint">人流固定每 1 秒、离岗固定每 10 秒、烟火固定每 30 秒；此项仅控制其他模式。</small></label>
  <div class="section-head inline-setting"><div><b>启用摄像头</b><small class="field-hint">关闭后停止该摄像头的采集与检测任务。</small></div><label class="switch"><input v-model="model.enabled" type="checkbox"><span></span></label></div>
</template>
