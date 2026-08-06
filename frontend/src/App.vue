<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref } from 'vue'

type Mode = 'black_screen'|'off_duty'|'on_duty'|'people_flow'|'phone_use'|'smoking'|'fire_smoke'|'intrusion'
type SceneType = 'workstation'|'customer_area'|'security_area'|'custom'
type Point = [number, number]
type DrawLayer = 'post_roi'|'flow_line'|'intrusion_zone'

interface Camera {
  id:string; name:string; source:string; enabled:boolean; online:boolean; last_error?:string
  scene_type:SceneType; modes:Mode[]; geometry:any; schedule:any; options:any
}
interface Template { scene_type:SceneType; name:string; description:string; modes:Mode[]; schedule:any; options:any; required_geometry:string[] }

const modeInfo:Record<Mode,{name:string;icon:string;note:string}> = {
  off_duty:{name:'离岗检测',icon:'◷',note:'排班内持续无人'},
  phone_use:{name:'玩手机检测',icon:'▣',note:'本地候选＋云端复核'},
  people_flow:{name:'人员计数',icon:'⇄',note:'跨线进出统计'},
  fire_smoke:{name:'烟火检测',icon:'♨',note:'本地安全模型'},
  intrusion:{name:'区域入侵',icon:'◇',note:'进入禁区立即告警'},
  black_screen:{name:'屏幕黑屏',icon:'◩',note:'亮度与内容变化'},
  on_duty:{name:'在岗判定',icon:'●',note:'高级模式'},
  smoking:{name:'人员吸烟',icon:'≈',note:'实验模式'},
}
const sceneInfo:Record<SceneType,{name:string;icon:string}> = {
  workstation:{name:'员工工位',icon:'▦'}, customer_area:{name:'客户位 / 入口',icon:'⇥'},
  security_area:{name:'库房 / 全局',icon:'⌾'}, custom:{name:'自定义',icon:'⚙'},
}
const tabs = [
  ['dashboard','监控总览','▦'],['cameras','摄像头配置','◉'],['alerts','告警中心','⚠'],
  ['traffic','人流报表','⇄'],['settings','系统配置','⚙'],
]
const active = ref('dashboard')
const loading = ref(false)
const dashboard = ref<any>({runtime:{}})
const cameras = ref<Camera[]>([])
const alerts = ref<any[]>([])
const analyses = ref<any[]>([])
const traffic = ref<any[]>([])
const templates = ref<Template[]>([])
const capabilities = ref<any[]>([])
const toast = reactive({show:false,message:'',kind:'ok'})
const preview = ref<Camera|null>(null)
const editor = ref<Camera|null>(null)
const drawLayer = ref<DrawLayer>('post_roi')
const canvasRef = ref<HTMLElement|null>(null)
let toastTimer:number|undefined
let refreshTimer:number|undefined
let socket:WebSocket|undefined

const api = async (url:string, options?:RequestInit) => {
  const response = await fetch(url, {headers:{'Content-Type':'application/json'}, ...options})
  if (!response.ok) {
    let message = `请求失败 HTTP ${response.status}`
    try { const data = await response.json(); message = data.detail || message } catch {}
    throw new Error(typeof message === 'string' ? message : JSON.stringify(message))
  }
  if (response.status === 204) return null
  return response.json()
}
const notify = (message:string, kind='ok') => {
  toast.message=message; toast.kind=kind; toast.show=true
  window.clearTimeout(toastTimer); toastTimer=window.setTimeout(()=>toast.show=false,4200)
}
const loadAll = async (silent=false) => {
  if (!silent) loading.value=true
  try {
    const [d,c,a,n,t,st,cp] = await Promise.all([
      api('/api/dashboard'), api('/api/cameras'), api('/api/alerts?limit=100'), api('/api/analyses?limit=100'),
      api('/api/traffic?limit=200'), api('/api/scene-templates'), api('/api/capabilities'),
    ])
    dashboard.value=d; cameras.value=c; alerts.value=a; analyses.value=n; traffic.value=t; templates.value=st; capabilities.value=cp
  } catch (error:any) { if(!silent) notify(error.message,'error') }
  finally { loading.value=false }
}

const defaultOptions = () => ({health_interval_seconds:5,yolo_fps:1,behavior_interval_seconds:15,off_duty_seconds:300,shift_grace_seconds:60,alert_cooldown_seconds:300,black_mean_max:18,black_std_max:12,black_ratio_min:.92,fire_smoke_fps:1,fire_confidence:.55,smoke_confidence:.45,intrusion_confidence:.5,intrusion_cooldown_seconds:60})
const emptySchedule = () => ({timezone:'Asia/Shanghai',weekly:{},holidays:[]})
const deepCopy = <T,>(value:T):T => JSON.parse(JSON.stringify(value))
const newCamera = reactive<any>({id:'',name:'',rtsp_url:'',scene_type:'workstation' as SceneType,modes:[] as Mode[],schedule:emptySchedule(),options:defaultOptions()})
const selectTemplate = (scene:SceneType) => {
  newCamera.scene_type=scene
  const item=templates.value.find(t=>t.scene_type===scene)
  if(item){newCamera.modes=[...item.modes];newCamera.schedule=deepCopy(item.schedule);newCamera.options={...defaultOptions(),...item.options}}
  else {newCamera.modes=['black_screen'];newCamera.schedule=emptySchedule()}
}
const defaultGeometry = (scene:SceneType) => {
  if(scene==='workstation') return {post_roi:[[.12,.12],[.88,.12],[.88,.9],[.12,.9]],flow_line:[],intrusion_zone:null}
  if(scene==='customer_area') return {post_roi:[],flow_line:[[.15,.52],[.85,.52]],intrusion_zone:null}
  if(scene==='security_area') return {post_roi:[],flow_line:[],intrusion_zone:{name:'禁区',points:[[.12,.12],[.88,.12],[.88,.9],[.12,.9]]}}
  return {post_roi:[],flow_line:[],intrusion_zone:null}
}
const toggleMode = (target:any, mode:Mode) => {
  const list=target.modes as Mode[]; const index=list.indexOf(mode)
  if(index>=0) list.splice(index,1); else list.push(mode)
}
const createCamera = async () => {
  try {
    if(!newCamera.id||!newCamera.name||!newCamera.rtsp_url) throw new Error('请填写摄像头 ID、名称和视频源')
    const created=await api('/api/cameras',{method:'POST',body:JSON.stringify({...newCamera,geometry:defaultGeometry(newCamera.scene_type)})})
    notify('摄像头已添加，请继续校准检测区域'); await loadAll(true)
    Object.assign(newCamera,{id:'',name:'',rtsp_url:'',scene_type:'workstation',modes:[],schedule:emptySchedule(),options:defaultOptions()}); selectTemplate('workstation')
    openEditor(created)
  } catch(error:any){notify(error.message,'error')}
}
const removeCamera = async (camera:Camera) => {
  if(!confirm(`确定删除 ${camera.name}？历史告警会保留。`)) return
  try{await api(`/api/cameras/${camera.id}`,{method:'DELETE'});notify('摄像头已删除');await loadAll(true)}catch(error:any){notify(error.message,'error')}
}
const analyze = async (camera:Camera) => {
  try{notify(`${camera.name} 已提交即时分析`);await api(`/api/cameras/${camera.id}/analyze`,{method:'POST'});await loadAll(true)}catch(error:any){notify(error.message,'error')}
}

const editForm = reactive<any>({scene_type:'custom',modes:[],geometry:{post_roi:[],flow_line:[],intrusion_zone:null},schedule:emptySchedule(),options:defaultOptions(),zone_name:'禁区'})
const openEditor = (camera:Camera) => {
  editor.value=camera
  Object.assign(editForm,{scene_type:camera.scene_type,modes:[...camera.modes],geometry:deepCopy(camera.geometry||defaultGeometry('custom')),schedule:deepCopy(camera.schedule||emptySchedule()),options:{...defaultOptions(),...(camera.options||{})},zone_name:camera.geometry?.intrusion_zone?.name||'禁区'})
  drawLayer.value = camera.scene_type==='customer_area'?'flow_line':camera.scene_type==='security_area'?'intrusion_zone':'post_roi'
}
const editorTemplate = (scene:SceneType) => {
  editForm.scene_type=scene
  const item=templates.value.find(t=>t.scene_type===scene)
  if(item){editForm.modes=[...item.modes];editForm.schedule=deepCopy(item.schedule);editForm.options={...defaultOptions(),...item.options};editForm.geometry=defaultGeometry(scene)}
}
const pointsFor = (layer:DrawLayer):Point[] => layer==='intrusion_zone' ? (editForm.geometry.intrusion_zone?.points||[]) : (editForm.geometry[layer]||[])
const setPoints = (layer:DrawLayer, points:Point[]) => {
  if(layer==='intrusion_zone') editForm.geometry.intrusion_zone={name:editForm.zone_name||'禁区',points}
  else editForm.geometry[layer]=points
}
const canvasClick = (event:MouseEvent) => {
  const target=canvasRef.value; if(!target) return
  const rect=target.getBoundingClientRect(); const p:[number,number]=[
    Math.max(0,Math.min(1,(event.clientX-rect.left)/rect.width)), Math.max(0,Math.min(1,(event.clientY-rect.top)/rect.height))]
  const current=[...pointsFor(drawLayer.value)]
  if(drawLayer.value==='flow_line'&&current.length>=2) current.splice(0,current.length)
  current.push(p); setPoints(drawLayer.value,current)
}
const clearLayer = () => setPoints(drawLayer.value,[])
const polygon = (points:Point[]) => points.map(p=>`${p[0]*100},${p[1]*100}`).join(' ')
const line = (points:Point[]) => points.length===2?{x1:points[0][0]*100,y1:points[0][1]*100,x2:points[1][0]*100,y2:points[1][1]*100}:null
const saveEditor = async () => {
  if(!editor.value) return
  try {
    if(editForm.modes.includes('off_duty')||editForm.modes.includes('phone_use')||editForm.modes.includes('on_duty')) if(pointsFor('post_roi').length<3) throw new Error('岗位区域至少需要 3 个点')
    if(editForm.modes.includes('people_flow')&&pointsFor('flow_line').length!==2) throw new Error('人员计数需要 2 个点的人流线')
    if(editForm.modes.includes('intrusion')&&pointsFor('intrusion_zone').length<3) throw new Error('区域入侵需要至少 3 个点的禁区')
    if(editForm.geometry.intrusion_zone) editForm.geometry.intrusion_zone.name=editForm.zone_name||'禁区'
    const id=editor.value.id
    await api(`/api/cameras/${id}`,{method:'PATCH',body:JSON.stringify({scene_type:editForm.scene_type,options:editForm.options})})
    await api(`/api/cameras/${id}/geometry`,{method:'PUT',body:JSON.stringify(editForm.geometry)})
    await api(`/api/cameras/${id}/schedule`,{method:'PUT',body:JSON.stringify(editForm.schedule)})
    await api(`/api/cameras/${id}/modes`,{method:'PUT',body:JSON.stringify({modes:editForm.modes})})
    editor.value=null;notify('检测策略与区域已保存');await loadAll(true)
  } catch(error:any){notify(error.message,'error')}
}
const weekdays=[['0','一'],['1','二'],['2','三'],['3','四'],['4','五'],['5','六'],['6','日']]
const dayEnabled=(d:string)=>Boolean(editForm.schedule.weekly?.[d]?.length)
const toggleDay=(d:string)=>{if(dayEnabled(d)) delete editForm.schedule.weekly[d];else editForm.schedule.weekly[d]=[{start:'08:30',end:'12:00'},{start:'13:30',end:'17:30'}]}
const firstShift = computed(()=>{const d=Object.keys(editForm.schedule.weekly||{})[0];return d?editForm.schedule.weekly[d][0]:{start:'08:30',end:'12:00'}})
const secondShift = computed(()=>{const d=Object.keys(editForm.schedule.weekly||{})[0];return d?(editForm.schedule.weekly[d][1]||{start:'13:30',end:'17:30'}):{start:'13:30',end:'17:30'}})
const syncShifts=()=>{for(const d of Object.keys(editForm.schedule.weekly||{})) editForm.schedule.weekly[d]=[deepCopy(firstShift.value),deepCopy(secondShift.value)]}

const modelSettings=reactive<any>({provider:'mock',base_url:'',api_key:'',economy_model:'qwen-vl',enhanced_model:'qwen-vl-max',api_key_configured:false})
const webhookSettings=reactive<any>({enabled:false,url:'',secret:'',secret_configured:false})
const detectorSettings=reactive<any>({general_model:'yolo26n.pt',general_device:'cpu',fire_smoke_model:'models/fire_smoke_yolov8.pt',fire_smoke_device:'cpu',model_sha256:'',license_name:'AGPL-3.0 (internal pilot only)',runtime:{}})
const loadSettings=async()=>{try{Object.assign(modelSettings,await api('/api/settings/models'));Object.assign(webhookSettings,await api('/api/settings/webhook'));Object.assign(detectorSettings,await api('/api/settings/detectors'))}catch(error:any){notify(error.message,'error')}}
const saveModels=async()=>{try{await api('/api/settings/models',{method:'PUT',body:JSON.stringify(modelSettings)});notify('视觉大模型配置已保存');await loadSettings()}catch(error:any){notify(error.message,'error')}}
const testModels=async()=>{try{const r=await api('/api/settings/models/test',{method:'POST'});notify(`模型连接成功，延迟 ${r.latency_ms||0}ms`)}catch(error:any){notify(error.message,'error')}}
const saveWebhook=async()=>{try{await api('/api/settings/webhook',{method:'PUT',body:JSON.stringify(webhookSettings)});notify('Webhook 配置已保存');await loadSettings()}catch(error:any){notify(error.message,'error')}}
const saveDetectors=async()=>{try{const body={...detectorSettings};delete body.runtime;delete body.updated_at;await api('/api/settings/detectors',{method:'PUT',body:JSON.stringify(body)});notify('本地检测器配置已保存并重新加载');await loadSettings()}catch(error:any){notify(error.message,'error')}}
const setTab=(name:string)=>{active.value=name;if(name==='settings')loadSettings()}
const scrollToAdd=()=>document.getElementById('add-camera')?.scrollIntoView({behavior:'smooth'})
const formatTime=(value:string)=>value?new Date(value).toLocaleString('zh-CN',{hour12:false}):'—'
const modeName=(mode:Mode)=>modeInfo[mode]?.name||mode
const onlineCount=computed(()=>cameras.value.filter(c=>c.online).length)
const latestTraffic=computed(()=>{const seen=new Set<string>();return traffic.value.filter(x=>{if(seen.has(x.camera_id))return false;seen.add(x.camera_id);return true})})

const connectWs=()=>{
  const protocol=location.protocol==='https:'?'wss':'ws';socket=new WebSocket(`${protocol}://${location.host}/ws/events`)
  socket.onmessage=(event)=>{try{const data=JSON.parse(event.data);if(data.type==='alert'){notify(`${data.severity==='critical'?'紧急：':''}${data.camera_name||data.camera_id} ${modeName(data.mode)}：${data.reason}`,'alert');loadAll(true)}}catch{}}
  socket.onclose=()=>window.setTimeout(connectWs,3000)
}
onMounted(async()=>{await loadAll();selectTemplate('workstation');connectWs();refreshTimer=window.setInterval(()=>loadAll(true),15000)})
onUnmounted(()=>{window.clearInterval(refreshTimer);socket?.close()})
</script>

<template>
  <div class="shell">
    <aside class="sidebar">
      <div class="brand"><div class="brand-mark">A</div><div><strong>灵眸</strong><small>AI VISION OPS</small></div></div>
      <nav><button v-for="tab in tabs" :key="tab[0]" :class="{active:active===tab[0]}" @click="setTab(tab[0])"><span>{{tab[2]}}</span>{{tab[1]}}</button></nav>
      <div class="system-pill"><i :class="onlineCount?'good':'warn'"></i><div><b>{{onlineCount}} / {{cameras.length}} 路在线</b><small>安全检测持续运行</small></div></div>
      <div class="side-foot"><span>端口 8100</span><span>内部试点</span></div>
    </aside>
    <main>
      <header class="topbar"><div><h1>{{tabs.find(t=>t[0]===active)?.[1]}}</h1><p>本地 YOLO 检测 + 外部视觉模型复核 · 安全模式全天运行</p></div><div class="top-actions"><button class="icon-btn refresh" :class="{spin:loading}" @click="loadAll()">↻</button><button v-if="active==='cameras'" class="primary" @click="scrollToAdd">＋ 添加视频源</button></div></header>

      <section v-if="active==='dashboard'" class="page">
        <div class="metrics">
          <article><span>监控源</span><strong>{{onlineCount}}<small> / {{cameras.length}}</small></strong><em>当前在线</em></article>
          <article><span>当前在店人数</span><strong>{{dashboard.current_people||0}}</strong><em>各入口实时合计</em></article>
          <article :class="{'critical-metric':dashboard.critical_alerts_today}"><span>今日烟火紧急告警</span><strong>{{dashboard.critical_alerts_today||0}}</strong><em>本地模型全天检测</em></article>
          <article><span>今日区域入侵</span><strong>{{dashboard.intrusions_today||0}}</strong><em>进入禁区即时触发</em></article>
        </div>
        <div class="scene-summary">
          <article v-for="scene in (['workstation','customer_area','security_area','custom'] as SceneType[])" :key="scene"><i>{{sceneInfo[scene].icon}}</i><div><b>{{dashboard.scene_counts?.[scene]||0}}</b><span>{{sceneInfo[scene].name}}</span></div></article>
        </div>
        <div class="section-head"><div><h2>实时监控</h2><p>人员绿色框；入侵与火焰红色；烟雾橙色</p></div></div>
        <div class="camera-grid">
          <article v-for="camera in cameras" :key="camera.id" class="camera-card">
            <div class="camera-shot" @click="preview=camera"><img v-if="camera.online" :src="`/api/cameras/${camera.id}/snapshot?t=${Date.now()}`"><div v-else class="no-signal"><span>⌁</span>{{camera.last_error||'等待视频流'}}</div><b :class="camera.online?'online':'offline'">{{camera.online?'LIVE':'OFFLINE'}}</b></div>
            <div class="camera-meta"><div><h3>{{camera.name}}</h3><p>{{sceneInfo[camera.scene_type]?.name}} · {{camera.id}}</p></div><button class="icon-btn" @click="openEditor(camera)">⚙</button></div>
            <div class="chips"><span v-for="mode in camera.modes" :key="mode">{{modeName(mode)}}</span></div>
          </article>
          <article v-if="!cameras.length" class="camera-card empty-card"><b>＋</b>请先添加监控源</article>
        </div>
        <div class="two-cols">
          <section class="panel"><div class="section-head"><div><h2>最近告警</h2><p>按严重级别优先排序</p></div><button class="link" @click="active='alerts'">查看全部</button></div><div v-if="!alerts.length" class="empty">暂无告警</div><div v-for="item in alerts.slice(0,6)" :key="item.id" class="alert-row" :class="`severity-${item.severity}`"><div class="alert-symbol">!</div><div><b>{{modeName(item.mode)}} · {{item.camera_id}}</b><p>{{item.reason}}</p></div><time>{{formatTime(item.created_at)}}</time></div></section>
          <section class="panel runtime-panel"><div class="section-head"><div><h2>运行状态</h2><p>普通与安全检测独立队列</p></div></div><dl><div><dt>通用 YOLO</dt><dd>{{dashboard.runtime?.detectors?.general?.status||'unknown'}}</dd></div><div><dt>烟火模型</dt><dd :class="{'amber':dashboard.runtime?.detectors?.fire_smoke?.status!=='ready'}">{{dashboard.runtime?.detectors?.fire_smoke?.status||'unknown'}}</dd></div><div><dt>普通队列</dt><dd>{{dashboard.runtime?.queues?.general||dashboard.runtime?.queue_depth||0}}</dd></div><div><dt>安全队列</dt><dd>{{dashboard.runtime?.queues?.fire||0}}</dd></div></dl></section>
        </div>
      </section>

      <section v-else-if="active==='cameras'" class="page">
        <div class="split-layout">
          <section id="add-camera" class="panel add-panel">
            <div class="section-head"><div><h2>添加监控源</h2><p>先选择场景，系统自动带出策略</p></div></div>
            <div class="scene-picker"><button v-for="scene in (['workstation','customer_area','security_area','custom'] as SceneType[])" :key="scene" :class="{selected:newCamera.scene_type===scene}" @click="selectTemplate(scene)"><i>{{sceneInfo[scene].icon}}</i><b>{{sceneInfo[scene].name}}</b></button></div>
            <label>摄像头 ID<input v-model.trim="newCamera.id" placeholder="例如 hall-entrance-01"></label>
            <label>显示名称<input v-model.trim="newCamera.name" placeholder="例如 营业厅入口"></label>
            <label>RTSP / 本地视频源<input v-model.trim="newCamera.rtsp_url" type="password" placeholder="rtsp://user:password@host/... "></label>
            <div class="field-title">启用能力 <small>可多选</small></div><div class="mode-picker"><button v-for="(info,mode) in modeInfo" :key="mode" :class="{selected:newCamera.modes.includes(mode)}" @click="toggleMode(newCamera,mode)"><i>{{info.icon}}</i><span><b>{{info.name}}</b><small>{{info.note}}</small></span></button></div>
            <button class="primary wide" @click="createCamera">添加并配置区域</button>
            <div class="config-note"><b>安全说明</b><p>烟火、入侵、黑屏始终全天运行，不受普通排班影响。视频烟火预警不能替代认证消防设备。</p></div>
          </section>
          <section class="panel source-list"><div class="section-head"><div><h2>已有监控源</h2><p>原有摄像头保持自定义场景与原配置</p></div></div>
            <article v-for="camera in cameras" :key="camera.id"><i class="source-state" :class="camera.online?'ok':'bad'"></i><div class="source-main"><h3>{{camera.name}}<small>{{sceneInfo[camera.scene_type]?.name}}</small></h3><p>{{camera.source}}</p><div class="chips"><span v-for="mode in camera.modes" :key="mode">{{modeName(mode)}}</span></div><small v-if="camera.last_error" class="error-text">{{camera.last_error}}</small></div><div class="source-actions"><button @click="preview=camera">预览</button><button @click="analyze(camera)">立即分析</button><button @click="openEditor(camera)">策略/区域</button><button class="danger" @click="removeCamera(camera)">删除</button></div></article>
          </section>
        </div>
      </section>

      <section v-else-if="active==='alerts'" class="page"><section class="panel table-panel"><div class="section-head"><div><h2>告警中心</h2><p>烟火紧急告警置顶；仅确认违规才告警</p></div></div><table><thead><tr><th>级别</th><th>证据</th><th>摄像头 / 场景</th><th>事件</th><th>区域</th><th>原因</th><th>时间</th></tr></thead><tbody><tr v-for="item in alerts" :key="item.id" :class="`severity-${item.severity}`"><td><span class="severity-badge" :class="item.severity">{{item.severity||'normal'}}</span></td><td><img v-if="item.evidence_url" class="evidence" :src="item.evidence_url"><span v-else>—</span></td><td>{{item.camera_id}}<br><small>{{sceneInfo[cameras.find(c=>c.id===item.camera_id)?.scene_type||'custom'].name}}</small></td><td><span class="event-type">{{modeName(item.mode)}}</span></td><td>{{item.zone_name||'—'}}</td><td class="reason">{{item.reason}}</td><td>{{formatTime(item.created_at)}}</td></tr></tbody></table><div v-if="!alerts.length" class="empty">暂无告警</div></section></section>

      <section v-else-if="active==='traffic'" class="page"><div class="metrics"><article><span>当前在店人数</span><strong>{{dashboard.current_people||0}}</strong></article><article><span>今日进入</span><strong>{{latestTraffic.reduce((n,x)=>n+x.entered,0)}}</strong></article><article><span>今日离开</span><strong>{{latestTraffic.reduce((n,x)=>n+x.exited,0)}}</strong></article><article><span>统计摄像头</span><strong>{{cameras.filter(c=>c.modes.includes('people_flow')).length}}</strong></article></div><section class="panel table-panel"><table><thead><tr><th>摄像头</th><th>时间</th><th>当前人数</th><th>进入</th><th>离开</th></tr></thead><tbody><tr v-for="row in traffic" :key="`${row.camera_id}-${row.bucket_start}`"><td>{{row.camera_id}}</td><td>{{formatTime(row.bucket_start)}}</td><td>{{row.current_count}}</td><td class="green">+{{row.entered}}</td><td>-{{row.exited}}</td></tr></tbody></table><div v-if="!traffic.length" class="empty">暂无人流数据</div></section></section>

      <section v-else class="page settings-grid">
        <section class="panel"><div class="section-head"><div><h2>外部视觉大模型</h2><p>仅用于玩手机与实验性人员吸烟复核</p></div><span class="status-dot" :class="{ready:modelSettings.api_key_configured||modelSettings.provider==='mock'}">{{modelSettings.api_key_configured?'API Key 已配置':modelSettings.provider==='mock'?'模拟模式':'未配置'}}</span></div><label>提供商<select v-model="modelSettings.provider"><option value="openai_compatible">OpenAI 兼容接口</option><option value="mock">模拟模式</option></select></label><label>Base URL<input v-model="modelSettings.base_url" placeholder="https://.../v1"></label><label>API Key<input v-model="modelSettings.api_key" type="password" placeholder="留空表示保持现有密钥"></label><div class="form-row"><label>经济模型<input v-model="modelSettings.economy_model"></label><label>增强模型<input v-model="modelSettings.enhanced_model"></label></div><div class="actions"><button class="primary" @click="saveModels">保存</button><button class="ghost" @click="testModels">测试连接</button></div></section>
        <section class="panel"><div class="section-head"><div><h2>本地检测器</h2><p>通用 YOLO 与烟火模型独立加载</p></div></div><div class="detector-status"><article><span>通用 YOLO</span><b>{{detectorSettings.runtime?.general?.status||'unknown'}}</b><small>{{detectorSettings.runtime?.general?.latency_ms||0}} ms</small></article><article :class="{dangerbox:detectorSettings.runtime?.fire_smoke?.status!=='ready'}"><span>烟火模型</span><b>{{detectorSettings.runtime?.fire_smoke?.status||'unknown'}}</b><small>{{detectorSettings.runtime?.fire_smoke?.detail||detectorSettings.runtime?.fire_smoke?.latency_ms+' ms'}}</small></article></div><div class="form-row"><label>通用模型<input v-model="detectorSettings.general_model"></label><label>运行设备<input v-model="detectorSettings.general_device"></label></div><div class="form-row"><label>烟火权重路径<input v-model="detectorSettings.fire_smoke_model"></label><label>运行设备<input v-model="detectorSettings.fire_smoke_device"></label></div><label>烟火权重 SHA256<input v-model="detectorSettings.model_sha256" readonly></label><label>许可证<input v-model="detectorSettings.license_name" readonly></label><button class="primary" @click="saveDetectors">保存并重载</button>
        </section>
        <section class="panel"><div class="section-head"><div><h2>Webhook</h2><p>HMAC-SHA256 签名，失败最多重试 5 次</p></div><label class="switch"><input v-model="webhookSettings.enabled" type="checkbox"><span></span></label></div><label>HTTPS URL<input v-model="webhookSettings.url" placeholder="https://your-service/events"></label><label>签名密钥<input v-model="webhookSettings.secret" type="password" placeholder="留空表示保持现有密钥"></label><button class="primary" @click="saveWebhook">保存 Webhook</button></section>
        <section class="panel"><div class="section-head"><div><h2>能力注册表</h2><p>计划能力只展示，不会进入检测任务</p></div></div><div class="capability-list"><span v-for="item in capabilities" :key="item.id"><b>{{item.name}}</b><em :class="item.availability">{{item.availability}}</em></span></div></section>
      </section>
    </main>

    <div v-if="preview" class="modal" @click.self="preview=null"><section class="preview-modal"><header class="modal-head"><div><h2>{{preview.name}}</h2><p>人员绿框 · 入侵/火焰红框 · 烟雾橙框</p></div><button type="button" @click.stop="preview=null">×</button></header><div class="live"><img :src="`/api/cameras/${preview.id}/preview`"><span>LIVE</span><span class="person-legend">■ 人员　■ 入侵/火焰　■ 烟雾</span></div></section></div>

    <div v-if="editor" class="modal" @click.self="editor=null"><section class="editor-modal"><header class="modal-head"><div><h2>{{editor.name}} · 策略与区域</h2><p>点击画面添加归一化坐标点；图层相互独立</p></div><button type="button" @click.stop="editor=null">×</button></header><div class="editor-body"><div class="editor-left">
      <div class="scene-picker compact-scenes"><button v-for="scene in (['workstation','customer_area','security_area','custom'] as SceneType[])" :key="scene" :class="{selected:editForm.scene_type===scene}" @click="editorTemplate(scene)"><i>{{sceneInfo[scene].icon}}</i><b>{{sceneInfo[scene].name}}</b></button></div>
      <div class="mode-picker compact"><button v-for="(info,mode) in modeInfo" :key="mode" :class="{selected:editForm.modes.includes(mode)}" @click="toggleMode(editForm,mode)"><i>{{info.icon}}</i><span><b>{{info.name}}</b></span></button></div>
      <div class="draw-actions"><button :class="{active:drawLayer==='post_roi'}" class="ghost" @click="drawLayer='post_roi'">岗位区域</button><button :class="{active:drawLayer==='flow_line'}" class="ghost" @click="drawLayer='flow_line'">人流线</button><button :class="{active:drawLayer==='intrusion_zone'}" class="ghost danger-layer" @click="drawLayer='intrusion_zone'">禁区</button><button class="danger" @click="clearLayer">清空当前图层</button></div>
      <div ref="canvasRef" class="geometry-stage" @click="canvasClick"><img :src="`/api/cameras/${editor.id}/snapshot?t=${Date.now()}`"><svg viewBox="0 0 100 100" preserveAspectRatio="none"><polygon v-if="pointsFor('post_roi').length>=3" :points="polygon(pointsFor('post_roi'))" class="post-zone"/><line v-if="line(pointsFor('flow_line'))" v-bind="line(pointsFor('flow_line'))" class="flow-line"/><polygon v-if="pointsFor('intrusion_zone').length>=3" :points="polygon(pointsFor('intrusion_zone'))" class="intrusion-zone"/><g v-for="(p,index) in pointsFor(drawLayer)" :key="index"><circle :cx="p[0]*100" :cy="p[1]*100" r="1.1"/><text :x="p[0]*100+1.5" :y="p[1]*100-1">{{index+1}}</text></g></svg></div><p class="hint">岗位/禁区至少 3 点；人流线恰好 2 点。当前图层已有 {{pointsFor(drawLayer).length}} 个点。</p>
    </div><aside class="editor-right"><label v-if="editForm.modes.includes('intrusion')">禁区名称<input v-model="editForm.zone_name"></label><div class="field-title">普通模式排班</div><div class="weekday"><button v-for="d in weekdays" :key="d[0]" :class="{selected:dayEnabled(d[0])}" @click="toggleDay(d[0])">{{d[1]}}</button></div><div class="form-row"><label>上午开始<input v-model="firstShift.start" type="time" @change="syncShifts"></label><label>上午结束<input v-model="firstShift.end" type="time" @change="syncShifts"></label></div><div class="form-row"><label>下午开始<input v-model="secondShift.start" type="time" @change="syncShifts"></label><label>下午结束<input v-model="secondShift.end" type="time" @change="syncShifts"></label></div><label>离岗时长（秒）<input v-model.number="editForm.options.off_duty_seconds" type="number" min="30"></label><label>玩手机候选间隔（秒）<input v-model.number="editForm.options.behavior_interval_seconds" type="number" min="5"></label><div class="form-row"><label>火焰阈值<input v-model.number="editForm.options.fire_confidence" type="number" min="0" max="1" step=".05"></label><label>烟雾阈值<input v-model.number="editForm.options.smoke_confidence" type="number" min="0" max="1" step=".05"></label></div><label>入侵置信度<input v-model.number="editForm.options.intrusion_confidence" type="number" min="0" max="1" step=".05"></label><div class="config-note"><b>全天安全模式</b><p>烟火、区域入侵、黑屏忽略此处排班，始终运行。</p></div><button class="primary wide" @click="saveEditor">保存策略</button></aside></div></section></div>

    <Transition name="toast"><div v-if="toast.show" class="toast" :class="toast.kind"><span>{{toast.kind==='ok'?'✓':'!'}}</span>{{toast.message}}</div></Transition>
  </div>
</template>
