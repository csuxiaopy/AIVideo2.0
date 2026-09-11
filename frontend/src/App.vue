<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { api, download, setCsrfToken, setUnauthorizedHandler } from './api'
import TechIcon from './components/TechIcon.vue'
import EvidencePreview from './components/EvidencePreview.vue'
import CameraBasicFields from './components/CameraBasicFields.vue'
import BatchCameraModal from './components/BatchCameraModal.vue'
import type { Camera, CameraDirectory, DrawLayer, Mode, Point, SceneType, SceneTemplate, TrafficCameraSummary, TrafficMonthlyReport, TrafficSummary } from './types'

const modeInfo:Record<Mode,{name:string;icon:string;note:string}> = {
  off_duty:{name:'离岗检测',icon:'offDuty',note:'持续无人后由大模型终审'},
  phone_use:{name:'玩手机检测',icon:'phone',note:'每 3 分钟单帧大模型联合检测'},
  people_flow:{name:'人员计数',icon:'traffic',note:'新人员进入画面自动统计'},
  fire_smoke:{name:'烟火检测',icon:'flame',note:'本地安全模型'},
  intrusion:{name:'区域入侵',icon:'intrusion',note:'进入禁区立即告警'},
  black_screen:{name:'屏幕黑屏',icon:'blackScreen',note:'亮度与内容变化'},
  on_duty:{name:'在岗判定',icon:'onDuty',note:'高级模式'},
  smoking:{name:'人员吸烟',icon:'smoking',note:'每 3 分钟单帧大模型联合检测'},
}
const sceneInfo:Record<SceneType,{name:string;icon:string;en:string}> = {
  workstation:{name:'员工工位',icon:'workstation',en:'WORKSTATION'},
  customer_area:{name:'客户位 / 入口',icon:'entrance',en:'ENTRANCE'},
  security_area:{name:'库房 / 全局',icon:'warehouse',en:'WAREHOUSE'},
  custom:{name:'其他',icon:'sliders',en:'OTHER'},
}
const tabs = [
  {key:'dashboard',name:'监控总览',icon:'dashboard',en:'MONITORING CENTER'},
  {key:'traffic',name:'人流报表',icon:'traffic',en:'PEOPLE FLOW'},
  {key:'alerts',name:'告警中心',icon:'alert',en:'AI ALERT CENTER'},
  {key:'cameras',name:'摄像头配置',icon:'video',en:'CAMERA MANAGEMENT'},
  {key:'webhooks',name:'企业微信机器人',icon:'webhook',en:'WECOM DELIVERY'},
  {key:'settings',name:'系统配置',icon:'settings',en:'SYSTEM SETTINGS'},
  {key:'users',name:'账号管理',icon:'users',en:'ACCOUNT MANAGEMENT'},
  {key:'logs',name:'日志管理',icon:'database',en:'SYSTEM LOGS'},
]
type AuthUser = {id:number;username:string;display_name:string;role:'admin'|'user';enabled:boolean;created_at:string;updated_at:string}
const authReady=ref(false)
const currentUser=ref<AuthUser|null>(null)
const isAdmin=computed(()=>currentUser.value?.role==='admin')
const loginForm=reactive({username:'',password:'',captcha_id:'',captcha_answer:''})
const captchaImage=ref('')
const loginBusy=ref(false)
const loginError=ref('')
const passwordModal=ref(false)
const passwordForm=reactive({old_password:'',new_password:'',confirm:''})
const users=ref<AuthUser[]>([])
const userForm=reactive({username:'',display_name:'',password:''})
const active = ref('dashboard')
const loading = ref(false)
const dashboard = ref<any>({runtime:{}})
const cameras = ref<Camera[]>([])
const cameraDirectories = ref<CameraDirectory[]>([])
const activeDirectory = ref('all')
const moveDirectory = ref('')
const dashboardCameraSearch = ref('')
const cameraSettingsSearch = ref('')
const focusedCameraId = ref('')
let focusCameraTimer:number|undefined
const matchesCameraName=(camera:Camera,query:string)=>camera.name.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())
const filteredDashboardCameras=computed(()=>cameras.value.filter(camera=>matchesCameraName(camera,dashboardCameraSearch.value)))
const directoryCameras=computed(()=>cameras.value.filter(camera=>activeDirectory.value==='all'||(activeDirectory.value==='unassigned'?!camera.directory_id:String(camera.directory_id)===activeDirectory.value)))
const filteredSettingsCameras=computed(()=>directoryCameras.value.filter(camera=>matchesCameraName(camera,cameraSettingsSearch.value)))
const unassignedCameraCount=computed(()=>cameras.value.filter(camera=>!camera.directory_id).length)
const alerts = ref<any[]>([])
type LogCategory = 'audit'|'analyses'|'model-calls'
const logCategory=ref<LogCategory>('audit')
const logData=reactive<{items:any[];total:number;page:number;page_size:number}>({items:[],total:0,page:1,page_size:50})
const logFilters=reactive({start:'',end:'',username:'',action:'',camera_id:'',mode:'',status:'',model:'',stage:'',outcome:''})
const logLoading=ref(false)
const expandedLogId=ref<number|null>(null)
const logDetails=reactive<Record<string,any>>({})
const emptyTrafficSummary = ():TrafficSummary => ({date:'',timezone:'Asia/Shanghai',total_flow_today:0,current_people:0,entered_today:0,exited_today:0,flow_camera_count:0,store_trend:[],cameras:[],current_ranking:[],flow_ranking:[]})
const trafficSummary = ref<TrafficSummary>(emptyTrafficSummary())
const shanghaiMonth=()=>new Date().toLocaleDateString('sv-SE',{timeZone:'Asia/Shanghai'}).slice(0,7)
const currentTrafficMonth=shanghaiMonth()
const trafficMonth=ref(currentTrafficMonth)
const emptyTrafficMonthlyReport=(month=currentTrafficMonth):TrafficMonthlyReport=>({month,timezone:'Asia/Shanghai',days:[],rows:[],daily_totals:[],grand_total:0})
const trafficMonthlyReport=ref<TrafficMonthlyReport>(emptyTrafficMonthlyReport())
const trafficMonthlyLoading=ref(false)
const trafficMonthlyError=ref('')
let trafficMonthlySeq=0
const templates = ref<SceneTemplate[]>([])
const capabilities = ref<any[]>([])
const toast = reactive({show:false,message:'',kind:'ok'})
const preview = ref<Camera|null>(null)
const previewSessionId = ref('')
const previewStreamUrl = ref('')
const previewLoading = ref(false)
const previewError = ref('')
const editor = ref<Camera|null>(null)
const batchModal = ref(false)
const cameraScheduleModal = ref(false)
const offDutyScheduleModal = ref(false)
const offDutyScheduleSaving = ref(false)
const defaultOffDutyShifts = () => ([
  {start:'09:00',end:'11:00',off_duty_seconds:300},
  {start:'12:00',end:'13:30',off_duty_seconds:900},
  {start:'13:30',end:'17:00',off_duty_seconds:300},
])
const cameraScheduleDraft = reactive({
  days:[] as string[],
  shifts:defaultOffDutyShifts(),
})
const offDutyScheduleForm = reactive({
  days:['0','1','2','3','4','5','6'] as string[],
  shifts:defaultOffDutyShifts(),
})
const selectedCameraIds = ref<string[]>([])
const allCamerasSelected = computed(()=>filteredSettingsCameras.value.length>0&&filteredSettingsCameras.value.every(camera=>selectedCameraIds.value.includes(camera.id)))
watch(cameraSettingsSearch,()=>{selectedCameraIds.value=[]})
const drawLayer = ref<DrawLayer>('post_roi')
const canvasRef = ref<HTMLElement|null>(null)
const now = ref(new Date())
const wsOnline = ref(false)
const testing = ref(false)
let toastTimer:number|undefined
let refreshTimer:number|undefined
let clockTimer:number|undefined
let socket:WebSocket|undefined
let previewHeartbeatTimer:number|undefined
const frameIntervalOptions = [1,5,10,20,30,60,120] as const

/* ---------- 顶栏时钟 / 全屏 / 系统状态 ---------- */
const dateStr = computed(()=>now.value.toLocaleDateString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit'}).replace(/\//g,'-'))
const timeStr = computed(()=>now.value.toLocaleTimeString('zh-CN',{hour12:false}))
const pageEn = computed(()=>tabs.find(tab=>tab.key===active.value)?.en||'MONITORING CENTER')
const sysStatusText = computed(()=>!wsOnline.value?'RECONNECTING':(cameras.value.length&&!onlineCount.value)?'NO SIGNAL':'SYSTEM ONLINE')
const sysStatusBad = computed(()=>!wsOnline.value||(cameras.value.length>0&&onlineCount.value===0))
const toggleFullscreen = async () => {
  try {
    if (document.fullscreenElement) await document.exitFullscreen()
    else await document.documentElement.requestFullscreen()
  } catch {}
}

/* ---------- 统一确认对话框（替代原生 confirm/prompt） ---------- */
const confirmState = reactive<{show:boolean;title:string;message:string;input:boolean;inputType:string;inputLabel:string;inputValue:string;confirmText:string;danger:boolean;resolve?:(value:any)=>void}>(
  {show:false,title:'',message:'',input:false,inputType:'text',inputLabel:'',inputValue:'',confirmText:'确认',danger:false})
const dialogConfirm = (options:{title:string;message:string;input?:boolean;inputType?:string;inputLabel?:string;inputValue?:string;confirmText?:string;danger?:boolean}):Promise<any> =>
  new Promise(resolve=>{
    Object.assign(confirmState,{show:true,input:false,inputType:'text',inputLabel:'',inputValue:'',confirmText:'确认',danger:false},options,{resolve})
  })
const resolveConfirm = (value:any) => {
  confirmState.show=false
  const done=confirmState.resolve
  confirmState.resolve=undefined
  done?.(value)
}

const notify = (message:string, kind='ok') => {
  toast.message=message; toast.kind=kind; toast.show=true
  window.clearTimeout(toastTimer); toastTimer=window.setTimeout(()=>toast.show=false,4200)
}
const loadTrafficMonthly=async(silent=false)=>{
  const month=trafficMonth.value
  const seq=++trafficMonthlySeq
  if(!silent)trafficMonthlyLoading.value=true
  trafficMonthlyError.value=''
  try{
    const report=await api<TrafficMonthlyReport>(`/api/traffic/monthly?month=${encodeURIComponent(month)}`)
    if(seq===trafficMonthlySeq)trafficMonthlyReport.value=report
  }catch(error:any){
    if(seq===trafficMonthlySeq){trafficMonthlyError.value=error.message;if(!silent)notify(`月度人流加载失败：${error.message}`,'error')}
  }finally{if(seq===trafficMonthlySeq)trafficMonthlyLoading.value=false}
}
const exportTrafficMonthly=async()=>{
  try{
    await download('/api/traffic/monthly/export',{month:trafficMonth.value},`人流月报-${trafficMonth.value}.xlsx`)
    notify(`${trafficMonth.value} 人流月报已导出`)
  }catch(error:any){notify(error.message,'error')}
}
watch(trafficMonth,()=>{trafficMonthlyReport.value=emptyTrafficMonthlyReport(trafficMonth.value);void loadTrafficMonthly()})
/* ---------- 告警筛选（服务端 mode/severity 参数，AND 组合） ---------- */
const severityLevels = [
  {value:'normal',label:'NORMAL'},{value:'high',label:'HIGH'},{value:'critical',label:'CRITICAL'},
] as const
const alertFilter = reactive({date:'',mode:'',severity:''})
const filteredAlerts = ref<any[]|null>(null)
let alertFilterSeq = 0
const hasAlertFilter = computed(()=>!!alertFilter.date||!!alertFilter.mode||!!alertFilter.severity)
const alertRows = computed(()=>filteredAlerts.value??alerts.value)
const selectedAlertIds=ref<number[]>([])
const expandedDeliveryIds=ref<number[]>([])
const webhookTargets=ref<any[]>([])
const webhookForm=reactive<any>({id:null,name:'',enabled:false,url:'',auto_severities:[]})
const sendModal=ref(false)
const selectedTargetIds=ref<number[]>([])
const enabledWebhookTargets=computed(()=>webhookTargets.value.filter(target=>target.enabled&&(!isAdmin.value||target.url)))
const allAlertsSelected=computed(()=>alertRows.value.length>0&&alertRows.value.every(row=>selectedAlertIds.value.includes(row.id)))
const toggleAllAlerts=()=>{selectedAlertIds.value=allAlertsSelected.value?[]:alertRows.value.map(row=>row.id)}
const toggleDeliveryDetails=(id:number)=>{expandedDeliveryIds.value=expandedDeliveryIds.value.includes(id)?expandedDeliveryIds.value.filter(x=>x!==id):[...expandedDeliveryIds.value,id]}
const applyAlertFilter = async () => {
  if(!hasAlertFilter.value){filteredAlerts.value=null;return}
  const seq=++alertFilterSeq
  try{
    const params=new URLSearchParams({limit:'100'})
    if(alertFilter.date)params.set('date',alertFilter.date)
    if(alertFilter.mode)params.set('mode',alertFilter.mode)
    if(alertFilter.severity)params.set('severity',alertFilter.severity)
    const rows=await api(`/api/alerts?${params.toString()}`)
    if(seq===alertFilterSeq)filteredAlerts.value=rows
  }catch(error:any){notify(`告警筛选加载失败：${error.message}`,'error')}
}
const resetAlertFilter = () => {alertFilter.date='';alertFilter.mode='';alertFilter.severity='';filteredAlerts.value=null;selectedAlertIds.value=[]}
watch(()=>[alertFilter.date,alertFilter.mode,alertFilter.severity],()=>{selectedAlertIds.value=[]})
const loadAll = async (silent=false) => {
  if (!silent) loading.value=true
  try {
    const [d,c,dirs,a,t,ds] = await Promise.all([
      api('/api/dashboard'), api('/api/cameras'), api('/api/camera-directories'), api('/api/alerts?limit=100'),
      api<TrafficSummary>('/api/traffic/summary'), api('/api/settings/display'),
    ])
    dashboard.value=d; cameras.value=c; cameraDirectories.value=dirs; alerts.value=a; trafficSummary.value=t
    selectedCameraIds.value=selectedCameraIds.value.filter(id=>cameras.value.some(camera=>camera.id===id))
    Object.assign(displaySettings,ds)
    if(isAdmin.value){const [st,cp]=await Promise.all([api('/api/scene-templates'),api('/api/capabilities')]);templates.value=st;capabilities.value=cp}
    if(active.value==='traffic'&&!displaySettings.show_traffic_report) setTab('dashboard')
    if(active.value==='traffic'&&trafficMonth.value===currentTrafficMonth) void loadTrafficMonthly(true)
    if(hasAlertFilter.value) void applyAlertFilter()
  } catch (error:any) { if(!silent) notify(error.message,'error') }
  finally { loading.value=false }
}

const defaultOptions = () => ({health_interval_seconds:5,yolo_fps:.1,behavior_interval_seconds:180,phone_use_seconds:600,off_duty_seconds:300,person_confidence:.3,shift_grace_seconds:0,alert_cooldown_seconds:300,black_mean_max:18,black_std_max:12,black_ratio_min:.92,fire_smoke_fps:1,fire_confidence:.3,smoke_confidence:.3,intrusion_confidence:.5,intrusion_cooldown_seconds:60,flow_min_stable_frames:3,stream_recovery_grace_seconds:15,flow_debug:false})
const emptySchedule = () => ({timezone:'Asia/Shanghai',weekly:{},holidays:[]})
const defaultIntrusionSchedule = () => ({timezone:'Asia/Shanghai',weekly:Object.fromEntries(Array.from({length:7},(_,day)=>[String(day),[{start:'20:00',end:'05:00'}]])),holidays:[]})
const deepCopy = <T,>(value:T):T => JSON.parse(JSON.stringify(value))
const newCamera = reactive<any>({id:'',name:'',rtsp_url:'',enabled:true,scene_type:'workstation' as SceneType,modes:[] as Mode[],schedule:emptySchedule(),intrusion_schedule:defaultIntrusionSchedule(),directory_id:null,options:defaultOptions(),frame_interval_seconds:1})
const selectTemplate = (scene:SceneType) => {
  newCamera.scene_type=scene
  const item=templates.value.find(t=>t.scene_type===scene)
  if(item){newCamera.modes=[...item.modes];newCamera.schedule=deepCopy(item.schedule);newCamera.intrusion_schedule=deepCopy(item.intrusion_schedule||defaultIntrusionSchedule());newCamera.options={...defaultOptions(),...item.options}}
  else {newCamera.modes=['black_screen'];newCamera.schedule=emptySchedule()}
}
const defaultGeometry = (scene:SceneType) => {
  const flow_roi=[[0,0],[1,0],[1,1],[0,1]]
  if(scene==='workstation') return {post_roi:[[0,0],[1,0],[1,1],[0,1]],flow_roi,intrusion_zone:null}
  if(scene==='security_area') return {post_roi:[],flow_roi,intrusion_zone:{name:'禁区',points:[[0,0],[1,0],[1,1],[0,1]]}}
  return {post_roi:[],flow_roi,intrusion_zone:null}
}
const toggleMode = (target:any, mode:Mode) => {
  const list=target.modes as Mode[]; const index=list.indexOf(mode)
  if(index>=0) list.splice(index,1); else list.push(mode)
}
const createCamera = async () => {
  try {
    if(!newCamera.id||!newCamera.name||!newCamera.rtsp_url) throw new Error('请填写摄像头 ID、名称和视频源')
    if(!/^[A-Za-z0-9_-]+$/.test(newCamera.id)) throw new Error('摄像头 ID 只能使用英文字母、数字、短横线和下划线')
    if(!/^(rtsp|rtsps|file):\/\//.test(newCamera.rtsp_url)) throw new Error('视频源必须以 rtsp://、rtsps:// 或 file:// 开头')
    if(!newCamera.modes.length) throw new Error('请至少选择一种检测模式')
    const created=await api('/api/cameras',{method:'POST',body:JSON.stringify({...newCamera,geometry:defaultGeometry(newCamera.scene_type)})})
    notify('摄像头已添加，请继续校准检测区域'); await loadAll(true)
    Object.assign(newCamera,{id:'',name:'',rtsp_url:'',enabled:true,scene_type:'workstation',modes:[],schedule:emptySchedule(),intrusion_schedule:defaultIntrusionSchedule(),directory_id:null,options:defaultOptions(),frame_interval_seconds:1}); selectTemplate('workstation')
    openEditor(created)
  } catch(error:any){notify(error.message,'error')}
}
const removeCamera = async (camera:Camera) => {
  const confirmed=await dialogConfirm({title:'删除摄像头',message:`确定删除「${camera.name}」？历史告警会保留。`,confirmText:'删除',danger:true})
  if(!confirmed) return
  try{await api(`/api/cameras/${camera.id}`,{method:'DELETE'});notify('摄像头已删除');await loadAll(true)}catch(error:any){notify(error.message,'error')}
}
const analyze = async (camera:Camera) => {
  try{notify(`${camera.name} 已提交即时分析`);await api(`/api/cameras/${camera.id}/analyze`,{method:'POST'});await loadAll(true)}catch(error:any){notify(error.message,'error')}
}
const batchCreated=async(count:number)=>{batchModal.value=false;notify(`成功添加 ${count} 个视频源`);await loadAll(true)}
const configureOffDutySchedules=()=>{
  const count=cameras.value.filter(camera=>camera.modes.includes('off_duty')).length
  if(!count){notify('当前没有启用离岗检测的摄像头','error');return}
  offDutyScheduleModal.value=true
}
const saveOffDutySchedules=async()=>{
  if(!offDutyScheduleForm.days.length){notify('请至少选择一天','error');return}
  const shifts=deepCopy(offDutyScheduleForm.shifts)
  if(!shifts.length){notify('请至少配置一个离岗时段','error');return}
  if(shifts.some(shift=>!shift.start||!shift.end||shift.start===shift.end)){notify('请填写有效的开始和结束时间','error');return}
  if(shifts.some(shift=>shift.off_duty_seconds<60||shift.off_duty_seconds>86400)){notify('离岗判定时间必须为 1 到 1440 分钟','error');return}
  const weekly=Object.fromEntries(offDutyScheduleForm.days.map(day=>[day,deepCopy(shifts)]))
  offDutyScheduleSaving.value=true
  try{
    const result=await api<{updated:number}>('/api/cameras/batch-off-duty-schedule',{method:'POST',body:JSON.stringify({timezone:'Asia/Shanghai',weekly,holidays:[]})})
    offDutyScheduleModal.value=false;notify(`已统一配置 ${result.updated} 个摄像头的离岗时间`);await loadAll(true)
  }catch(error:any){notify(error.message,'error')}
  finally{offDutyScheduleSaving.value=false}
}

const editForm = reactive<any>({id:'',name:'',rtsp_url:'',enabled:true,scene_type:'custom',modes:[],geometry:defaultGeometry('custom'),schedule:emptySchedule(),intrusion_schedule:defaultIntrusionSchedule(),directory_id:null,options:defaultOptions(),zone_name:'禁区',frame_interval_seconds:60})
const openEditor = (camera:Camera) => {
  editor.value=camera
  Object.assign(editForm,{id:camera.id,name:camera.name,rtsp_url:'',enabled:camera.enabled,scene_type:camera.scene_type,modes:[...camera.modes],geometry:deepCopy(camera.geometry||defaultGeometry('custom')),schedule:deepCopy(camera.schedule||emptySchedule()),intrusion_schedule:deepCopy(camera.intrusion_schedule||defaultIntrusionSchedule()),directory_id:camera.directory_id??null,options:{...defaultOptions(),...(camera.options||{})},zone_name:camera.geometry?.intrusion_zone?.name||'禁区',frame_interval_seconds:camera.frame_interval_seconds||60})
  drawLayer.value = camera.scene_type==='security_area'?'intrusion_zone':'post_roi'
}
const editorTemplate = (scene:SceneType) => {
  editForm.scene_type=scene
  const item=templates.value.find(t=>t.scene_type===scene)
  if(item){editForm.modes=[...item.modes];editForm.schedule=deepCopy(item.schedule);editForm.intrusion_schedule=deepCopy(item.intrusion_schedule||defaultIntrusionSchedule());editForm.options={...defaultOptions(),...item.options};editForm.geometry=defaultGeometry(scene)}
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
  current.push(p); setPoints(drawLayer.value,current)
}
const clearLayer = () => setPoints(drawLayer.value,[])
const fullScreenFlow = () => setPoints('flow_roi',[[0,0],[1,0],[1,1],[0,1]])
const fullScreenIntrusion = () => setPoints('intrusion_zone',[[0,0],[1,0],[1,1],[0,1]])
const optionMinutes = (key:string) => Math.round(Number(editForm.options[key]||0)/60)
const setOptionMinutes = (key:string,event:Event) => {editForm.options[key]=Number((event.target as HTMLInputElement).value)*60}
const usesPersonDetection = computed(()=>editForm.modes.some((mode:Mode)=>['off_duty','on_duty','people_flow','intrusion'].includes(mode)))
const polygon = (points:Point[]) => points.map(p=>`${p[0]*100},${p[1]*100}`).join(' ')
const saveEditor = async () => {
  if(!editor.value) return
  try {
    if(!editForm.id||!editForm.name) throw new Error('请填写摄像头 ID 和显示名称')
    if(!/^[A-Za-z0-9_-]+$/.test(editForm.id)) throw new Error('摄像头 ID 只能使用英文字母、数字、短横线和下划线')
    if(editForm.rtsp_url&&!/^(rtsp|rtsps|file):\/\//.test(editForm.rtsp_url)) throw new Error('视频源必须以 rtsp://、rtsps:// 或 file:// 开头')
    if(editForm.modes.includes('off_duty')||editForm.modes.includes('phone_use')||editForm.modes.includes('on_duty')) if(pointsFor('post_roi').length<3) throw new Error('岗位区域至少需要 3 个点')
    if(editForm.modes.includes('off_duty')&&!Object.keys(editForm.schedule.weekly||{}).length) throw new Error('离岗检测请至少选择一个生效星期')
    const configuredShifts=Object.values(editForm.schedule.weekly||{}).flat() as any[]
    if(editForm.modes.includes('off_duty')&&configuredShifts.some(shift=>!shift.off_duty_seconds||shift.off_duty_seconds<60||shift.off_duty_seconds>86400)) throw new Error('每个离岗时段的判定时间必须为 1 到 1440 分钟')
    if(editForm.modes.includes('intrusion')&&pointsFor('intrusion_zone').length<3) throw new Error('区域入侵需要至少 3 个点的禁区')
    if(editForm.modes.includes('people_flow')&&pointsFor('flow_roi').length<3) throw new Error('人流检测需要至少 3 个点的 ROI')
    if(editForm.modes.includes('phone_use')&&editForm.options.behavior_interval_seconds>editForm.options.phone_use_seconds) throw new Error('大模型检测间隔不能大于玩手机判定时间')
    if(editForm.geometry.intrusion_zone) editForm.geometry.intrusion_zone.name=editForm.zone_name||'禁区'
    const id=editor.value.id
    const body=deepCopy(editForm);delete body.zone_name;if(!body.rtsp_url)delete body.rtsp_url
    const updated=await api<Camera>(`/api/cameras/${encodeURIComponent(id)}`,{method:'PATCH',body:JSON.stringify(body)})
    const index=cameras.value.findIndex(camera=>camera.id===id);if(index>=0)cameras.value[index]=updated
    editor.value=null;notify('修改成功');await loadAll(true)
  } catch(error:any){notify(error.message,'error')}
}
const weekdays=[['0','一'],['1','二'],['2','三'],['3','四'],['4','五'],['5','六'],['6','日']]
const scheduleSummary=computed(()=>{
  const weekly=editForm.schedule.weekly||{}
  const days=Object.keys(weekly).sort().map(day=>weekdays.find(item=>item[0]===day)?.[1]).filter(Boolean)
  const firstDay=Object.keys(weekly)[0]
  const count=firstDay?(weekly[firstDay]?.length||0):0
  return days.length?`周${days.join('、')} · ${count} 个时段`:'未配置普通模式排班'
})
const openCameraSchedule=()=>{
  const weekly=editForm.schedule.weekly||{}
  const days=Object.keys(weekly).sort()
  const firstDay=days[0]
  cameraScheduleDraft.days=[...days]
  cameraScheduleDraft.shifts=deepCopy(firstDay?weekly[firstDay]:defaultOffDutyShifts()).map((shift:any)=>({
    ...shift,off_duty_seconds:shift.off_duty_seconds??editForm.options.off_duty_seconds,
  }))
  cameraScheduleModal.value=true
}
const toggleScheduleDay=(day:string)=>{
  cameraScheduleDraft.days=cameraScheduleDraft.days.includes(day)
    ?cameraScheduleDraft.days.filter(item=>item!==day)
    :[...cameraScheduleDraft.days,day].sort()
}
const addScheduleShift=()=>cameraScheduleDraft.shifts.push({start:'17:00',end:'18:00',off_duty_seconds:300})
const removeScheduleShift=(index:number)=>{
  if(cameraScheduleDraft.shifts.length<=1){notify('请至少保留一个排班时段','error');return}
  cameraScheduleDraft.shifts.splice(index,1)
}
const setShiftMinutes=(shift:any,event:Event)=>{shift.off_duty_seconds=Number((event.target as HTMLInputElement).value)*60}
const addBatchOffDutyShift=()=>offDutyScheduleForm.shifts.push({start:'17:00',end:'18:00',off_duty_seconds:300})
const removeBatchOffDutyShift=(index:number)=>{
  if(offDutyScheduleForm.shifts.length<=1){notify('请至少保留一个离岗时段','error');return}
  offDutyScheduleForm.shifts.splice(index,1)
}
const setBatchShiftMinutes=(shift:any,event:Event)=>{shift.off_duty_seconds=Number((event.target as HTMLInputElement).value)*60}
const applyCameraSchedule=()=>{
  if(editForm.modes.includes('off_duty')&&!cameraScheduleDraft.days.length){notify('离岗检测请至少选择一天','error');return}
  const shifts=deepCopy(cameraScheduleDraft.shifts)
  if(cameraScheduleDraft.days.length&&!shifts.length){notify('请至少配置一个排班时段','error');return}
  if(shifts.some((shift:any)=>!shift.start||!shift.end||shift.start===shift.end)){notify('请填写有效的开始和结束时间','error');return}
  if(editForm.modes.includes('off_duty')&&shifts.some((shift:any)=>!shift.off_duty_seconds||shift.off_duty_seconds<60||shift.off_duty_seconds>86400)){notify('离岗判定时间必须为 1 到 1440 分钟','error');return}
  editForm.schedule={
    ...(editForm.schedule||emptySchedule()),
    weekly:Object.fromEntries(cameraScheduleDraft.days.map(day=>[day,deepCopy(shifts)])),
  }
  cameraScheduleModal.value=false
}
const intrusionShift=computed(()=>editForm.intrusion_schedule?.weekly?.['0']?.[0]||{start:'20:00',end:'05:00'})
const syncIntrusionShift=()=>{const shift=deepCopy(intrusionShift.value);editForm.intrusion_schedule=defaultIntrusionSchedule();for(const d of Object.keys(editForm.intrusion_schedule.weekly))editForm.intrusion_schedule.weekly[d]=[deepCopy(shift)]}

const createDirectory=async()=>{const name=await dialogConfirm({title:'新建目录',message:'为摄像头目录设置名称。',input:true,inputLabel:'目录名称',confirmText:'创建'});if(!name)return;try{await api('/api/camera-directories',{method:'POST',body:JSON.stringify({name})});notify('目录已创建');await loadAll(true)}catch(error:any){notify(error.message,'error')}}
const renameDirectory=async(directory:CameraDirectory)=>{const name=await dialogConfirm({title:'重命名目录',message:`当前名称：${directory.name}`,input:true,inputLabel:'目录名称',inputValue:directory.name,confirmText:'保存'});if(!name)return;try{await api(`/api/camera-directories/${directory.id}`,{method:'PATCH',body:JSON.stringify({name})});notify('目录已重命名');await loadAll(true)}catch(error:any){notify(error.message,'error')}}
const removeDirectory=async(directory:CameraDirectory)=>{const ok=await dialogConfirm({title:'删除目录',message:`删除「${directory.name}」？其中 ${directory.camera_count} 个摄像头将移到未分组。`,confirmText:'删除目录',danger:true});if(!ok)return;try{await api(`/api/camera-directories/${directory.id}`,{method:'DELETE'});if(activeDirectory.value===String(directory.id))activeDirectory.value='unassigned';notify('目录已删除，摄像头已移到未分组');await loadAll(true)}catch(error:any){notify(error.message,'error')}}
const moveSelectedCameras=async()=>{if(!selectedCameraIds.value.length)return;const directoryId=moveDirectory.value?Number(moveDirectory.value):null;try{const result=await api('/api/cameras/batch-move',{method:'POST',body:JSON.stringify({ids:selectedCameraIds.value,directory_id:directoryId})});selectedCameraIds.value=[];notify(`已移动 ${result.moved} 个摄像头`);await loadAll(true)}catch(error:any){notify(error.message,'error')}}

const exportSelectedAlerts=async()=>{if(!selectedAlertIds.value.length)return;try{await download('/api/alerts/export',{alert_ids:selectedAlertIds.value},`告警-已选择-${new Date().toISOString().slice(0,10)}.xlsx`);notify(`已导出 ${selectedAlertIds.value.length} 条告警`)}catch(error:any){notify(error.message,'error')}}
const exportFilteredAlerts=async()=>{try{await download('/api/alerts/export',{date:alertFilter.date||null,mode:alertFilter.mode||null,severity:alertFilter.severity||null},`告警-${alertFilter.date||'全部'}.xlsx`);notify('筛选结果已导出')}catch(error:any){notify(error.message,'error')}}
const deleteSelectedAlerts=async()=>{
  if(!selectedAlertIds.value.length)return
  const ids=[...selectedAlertIds.value]
  const confirmed=await dialogConfirm({title:'删除勾选告警',message:`确定永久删除已勾选的 ${ids.length} 条告警及其证据图片？此操作不可恢复。`,confirmText:`删除 ${ids.length} 条`,danger:true})
  if(!confirmed)return
  try{
    const result=await api<{deleted:number;deleted_ids:number[];missing_ids:number[]}>('/api/alerts/batch-delete',{method:'POST',body:JSON.stringify({alert_ids:ids})})
    selectedAlertIds.value=[]
    expandedDeliveryIds.value=expandedDeliveryIds.value.filter(id=>!result.deleted_ids.includes(id))
    notify(`已删除 ${result.deleted} 条告警`)
    await loadAll(true)
  }catch(error:any){notify(error.message,'error')}
}

const modelSettings=reactive<any>({provider:'mock',base_url:'',api_key:'',economy_model:'qwen-vl',api_key_configured:false})
const detectorSettings=reactive<any>({general_model:'yolo26s.pt',general_device:'cpu',fire_smoke_model:'models/fire_smoke_yolov8.pt',fire_smoke_device:'cpu',model_sha256:'',license_name:'AGPL-3.0 (internal pilot only)',runtime:{}})
const retentionSettings=reactive<any>({alert_retention_days:30,log_retention_days:30,auto_cleanup_enabled:true})
const displaySettings=reactive({show_traffic_report:true,show_current_store_count:true})
const visibleTabs=computed(()=>tabs.filter(tab=>(isAdmin.value||['dashboard','traffic','alerts'].includes(tab.key))&&(tab.key!=='traffic'||displaySettings.show_traffic_report)))
const loadSettings=async()=>{try{const [m,d,r,s]=await Promise.all([api('/api/settings/models'),api('/api/settings/detectors'),api('/api/settings/retention'),api('/api/settings/display')]);Object.assign(modelSettings,m);Object.assign(detectorSettings,d);Object.assign(retentionSettings,r);Object.assign(displaySettings,s)}catch(error:any){notify(`系统配置读取失败：${error.message}`,'error')}}
const loadWebhooks=async()=>{try{const result=await api(isAdmin.value?'/api/settings/webhooks':'/api/alert-webhook-targets');webhookTargets.value=result.items}catch(error:any){notify(`Webhook 配置读取失败：${error.message}`,'error')}}
const saveDisplaySettings=async()=>{try{Object.assign(displaySettings,await api('/api/settings/display',{method:'PATCH',body:JSON.stringify(displaySettings)}));if(active.value==='traffic'&&!displaySettings.show_traffic_report)setTab('dashboard');notify('人流数据展示设置已保存')}catch(error:any){notify(error.message,'error');await loadSettings()}}
const saveModels=async()=>{try{
  const baseUrl=String(modelSettings.base_url||'').trim().replace(/\/+$/,'')
  if(modelSettings.provider!=='mock'&&!baseUrl){notify('外部模型必须填写 Base URL 和 API Key','error');return}
  if(baseUrl&&!/^https?:\/\//i.test(baseUrl)){notify('Base URL 必须以 http:// 或 https:// 开头','error');return}
  if(baseUrl){try{new URL(baseUrl)}catch{notify('Base URL 不是合法的 URL','error');return}}
  await api('/api/settings/models',{method:'PUT',body:JSON.stringify(modelSettings)});notify('视觉大模型配置已保存');await loadSettings()
}catch(error:any){notify(error.message,'error')}}
const testModels=async()=>{testing.value=true;try{const r=await api('/api/settings/models/test',{method:'POST'});notify(`模型连接成功，延迟 ${r.latency_ms||0}ms`)}catch(error:any){notify(error.message,'error')}finally{testing.value=false}}
const resetWebhookForm=()=>Object.assign(webhookForm,{id:null,name:'',enabled:false,url:'',auto_severities:[]})
const editWebhook=(target:any)=>Object.assign(webhookForm,{...target,auto_severities:[...target.auto_severities]})
const saveWebhook=async()=>{try{const id=webhookForm.id;const path=id?`/api/settings/webhooks/${id}`:'/api/settings/webhooks';await api(path,{method:id?'PUT':'POST',body:JSON.stringify(webhookForm)});notify(`Webhook 已${id?'更新':'新增'}`);resetWebhookForm();await loadWebhooks()}catch(error:any){notify(error.message,'error')}}
const removeWebhook=async(target:any)=>{const ok=await dialogConfirm({title:'删除 Webhook',message:`确定删除「${target.name}」？历史投递记录仍会保留。`,confirmText:'删除',danger:true});if(!ok)return;try{await api(`/api/settings/webhooks/${target.id}`,{method:'DELETE'});notify('Webhook 已删除');if(webhookForm.id===target.id)resetWebhookForm();await loadWebhooks()}catch(error:any){notify(error.message,'error')}}
const testWebhook=async(target:any)=>{try{await api(`/api/settings/webhooks/${target.id}/test`,{method:'POST'});notify(`${target.name} 连接测试成功`)}catch(error:any){notify(error.message,'error')}}
const openWebhookSend=()=>{if(!selectedAlertIds.value.length){notify('请先选择告警','error');return}selectedTargetIds.value=[];sendModal.value=true}
const manualSend=async()=>{if(!selectedTargetIds.value.length){notify('请选择至少一个 Webhook','error');return}try{const result=await api('/api/alerts/webhook-send',{method:'POST',body:JSON.stringify({alert_ids:selectedAlertIds.value,webhook_target_ids:selectedTargetIds.value})});notify(`已完成 ${result.deliveries} 次投递`);sendModal.value=false;selectedAlertIds.value=[];await loadAll(true)}catch(error:any){notify(error.message,'error')}}
const saveRetention=async()=>{try{await api('/api/settings/retention',{method:'PUT',body:JSON.stringify(retentionSettings)});notify('数据保留策略已保存');await loadSettings()}catch(error:any){notify(error.message,'error')}}
const cleanupAlerts=async()=>{
  const answer=await dialogConfirm({title:'清理历史告警',message:'删除多少天前的告警记录及证据图片？范围 1-365 天。',input:true,inputType:'number',inputLabel:'保留天数',inputValue:String(retentionSettings.alert_retention_days||30),confirmText:'继续',danger:true})
  if(answer===null||answer===false) return
  const n=parseInt(String(answer))
  if(isNaN(n)||n<1||n>365){notify('保留天数必须在 1-365 之间','error');return}
  const confirmed=await dialogConfirm({title:'确认清理',message:`将删除 ${n} 天前的全部告警记录及其证据图片，该操作不可恢复。`,confirmText:'确认清理',danger:true})
  if(!confirmed) return
  try{const r=await api(`/api/alerts?before_days=${n}`,{method:'DELETE'});notify(`已清理 ${r.deleted} 条告警`);await loadAll(true)}catch(error:any){notify(error.message,'error')}
}
const saveDetectors=async()=>{try{const body={...detectorSettings};delete body.runtime;delete body.updated_at;await api('/api/settings/detectors',{method:'PUT',body:JSON.stringify(body)});notify('本地检测器配置已保存并重新加载');await loadSettings()}catch(error:any){notify(error.message,'error')}}
const logPages=computed(()=>Math.max(1,Math.ceil(logData.total/logData.page_size)))
const logDateValue=(value:string,end=false)=>{if(!value)return '';const date=new Date(`${value}T00:00:00`);if(end)date.setDate(date.getDate()+1);return date.toISOString()}
const activeLogFilters=()=>{
  const values:any={start:logDateValue(logFilters.start),end:logDateValue(logFilters.end,true)}
  if(logCategory.value==='audit')Object.assign(values,{username:logFilters.username,action:logFilters.action,outcome:logFilters.outcome})
  if(logCategory.value==='analyses')Object.assign(values,{camera_id:logFilters.camera_id,mode:logFilters.mode,status:logFilters.status})
  if(logCategory.value==='model-calls')Object.assign(values,{camera_id:logFilters.camera_id,model:logFilters.model,stage:logFilters.stage,outcome:logFilters.outcome})
  return Object.fromEntries(Object.entries(values).filter(([,value])=>value!==''))
}
const loadLogs=async(page=1)=>{if(!isAdmin.value)return;logLoading.value=true;try{const params=new URLSearchParams({...activeLogFilters(),page:String(page),page_size:String(logData.page_size)});const result=await api(`/api/logs/${logCategory.value}?${params}`);Object.assign(logData,result);expandedLogId.value=null}catch(error:any){notify(`日志加载失败：${error.message}`,'error')}finally{logLoading.value=false}}
const switchLogCategory=(category:LogCategory)=>{logCategory.value=category;logData.page=1;expandedLogId.value=null;void loadLogs(1)}
const resetLogFilters=()=>{Object.assign(logFilters,{start:'',end:'',username:'',action:'',camera_id:'',mode:'',status:'',model:'',stage:'',outcome:''});void loadLogs(1)}
const logDetailKey=(id:number)=>`${logCategory.value}:${id}`
const toggleLogDetail=async(row:any)=>{if(expandedLogId.value===row.id){expandedLogId.value=null;return}try{const key=logDetailKey(row.id);if(!logDetails[key])logDetails[key]=await api(`/api/logs/${logCategory.value}/${row.id}`);expandedLogId.value=row.id}catch(error:any){notify(error.message,'error')}}
const exportLogs=async()=>{try{await download(`/api/logs/${logCategory.value}/export`,activeLogFilters(),`${logCategory.value}-logs.csv`);notify('日志已导出')}catch(error:any){notify(error.message,'error')}}
const logResultText=(row:any)=>row.outcome==='success'?'成功':row.outcome==='failure'||row.outcome==='error'?'失败':row.status||row.outcome
const prettyLog=(value:any)=>JSON.stringify(value,null,2)
const setTab=(name:string)=>{if(!visibleTabs.value.some(tab=>tab.key===name))name='dashboard';if(name==='traffic'&&!displaySettings.show_traffic_report){notify('人流报表已在系统配置中关闭','error');name='dashboard'}active.value=name;location.hash=name;if(name==='traffic')void loadTrafficMonthly();if(name==='settings')loadSettings();if(name==='webhooks')loadWebhooks();if(name==='users')loadUsers();if(name==='logs')loadLogs(logData.page)}
const focusDashboardCamera=async(row:TrafficCameraSummary|null)=>{
  if(!row)return
  const camera=cameras.value.find(item=>item.id===row.camera_id)
  if(!camera)return
  window.clearTimeout(focusCameraTimer)
  dashboardCameraSearch.value=camera.name
  focusedCameraId.value=camera.id
  setTab('dashboard')
  await nextTick()
  document.getElementById(`camera-card-${camera.id}`)?.scrollIntoView({behavior:'smooth',block:'center'})
  focusCameraTimer=window.setTimeout(()=>{if(focusedCameraId.value===camera.id)focusedCameraId.value=''},2000)
}
const scrollToAdd=()=>document.getElementById('add-camera')?.scrollIntoView({behavior:'smooth'})
const formatTime=(value?:string)=>value?new Date(value).toLocaleString('zh-CN',{hour12:false}):'尚未抓帧'
const shortTime=(value?:string)=>value?new Date(value).toLocaleTimeString('zh-CN',{hour12:false}):'--:--:--'
const modeName=(mode:Mode)=>modeInfo[mode]?.name||mode
const eventPhaseName=(phase?:string)=>phase==='threshold'?'达到阈值':phase==='resolved'?'事件结束':''
const eventRange=(item:any)=>item.event_started_at?`${formatTime(item.event_started_at)} 至 ${formatTime(item.event_ended_at||item.created_at)}`:''
const maskedSource=(source:string)=>source?source.replace(/(\/\/[^/:@]+:)[^@]*(?=@)/,'$1****'):''
const onlineCount=computed(()=>cameras.value.filter(c=>c.online).length)
const chartMax=computed(()=>Math.max(1,...trafficSummary.value.store_trend.map(point=>point.current_people)))
const chartPoints=computed(()=>trafficSummary.value.store_trend.map((point,index,rows)=>{
  const x=rows.length===1?500:44+index*912/(rows.length-1)
  const y=220-point.current_people/chartMax.value*176
  return {x,y,...point}
}))
const chartPolyline=computed(()=>chartPoints.value.map(point=>`${point.x},${point.y}`).join(' '))
const chartArea=computed(()=>chartPoints.value.length?`44,220 ${chartPolyline.value} ${chartPoints.value.at(-1)?.x||44},220`:'')
const chartTicks=computed(()=>[chartMax.value,Math.round(chartMax.value/2),0])
const trendTime=(value?:string)=>value?new Date(value).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false}):'--:--'
const podiumRows=(rows:TrafficCameraSummary[])=>([rows[1]||null,rows[0]||null,rows[2]||null])
const snapshotUrl=(camera:Camera)=>`/api/cameras/${encodeURIComponent(camera.id)}/snapshot?v=${encodeURIComponent(camera.last_frame_at||'none')}`

const clearPreviewHeartbeat=()=>{
  window.clearInterval(previewHeartbeatTimer)
  previewHeartbeatTimer=undefined
}
const releasePreviewLease=(keepalive=false)=>{
  const camera=preview.value
  const sessionId=previewSessionId.value
  clearPreviewHeartbeat()
  previewStreamUrl.value=''
  previewSessionId.value=''
  if(!camera||!sessionId) return Promise.resolve()
  return api(`/api/cameras/${encodeURIComponent(camera.id)}/preview/stop`,{
    method:'POST',body:JSON.stringify({session_id:sessionId}),keepalive,
  }).then(()=>undefined).catch(()=>undefined)
}
const closePreview=async()=>{
  const release=releasePreviewLease()
  preview.value=null;previewLoading.value=false;previewError.value=''
  await release
  await loadAll(true)
}
const openPreview=async(camera:Camera)=>{
  if(preview.value) await closePreview()
  preview.value=camera;previewLoading.value=true;previewError.value=''
  try{
    const session=await api<{session_id:string;stream_url:string;heartbeat_interval_seconds:number}>(`/api/cameras/${encodeURIComponent(camera.id)}/preview/start`,{method:'POST'})
    previewSessionId.value=session.session_id
    previewStreamUrl.value=`${session.stream_url}&v=${Date.now()}`
    previewHeartbeatTimer=window.setInterval(async()=>{
      try{
        await api(`/api/cameras/${encodeURIComponent(camera.id)}/preview/heartbeat`,{method:'POST',body:JSON.stringify({session_id:session.session_id})})
      }catch(error:any){
        previewError.value=error.message||'实时预览连接已释放，请重新打开。'
        await releasePreviewLease()
      }
    },Math.max(5000,session.heartbeat_interval_seconds*1000))
  }catch(error:any){
    previewError.value=error.message||'实时预览启动失败'
  }finally{previewLoading.value=false}
}
const abandonPreview=()=>{void releasePreviewLease(true)}

const clearAuthenticatedState=()=>{
  currentUser.value=null;setCsrfToken('');socket?.close();socket=undefined;wsOnline.value=false
  window.clearInterval(refreshTimer);refreshTimer=undefined
  window.clearTimeout(focusCameraTimer);focusCameraTimer=undefined
  active.value='dashboard';dashboardCameraSearch.value='';cameraSettingsSearch.value='';focusedCameraId.value='';selectedCameraIds.value=[]
  void loadCaptcha()
}
const toggleAllCameras = () => {
  selectedCameraIds.value = allCamerasSelected.value ? [] : filteredSettingsCameras.value.map(camera=>camera.id)
}
const removeSelectedCameras = async () => {
  const selected=filteredSettingsCameras.value.filter(camera=>selectedCameraIds.value.includes(camera.id))
  if(!selected.length)return
  const previewNames=selected.slice(0,3).map(camera=>camera.name).join('、')
  const preview=selected.length>3?`${previewNames} 等`:`${previewNames}`
  const confirmed=await dialogConfirm({title:'批量删除摄像头',message:`确定删除 ${selected.length} 个摄像头（${preview}）？此操作不可撤销。`,confirmText:`删除 ${selected.length} 个`,danger:true})
  if(!confirmed)return
  try{
    const result=await api<{deleted:number;missing_ids:string[]}>('/api/cameras/batch-delete',{method:'POST',body:JSON.stringify({ids:selected.map(camera=>camera.id)})})
    selectedCameraIds.value=[];notify(`已删除 ${result.deleted} 个摄像头`);await loadAll(true)
  }catch(error:any){notify(error.message,'error')}
}
const loadUsers=async()=>{if(!isAdmin.value)return;try{users.value=await api('/api/users')}catch(error:any){notify(error.message,'error')}}
const loadCaptcha=async()=>{try{const result=await api<{captcha_id:string;image:string}>('/api/auth/captcha');loginForm.captcha_id=result.captcha_id;loginForm.captcha_answer='';captchaImage.value=result.image}catch(error:any){loginError.value=error.message}}
const login=async()=>{loginBusy.value=true;loginError.value='';try{
  const result=await api<{user:AuthUser;csrf_token:string}>('/api/auth/login',{method:'POST',body:JSON.stringify(loginForm)})
  currentUser.value=result.user;setCsrfToken(result.csrf_token);loginForm.password='';loginForm.captcha_answer='';await startAuthenticated()
}catch(error:any){loginError.value=error.message;await loadCaptcha()}finally{loginBusy.value=false}}
const logout=async()=>{try{await api('/api/auth/logout',{method:'POST'})}catch{}finally{clearAuthenticatedState()}}
const changePassword=async()=>{if(passwordForm.new_password!==passwordForm.confirm){notify('两次输入的新密码不一致','error');return}try{
  await api('/api/auth/password',{method:'PUT',body:JSON.stringify({old_password:passwordForm.old_password,new_password:passwordForm.new_password})})
  passwordModal.value=false;Object.assign(passwordForm,{old_password:'',new_password:'',confirm:''});clearAuthenticatedState();loginError.value='密码已修改，请重新登入'
}catch(error:any){notify(error.message,'error')}}
const createUserAccount=async()=>{try{await api('/api/users',{method:'POST',body:JSON.stringify(userForm)});Object.assign(userForm,{username:'',display_name:'',password:''});notify('普通用户已创建');await loadUsers()}catch(error:any){notify(error.message,'error')}}
const toggleUser=async(user:AuthUser)=>{try{await api(`/api/users/${user.id}`,{method:'PATCH',body:JSON.stringify({enabled:!user.enabled})});notify(user.enabled?'账号已停用':'账号已启用');await loadUsers()}catch(error:any){notify(error.message,'error')}}
const editUserName=async(user:AuthUser)=>{const value=await dialogConfirm({title:'修改显示名称',message:`账号：${user.username}`,input:true,inputLabel:'显示名称',inputValue:user.display_name,confirmText:'保存'});if(!value)return;try{await api(`/api/users/${user.id}`,{method:'PATCH',body:JSON.stringify({display_name:String(value)})});notify('显示名称已更新');await loadUsers()}catch(error:any){notify(error.message,'error')}}
const resetUserPassword=async(user:AuthUser)=>{const value=await dialogConfirm({title:'重置密码',message:`为「${user.display_name}」设置新密码（8-128 位）。`,input:true,inputType:'password',inputLabel:'新密码',confirmText:'确认重置'});if(!value)return;try{await api(`/api/users/${user.id}/reset-password`,{method:'POST',body:JSON.stringify({new_password:String(value)})});notify('密码已重置，该账号的已有会话已注销')}catch(error:any){notify(error.message,'error')}}
const deleteUserAccount=async(user:AuthUser)=>{const ok=await dialogConfirm({title:'删除账号',message:`确定删除「${user.display_name}（${user.username}）」？`,confirmText:'删除',danger:true});if(!ok)return;try{await api(`/api/users/${user.id}`,{method:'DELETE'});notify('账号已删除');await loadUsers()}catch(error:any){notify(error.message,'error')}}

const startAuthenticated=async()=>{
  await Promise.all([loadAll(),loadWebhooks()]);const requested=location.hash.slice(1);setTab(requested||'dashboard')
  selectTemplate('workstation');connectWs();window.clearInterval(refreshTimer);refreshTimer=window.setInterval(()=>loadAll(true),15000)
}

const connectWs=()=>{
  if(!currentUser.value)return
  const protocol=location.protocol==='https:'?'wss':'ws';socket=new WebSocket(`${protocol}://${location.host}/ws/events`)
  socket.onopen=()=>{wsOnline.value=true}
  socket.onmessage=(event)=>{try{const data=JSON.parse(event.data);if(data.type==='alert'){notify(`${data.severity==='critical'?'紧急：':''}${data.camera_name||data.camera_id} ${modeName(data.mode)}：${data.reason}`,'alert');loadAll(true)}}catch{}}
  socket.onclose=()=>{wsOnline.value=false;if(currentUser.value)window.setTimeout(connectWs,3000)}
}
onMounted(async()=>{setUnauthorizedHandler(clearAuthenticatedState);try{const result=await api<{user:AuthUser;csrf_token:string}>('/api/auth/me');currentUser.value=result.user;setCsrfToken(result.csrf_token);await startAuthenticated()}catch{await loadCaptcha()}finally{authReady.value=true}clockTimer=window.setInterval(()=>now.value=new Date(),1000);window.addEventListener('beforeunload',abandonPreview);window.addEventListener('hashchange',()=>{if(currentUser.value)setTab(location.hash.slice(1)||'dashboard')})})
onUnmounted(()=>{window.clearInterval(refreshTimer);window.clearInterval(clockTimer);window.clearTimeout(focusCameraTimer);socket?.close();window.removeEventListener('beforeunload',abandonPreview);abandonPreview()})
</script>

<template>
  <div v-if="!authReady" class="auth-screen"><div class="auth-loader">SYSTEM INITIALIZING</div></div>
  <div v-else-if="!currentUser" class="auth-screen">
    <form class="auth-card" @submit.prevent="login">
      <div class="brand auth-brand"><img class="brand-mark" src="/brand-icon.png" alt="江苏有线"><div class="brand-text"><strong>江苏有线</strong><small>无锡广电 · AI巡检</small></div></div>
      <span class="head-en">SECURE ACCESS</span><h1>账号登入</h1><p>请输入管理员分配的账号和密码</p>
      <label>用户名<input v-model.trim="loginForm.username" autocomplete="username" autofocus></label>
      <label>密码<input v-model="loginForm.password" type="password" autocomplete="current-password"></label>
      <label>图形验证码<div class="captcha-row"><input v-model.trim="loginForm.captcha_answer" maxlength="4" autocomplete="off"><button type="button" title="点击换一张" @click="loadCaptcha"><img v-if="captchaImage" :src="captchaImage" alt="图形验证码"></button></div></label>
      <div v-if="loginError" class="auth-error">{{loginError}}</div>
      <button class="primary wide" :disabled="loginBusy">{{loginBusy?'登入中…':'登入系统'}}</button>
    </form>
  </div>
  <div v-else class="shell">
    <aside class="sidebar">
      <div class="brand">
        <img class="brand-mark" src="/brand-icon.png" alt="江苏有线">
        <div class="brand-text"><strong>江苏有线</strong><small>无锡广电 · AI巡检</small></div>
      </div>
      <nav><button v-for="tab in visibleTabs" :key="tab.key" :class="{active:active===tab.key}" @click="setTab(tab.key)"><TechIcon :name="tab.icon" :size="16"/>{{tab.name}}</button></nav>
      <div class="system-pill">
        <i :class="onlineCount?'good':'warn'"></i>
        <div><b>{{onlineCount}} / {{cameras.length}}</b><small>CAMERAS ONLINE · 安全检测持续运行</small></div>
      </div>
      <div class="account-panel"><b>{{currentUser.display_name}}</b><small>{{isAdmin?'管理员':'普通用户'}} · {{currentUser.username}}</small><div><button @click="passwordModal=true">修改密码</button><button @click="logout">退出</button></div></div>
      <div class="side-foot"><span>WUXI BROADCASTING</span><span>V2.0</span></div>
    </aside>

    <main>
      <header class="topbar">
        <div class="top-title">
          <h1>无锡广电AI巡检系统</h1>
          <span class="top-en">AI INTELLIGENT INSPECTION PLATFORM</span>
        </div>
        <div class="hud-center"><span>{{pageEn}}</span></div>
        <div class="top-actions">
          <div class="clock-stack"><b>{{dateStr}} {{timeStr}}</b><small>LOCAL TIME</small></div>
          <span class="sys-badge" :class="{offline:sysStatusBad}">{{sysStatusText}}</span>
          <button class="icon-btn refresh" :class="{spin:loading}" aria-label="刷新数据" title="刷新数据" @click="loadAll()"><TechIcon name="refresh" :size="17"/></button>
          <button class="icon-btn" aria-label="全屏" title="全屏切换" @click="toggleFullscreen"><TechIcon name="maximize" :size="16"/></button>
        </div>
      </header>
      <div v-if="loading" class="top-loading"></div>

      <!-- ============ 监控总览 DASHBOARD ============ -->
      <section v-if="active==='dashboard'" class="page">
        <div class="metrics">
          <article>
            <span>监控源</span><span class="metric-en">CAMERAS ONLINE</span>
            <strong>{{onlineCount}}<small> / {{cameras.length}}</small></strong>
            <em>{{cameras.length===0?'等待接入':onlineCount===cameras.length?'系统正常运行':'部分摄像头离线'}}</em>
          </article>
          <article v-if="displaySettings.show_current_store_count">
            <span>当前画面人数</span><span class="metric-en">ON-SCREEN VISITORS</span>
            <strong>{{dashboard.current_people||0}}</strong>
            <em>各入口实时统计</em>
          </article>
          <article :class="{'critical-metric':dashboard.critical_alerts_today}">
            <span>今日烟火紧急告警</span><span class="metric-en">FIRE / SMOKE ALERTS</span>
            <strong>{{dashboard.critical_alerts_today||0}}</strong>
            <em>本地模型全天候检测</em>
          </article>
          <article :class="{alerting:dashboard.intrusions_today}">
            <span>今日区域入侵</span><span class="metric-en">INTRUSION ALERTS</span>
            <strong>{{dashboard.intrusions_today||0}}</strong>
            <em>进入禁区即时触发</em>
          </article>
        </div>

        <div class="scene-summary">
          <article v-for="scene in (['workstation','customer_area','security_area','custom'] as SceneType[])" :key="scene">
            <TechIcon :name="sceneInfo[scene].icon" :size="22"/>
            <div><b>{{dashboard.scene_counts?.[scene]||0}}</b><span>{{sceneInfo[scene].name}}</span><small>{{sceneInfo[scene].en}}</small></div>
          </article>
        </div>

        <div class="section-head">
          <div><h2>AI 监控矩阵</h2><span class="head-en">AI MONITORING GRID</span><p>后台按各摄像头抽帧周期更新快照；页面不会自动启动实时视频</p></div>
        </div>
        <div class="camera-searchbar">
          <label><TechIcon name="search" :size="14"/><input v-model="dashboardCameraSearch" type="search" placeholder="按监控名称搜索" aria-label="搜索监控总览"></label>
          <button v-if="dashboardCameraSearch" class="ghost" @click="dashboardCameraSearch=''">清除</button>
          <span><b>{{filteredDashboardCameras.length}}</b> / {{cameras.length}} <small>MATCHED / TOTAL</small></span>
        </div>
        <div class="camera-grid">
          <article v-for="camera in filteredDashboardCameras" :id="`camera-card-${camera.id}`" :key="camera.id" class="camera-card" :class="{'camera-focus':focusedCameraId===camera.id}">
            <div class="camera-shot">
              <img v-if="camera.last_frame_at" :src="snapshotUrl(camera)" loading="lazy" :alt="`${camera.name} 最近快照`">
              <div v-else class="no-signal"><TechIcon name="video" :size="26"/><b>NO SIGNAL</b><span>{{camera.last_error||'等待首次抓帧'}}</span></div>
              <div class="shot-top">
                <span class="cam-live" :class="{off:!camera.online}">{{camera.online?'ONLINE':'OFFLINE'}}</span>
                <span class="cam-time">{{shortTime(camera.last_frame_at)}}</span>
                <span class="cam-id">{{camera.id}}</span>
              </div>
              <div class="shot-bottom">
                <div><h3>{{camera.name}}</h3><p>{{sceneInfo[camera.scene_type]?.name}} · 每 {{camera.frame_interval_seconds}} 秒</p></div>
                <span v-if="camera.enabled" class="ai-tag"><TechIcon name="cpu" :size="10"/>AI ACTIVE</span>
                <span v-else class="ai-tag" style="color:var(--text-muted);border-color:var(--border-dim)">PAUSED</span>
              </div>
              <div v-if="isAdmin" class="shot-actions">
                <button @click="openPreview(camera)"><TechIcon name="play" :size="12"/>实时</button>
                <button @click="analyze(camera)"><TechIcon name="zap" :size="12"/>分析</button>
                <button @click="openEditor(camera)"><TechIcon name="edit" :size="12"/>编辑</button>
              </div>
            </div>
            <dl class="snapshot-meta">
              <div><dt>最近抓帧</dt><dd>{{formatTime(camera.last_frame_at)}}</dd></div>
              <div><dt>抽帧频率</dt><dd>每 {{camera.frame_interval_seconds}} 秒</dd></div>
            </dl>
            <div class="chips"><span v-for="mode in camera.modes" :key="mode">{{modeName(mode)}}</span></div>
            <button v-if="isAdmin" class="preview-button" :class="{active:camera.preview_active}" @click="openPreview(camera)"><TechIcon name="eye" :size="13"/>{{camera.preview_active?'加入实时预览':'查看实时视频'}}</button>
          </article>
          <article v-if="!cameras.length" class="camera-card empty-card">
            <TechIcon name="video" :size="30"/><b class="head-en">NO CAMERA SOURCE</b><span>请先添加监控源</span>
          </article>
          <article v-else-if="!filteredDashboardCameras.length" class="camera-card empty-card">
            <TechIcon name="search" :size="30"/><b class="head-en">NO MATCHED CAMERA</b><span>没有名称匹配“{{dashboardCameraSearch.trim()}}”的监控</span>
          </article>
        </div>

        <div class="two-cols">
          <section class="panel">
            <div class="section-head"><div><h2>最近告警</h2><span class="head-en">RECENT ALERTS</span><p>按严重级别优先排序</p></div><button class="link" @click="setTab('alerts')">查看全部</button></div>
            <div v-if="!alerts.length" class="empty"><b>NO ALERTS</b><p>暂无告警</p></div>
            <div v-for="item in alerts.slice(0,6)" :key="item.id" class="alert-row" :class="`severity-${item.severity}`"><div class="alert-symbol">!</div><div><b>{{modeName(item.mode)}} · {{item.alert_name||item.camera_name}}</b><p>{{item.reason}}</p></div><time>{{formatTime(item.created_at)}}</time></div>
          </section>
          <section class="panel runtime-panel">
            <div class="section-head"><div><h2>运行状态</h2><span class="head-en">SYSTEM RUNTIME</span><p>普通与安全检测独立队列</p></div></div>
            <dl>
              <div><dt>通用 YOLO</dt><dd :class="dashboard.runtime?.detectors?.general?.status==='ready'?'ok':''">{{dashboard.runtime?.detectors?.general?.status||'unknown'}}</dd></div>
              <div><dt>烟火模型</dt><dd :class="{'amber':dashboard.runtime?.detectors?.fire_smoke?.status!=='ready'}">{{dashboard.runtime?.detectors?.fire_smoke?.status||'unknown'}}</dd></div>
              <div><dt>普通队列</dt><dd>{{dashboard.runtime?.queues?.general||dashboard.runtime?.queue_depth||0}}</dd></div>
              <div><dt>安全队列</dt><dd>{{dashboard.runtime?.queues?.fire||0}}</dd></div>
            </dl>
          </section>
        </div>
      </section>

      <!-- ============ 摄像头配置 CAMERA MANAGEMENT ============ -->
      <section v-else-if="active==='cameras'" class="page">
        <div class="split-layout">
          <section id="add-camera" class="panel add-panel">
            <div class="section-head"><div><h2>添加监控源</h2><span class="head-en">ADD CAMERA SOURCE</span><p>先选择场景，系统自动带出策略</p></div></div>
            <div class="camera-config-scroll">
              <div class="scene-picker">
                <button v-for="scene in (['workstation','customer_area','security_area','custom'] as SceneType[])" :key="scene" :class="{selected:newCamera.scene_type===scene}" @click="selectTemplate(scene)"><TechIcon :name="sceneInfo[scene].icon" :size="20"/><b>{{sceneInfo[scene].name}}</b></button>
              </div>
              <CameraBasicFields :model="newCamera" />
              <label>所属目录<select v-model="newCamera.directory_id"><option :value="null">未分组</option><option v-for="directory in cameraDirectories" :key="directory.id" :value="directory.id">{{directory.name}}</option></select></label>
              <div class="field-title">启用能力 <small>可多选</small></div>
              <div class="mode-picker">
                <button v-for="(info,mode) in modeInfo" :key="mode" :class="{selected:newCamera.modes.includes(mode)}" @click="toggleMode(newCamera,mode)"><TechIcon :name="info.icon" :size="17"/><span><b>{{info.name}}</b><small>{{info.note}}</small></span></button>
              </div>
              <button class="primary wide" @click="createCamera"><TechIcon name="plus" :size="14"/>添加并配置区域</button>
              <div class="config-note"><b>安全说明</b><p>烟火、黑屏全天运行；区域入侵默认每天 20:00 至次日 05:00。视频烟火预警不能替代认证消防设备。</p></div>
            </div>
          </section>

          <section class="panel source-list">
            <div class="section-head">
              <div><h2>已有监控源</h2><span class="head-en">CAMERA SOURCES · {{cameras.length}}</span><p>原有摄像头保持自定义场景与原配置</p></div>
              <div class="actions">
                <button class="ghost" @click="createDirectory"><TechIcon name="plus" :size="13"/>新建目录</button>
                <button class="ghost" @click="configureOffDutySchedules"><TechIcon name="refresh" :size="13"/>一键配置离岗时间</button>
                <label v-if="cameras.length" class="camera-select-all"><input type="checkbox" :checked="allCamerasSelected" @change="toggleAllCameras">全选</label>
                <button class="danger" :disabled="!selectedCameraIds.length" @click="removeSelectedCameras"><TechIcon name="trash" :size="13"/>批量删除 ({{selectedCameraIds.length}})</button>
                <button class="ghost" @click="batchModal=true"><TechIcon name="layers" :size="13"/>批量添加</button>
                <button class="primary" @click="scrollToAdd"><TechIcon name="plus" :size="13"/>添加视频源</button>
              </div>
            </div>
            <div class="camera-directory-layout">
              <div class="directory-bar">
                <div class="directory-summary"><span>目录数量</span><b>{{cameraDirectories.length}}</b></div>
                <button :class="{active:activeDirectory==='all'}" @click="activeDirectory='all';selectedCameraIds=[]"><span>全部</span><b>{{cameras.length}}</b></button>
                <button :class="{active:activeDirectory==='unassigned'}" @click="activeDirectory='unassigned';selectedCameraIds=[]"><span>未分组</span><b>{{unassignedCameraCount}}</b></button>
                <span v-for="directory in cameraDirectories" :key="directory.id" :class="{active:activeDirectory===String(directory.id)}">
                  <button :title="directory.name" @click="activeDirectory=String(directory.id);selectedCameraIds=[]"><span>{{directory.name}}</span><b>{{directory.camera_count}}</b></button>
                  <button class="directory-action" title="重命名" @click="renameDirectory(directory)">✎</button>
                  <button class="directory-action danger" title="删除" @click="removeDirectory(directory)">×</button>
                </span>
              </div>
              <div class="source-list-content">
                <div v-if="selectedCameraIds.length" class="batch-move-bar"><select v-model="moveDirectory"><option value="">未分组</option><option v-for="directory in cameraDirectories" :key="directory.id" :value="String(directory.id)">{{directory.name}}</option></select><button class="primary" @click="moveSelectedCameras">移动所选 ({{selectedCameraIds.length}})</button></div>
                <div class="camera-searchbar compact">
                  <label><TechIcon name="search" :size="14"/><input v-model="cameraSettingsSearch" type="search" placeholder="按监控名称搜索" aria-label="搜索摄像头设置"></label>
                  <button v-if="cameraSettingsSearch" class="ghost" @click="cameraSettingsSearch=''">清除</button>
                  <span><b>{{filteredSettingsCameras.length}}</b> / {{directoryCameras.length}}</span>
                </div>
                <article v-for="camera in filteredSettingsCameras" :key="camera.id">
                  <label class="camera-select" :aria-label="`选择 ${camera.name}`"><input v-model="selectedCameraIds" type="checkbox" :value="camera.id"></label>
                  <i class="source-state" :class="camera.online?'ok':'bad'"></i>
                  <div class="source-main">
                    <h3>{{camera.name}}<small>{{sceneInfo[camera.scene_type]?.name}} · 每 {{camera.frame_interval_seconds}} 秒 · {{camera.id}}</small></h3>
                    <p>{{maskedSource(camera.source)}}</p>
                    <div class="chips"><span v-for="mode in camera.modes" :key="mode">{{modeName(mode)}}</span></div>
                    <small v-if="camera.last_error" class="error-text" :title="camera.last_error">最近抓帧失败：{{camera.last_error}}</small>
                  </div>
                  <div class="source-actions">
                    <button @click="openPreview(camera)"><TechIcon name="play" :size="12"/>实时视频</button>
                    <button @click="analyze(camera)"><TechIcon name="zap" :size="12"/>立即分析</button>
                    <button @click="openEditor(camera)"><TechIcon name="edit" :size="12"/>编辑</button>
                    <button class="danger" @click="removeCamera(camera)"><TechIcon name="trash" :size="12"/>删除</button>
                  </div>
                </article>
                <div v-if="!cameras.length" class="empty"><b>NO CAMERA SOURCE</b><p>暂无监控源，请在左侧添加</p></div>
                <div v-else-if="!filteredSettingsCameras.length" class="empty"><b>NO MATCHED CAMERA</b><p>没有名称匹配“{{cameraSettingsSearch.trim()}}”的监控</p></div>
              </div>
            </div>
          </section>
        </div>
      </section>

      <!-- ============ 告警中心 AI ALERT CENTER ============ -->
      <section v-else-if="active==='alerts'" class="page">
        <section class="panel table-panel">
          <div class="section-head">
            <div><h2>告警中心</h2><span class="head-en">AI ALERT CENTER</span><p>烟火紧急告警置顶；仅确认违规才告警</p></div>
            <div class="actions"><button class="ghost" :disabled="!selectedAlertIds.length" @click="exportSelectedAlerts">导出勾选 ({{selectedAlertIds.length}})</button><button class="ghost" @click="exportFilteredAlerts">导出筛选结果</button><button class="primary" :disabled="!selectedAlertIds.length" @click="openWebhookSend"><TechIcon name="webhook" :size="13"/>发送企业微信 ({{selectedAlertIds.length}})</button><button v-if="isAdmin" class="danger" :disabled="!selectedAlertIds.length" @click="deleteSelectedAlerts"><TechIcon name="trash" :size="13"/>删除勾选 ({{selectedAlertIds.length}})</button><button v-if="isAdmin" class="link" @click="cleanupAlerts"><TechIcon name="trash" :size="13"/>清理历史</button></div>
          </div>
          <div class="filter-bar">
            <label class="filter-item"><span class="filter-label">日期 / DATE</span><input v-model="alertFilter.date" type="date" @change="applyAlertFilter"></label>
            <label class="filter-item"><span class="filter-label">事件类型 / EVENT</span>
              <select v-model="alertFilter.mode" @change="applyAlertFilter">
                <option value="">全部事件</option>
                <option v-for="(info,mode) in modeInfo" :key="mode" :value="mode">{{info.name}}</option>
              </select>
            </label>
            <label class="filter-item"><span class="filter-label">告警级别 / LEVEL</span>
              <select v-model="alertFilter.severity" @change="applyAlertFilter">
                <option value="">全部级别</option>
                <option v-for="lv in severityLevels" :key="lv.value" :value="lv.value">{{lv.label}}</option>
              </select>
            </label>
            <button class="ghost filter-reset" :disabled="!hasAlertFilter" @click="resetAlertFilter"><TechIcon name="refresh" :size="13"/>清除筛选</button>
            <span class="filter-count"><b>{{alertRows.length}}</b> / {{alerts.length}} <small>MATCHED / TOTAL</small></span>
          </div>
          <table v-if="alertRows.length">
            <thead><tr><th><input type="checkbox" :checked="allAlertsSelected" @change="toggleAllAlerts"></th><th>级别</th><th>证据</th><th>名称</th><th>事件</th><th>原因</th><th>Webhook</th><th>时间</th></tr></thead>
            <tbody>
              <template v-for="item in alertRows" :key="item.id">
              <tr :class="`severity-${item.severity}`">
                <td><input v-model="selectedAlertIds" type="checkbox" :value="item.id"></td>
                <td><span class="severity-badge" :class="item.severity">{{item.severity||'normal'}}</span></td>
                <td><EvidencePreview :src="item.evidence_urls?.length?item.evidence_urls:item.evidence_url" alt="告警证据" /></td>
                <td>{{item.alert_name||item.camera_name||'未知告警'}}</td>
                <td><span class="event-type">{{modeName(item.mode)}}</span><small v-if="item.event_phase"><br>{{eventPhaseName(item.event_phase)}}<br>{{eventRange(item)}}</small></td>
                <td class="reason">{{item.reason}}</td>
                <td><button v-if="item.webhook_delivery?.total" class="delivery-summary" @click="toggleDeliveryDetails(item.id)">{{item.webhook_delivery.delivered}}/{{item.webhook_delivery.total}} 成功</button><span v-else class="muted">未发送</span></td>
                <td>{{formatTime(item.created_at)}}</td>
              </tr>
              <tr v-if="expandedDeliveryIds.includes(item.id)" class="delivery-detail-row"><td colspan="8"><div class="delivery-list"><span v-for="delivery in item.webhook_delivery.items" :key="delivery.id" :class="delivery.status"><b>{{delivery.target_name}}</b><em>{{delivery.trigger==='manual'?'手动':'自动'}}</em><strong>{{delivery.status}}</strong><small v-if="delivery.error">{{delivery.error}}</small></span></div></td></tr>
              </template>
            </tbody>
          </table>
          <div v-else-if="alerts.length" class="empty"><b>NO MATCHED ALERTS</b><p>当前筛选条件下没有匹配的告警</p><button class="link" @click="resetAlertFilter">清除筛选</button></div>
          <div v-else class="empty"><b>NO ALERTS</b><p>暂无告警记录</p></div>
        </section>
      </section>

      <!-- ============ 企业微信机器人 WECOM DELIVERY ============ -->
      <section v-else-if="active==='webhooks'" class="page webhook-page">
        <div class="webhook-layout">
          <section class="panel webhook-form">
            <div class="section-head"><div><h2>{{webhookForm.id?'编辑':'新增'}}企业微信机器人</h2><span class="head-en">WECOM BOT CONFIGURATION</span><p>告警将发送 Markdown 摘要和证据图片</p></div></div>
            <label>名称<input v-model.trim="webhookForm.name" placeholder="值班告警群"></label>
            <label>机器人 Webhook URL<input v-model.trim="webhookForm.url" placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."></label>
            <div class="field-title">自动发送告警级别</div>
            <div class="severity-options"><label v-for="level in severityLevels" :key="level.value"><input v-model="webhookForm.auto_severities" type="checkbox" :value="level.value"><span :class="level.value">{{level.label}}</span></label></div>
            <div class="inline-setting"><div><b>启用目标</b><small>关闭后不参与自动或手动发送</small></div><label class="switch"><input v-model="webhookForm.enabled" type="checkbox"><span></span></label></div>
            <div class="actions"><button class="primary" @click="saveWebhook">{{webhookForm.id?'保存修改':'新增 Webhook'}}</button><button v-if="webhookForm.id" class="ghost" @click="resetWebhookForm">取消编辑</button></div>
          </section>
          <section class="webhook-cards">
            <article v-for="target in webhookTargets" :key="target.id" class="panel webhook-card" :class="{disabled:!target.enabled}">
              <div class="section-head"><div><h2>{{target.name}}</h2><span class="head-en">TARGET #{{target.id}}</span></div><span class="status-dot" :class="{ready:target.enabled,warn:!target.enabled}">{{target.enabled?'● ENABLED':'● DISABLED'}}</span></div>
              <code>{{target.url}}</code>
              <div class="webhook-levels"><span v-for="level in target.auto_severities" :key="level" :class="level">{{level}}</span><small v-if="!target.auto_severities.length">不自动发送</small></div>
              <p>企业微信机器人 · 更新于 {{formatTime(target.updated_at)}}</p>
              <div class="actions"><button class="ghost" @click="editWebhook(target)"><TechIcon name="edit" :size="12"/>编辑</button><button class="ghost" @click="testWebhook(target)"><TechIcon name="zap" :size="12"/>测试</button><button class="danger" @click="removeWebhook(target)"><TechIcon name="trash" :size="12"/>删除</button></div>
            </article>
            <div v-if="!webhookTargets.length" class="panel empty"><b>NO WEBHOOK TARGET</b><p>尚未配置 Webhook</p></div>
          </section>
        </div>
      </section>

      <!-- ============ 人流报表 PEOPLE FLOW ============ -->
      <section v-else-if="active==='traffic'" class="page traffic-dashboard">
        <div class="metrics">
          <article><span>今日总人流</span><span class="metric-en">TOTAL FLOW TODAY</span><strong>{{trafficSummary.total_flow_today}}</strong><em>有效进入画面人次</em></article>
          <article><span>当前画面人数</span><span class="metric-en">ON-SCREEN NOW</span><strong>{{trafficSummary.current_people}}</strong><em>各摄像头有效人员轨迹</em></article>
          <article><span>识别方式</span><span class="metric-en">COUNTING MODE</span><strong>TRACK</strong><em>稳定人员轨迹自动计数</em></article>
          <article><span>统计摄像头</span><span class="metric-en">FLOW CAMERAS</span><strong>{{trafficSummary.flow_camera_count}}</strong><em>启用人员计数</em></article>
        </div>
        <section class="panel traffic-monthly-panel">
          <div class="section-head traffic-monthly-head">
            <div><h2>月度人流统计</h2><span class="head-en">MONTHLY PEOPLE FLOW · {{trafficMonth}}</span><p>按营业厅汇总每日进入画面人次，空白表示当天没有统计记录</p></div>
            <div class="traffic-monthly-actions">
              <label>选择月份<input v-model="trafficMonth" type="month" :max="currentTrafficMonth"></label>
              <button class="ghost" :disabled="trafficMonthlyLoading" @click="exportTrafficMonthly"><TechIcon name="database" :size="13"/>导出 Excel</button>
            </div>
          </div>
          <div v-if="trafficMonthlyLoading&&!trafficMonthlyReport.days.length" class="empty traffic-monthly-empty"><b>LOADING MONTHLY REPORT</b><p>正在加载月度人流数据</p></div>
          <div v-else-if="trafficMonthlyError" class="empty traffic-monthly-empty"><b>MONTHLY REPORT ERROR</b><p>{{trafficMonthlyError}}</p><button class="ghost" @click="loadTrafficMonthly()">重新加载</button></div>
          <div v-else-if="!trafficMonthlyReport.rows.length" class="empty traffic-monthly-empty"><b>NO FLOW CAMERA</b><p>当前没有启用人员计数的营业厅</p></div>
          <div v-else class="traffic-monthly-wrap" :class="{loading:trafficMonthlyLoading}">
            <table class="traffic-monthly-table">
              <thead><tr><th>营业厅</th><th v-for="day in trafficMonthlyReport.days" :key="day">{{Number(day.slice(-2))}}日</th><th>月合计</th></tr></thead>
              <tbody>
                <tr v-for="row in trafficMonthlyReport.rows" :key="row.directory_id??'unassigned'">
                  <th>{{row.hall_name}}</th><td v-for="(value,index) in row.values" :key="trafficMonthlyReport.days[index]">{{value??''}}</td><td>{{row.monthly_total}}</td>
                </tr>
              </tbody>
              <tfoot><tr><th>当月人流总计</th><td v-for="(value,index) in trafficMonthlyReport.daily_totals" :key="trafficMonthlyReport.days[index]">{{value??''}}</td><td>{{trafficMonthlyReport.grand_total}}</td></tr></tfoot>
            </table>
          </div>
        </section>
        <section class="panel traffic-trend-panel">
          <div class="section-head"><div><h2>今日画面人数趋势</h2><span class="head-en">ON-SCREEN TREND · {{trafficSummary.date}}</span></div><div class="trend-current"><small>当前人数</small><strong>{{trafficSummary.current_people}}</strong></div></div>
          <div v-if="chartPoints.length" class="traffic-chart">
            <svg viewBox="0 0 1000 260" preserveAspectRatio="none" role="img" aria-label="今日画面人数趋势折线图">
              <defs><linearGradient id="trafficArea" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#00e5ff" stop-opacity=".34"/><stop offset="1" stop-color="#008cff" stop-opacity="0"/></linearGradient></defs>
              <g class="chart-grid"><line v-for="y in [44,132,220]" :key="y" x1="44" x2="956" :y1="y" :y2="y"/></g>
              <text v-for="(tick,index) in chartTicks" :key="tick+index" x="34" :y="[48,136,224][index]" text-anchor="end" class="chart-axis-label">{{tick}}</text>
              <polygon :points="chartArea" fill="url(#trafficArea)"/>
              <polyline :points="chartPolyline" class="chart-line"/>
              <circle :cx="chartPoints.at(-1)?.x" :cy="chartPoints.at(-1)?.y" r="5" class="chart-current-dot"/>
            </svg>
            <div class="chart-time-axis"><span>{{trendTime(trafficSummary.store_trend[0]?.time)}}</span><span>{{trendTime(trafficSummary.store_trend[Math.floor(trafficSummary.store_trend.length/2)]?.time)}}</span><span>{{trendTime(trafficSummary.store_trend.at(-1)?.time)}}</span></div>
          </div>
          <div v-else class="empty trend-empty"><b>NO FLOW DATA TODAY</b><p>今日尚未产生人流统计数据</p></div>
        </section>

        <div class="ranking-grid">
          <section v-for="ranking in [{title:'当前画面人数排名',en:'ON-SCREEN RANKING',rows:trafficSummary.current_ranking,key:'current_count'},{title:'今日人流排名',en:'DAILY FLOW RANKING',rows:trafficSummary.flow_ranking,key:'entered_today'}]" :key="ranking.en" class="panel podium-panel">
            <div class="section-head"><div><h2>{{ranking.title}}</h2><span class="head-en">{{ranking.en}}</span></div></div>
            <div class="podium">
              <article v-for="(row,index) in podiumRows(ranking.rows)" :key="row?.camera_id||index" :class="[`place-${[2,1,3][index]}`,{clickable:!!row}]" :role="row?'button':undefined" :tabindex="row?0:undefined" @click="focusDashboardCamera(row)" @keydown.enter.prevent="focusDashboardCamera(row)" @keydown.space.prevent="focusDashboardCamera(row)">
                <div class="podium-person"><span>{{[2,1,3][index]}}</span><b>{{row?.camera_name||'暂无'}}</b><small>{{row?.camera_id||'—'}}</small><strong>{{row ? row[ranking.key as keyof TrafficCameraSummary] : 0}}</strong></div>
                <div class="podium-step">NO.{{[2,1,3][index]}}</div>
              </article>
            </div>
          </section>
        </div>

        <section class="traffic-camera-section">
          <div class="section-head"><div><h2>摄像头统计</h2><span class="head-en">CAMERA FLOW OVERVIEW</span><p>按今日总人流从高到低排列</p></div></div>
          <div v-if="trafficSummary.cameras.length" class="traffic-camera-grid">
            <article v-for="camera in trafficSummary.cameras" :key="camera.camera_id" class="panel traffic-camera-card clickable" role="button" tabindex="0" @click="focusDashboardCamera(camera)" @keydown.enter.prevent="focusDashboardCamera(camera)" @keydown.space.prevent="focusDashboardCamera(camera)">
              <header><div><h3>{{camera.camera_name}}</h3><code>{{camera.camera_id}}</code></div><span class="status-dot" :class="camera.online?'ready':'bad'">{{camera.online?'在线':'离线'}}</span></header>
              <div class="camera-flow-main"><div><small>今日总人流</small><strong>{{camera.entered_today}}</strong></div><div><small>当前人数</small><strong>{{camera.current_count}}</strong></div></div>
              <div class="camera-flow-detail"><span>今日进入画面人次 <b class="green">+{{camera.entered_today}}</b></span><span>Track 稳定去重</span></div>
              <footer>最后统计：{{camera.last_stat_at?formatTime(camera.last_stat_at):'暂无数据'}}</footer>
            </article>
          </div>
          <div v-else class="panel empty"><b>NO FLOW CAMERA</b><p>尚未配置人员计数摄像头</p></div>
        </section>
      </section>

      <!-- ============ 日志管理 ADMIN LOGS ============ -->
      <section v-else-if="active==='logs'" class="page logs-page">
        <section class="panel logs-shell">
          <div class="section-head"><div><h2>日志管理</h2><span class="head-en">ADMINISTRATOR LOGS</span><p>用户审计、摄像头分析与外部大模型调用记录</p></div><button class="ghost" @click="exportLogs"><TechIcon name="database" :size="13"/>导出当前筛选</button></div>
          <div class="log-tabs">
            <button :class="{active:logCategory==='audit'}" @click="switchLogCategory('audit')">用户操作日志</button>
            <button :class="{active:logCategory==='analyses'}" @click="switchLogCategory('analyses')">摄像头分析日志</button>
            <button :class="{active:logCategory==='model-calls'}" @click="switchLogCategory('model-calls')">大模型调用日志</button>
          </div>
          <div class="log-filters">
            <label>开始日期<input v-model="logFilters.start" type="date"></label><label>结束日期<input v-model="logFilters.end" type="date"></label>
            <template v-if="logCategory==='audit'"><label>用户<input v-model.trim="logFilters.username" placeholder="用户名"></label><label>操作<input v-model.trim="logFilters.action" placeholder="操作名称"></label></template>
            <template v-else><label>摄像头<input v-model.trim="logFilters.camera_id" placeholder="摄像头 ID"></label></template>
            <template v-if="logCategory==='analyses'"><label>检测模式<select v-model="logFilters.mode"><option value="">全部</option><option v-for="(_,mode) in modeInfo" :key="mode" :value="mode">{{modeName(mode)}}</option></select></label><label>分析状态<select v-model="logFilters.status"><option value="">全部</option><option value="confirmed">confirmed</option><option value="suspected">suspected</option><option value="uncertain">uncertain</option><option value="none">none</option></select></label></template>
            <template v-if="logCategory==='model-calls'"><label>模型<input v-model.trim="logFilters.model" placeholder="模型名称"></label><label>阶段<select v-model="logFilters.stage"><option value="">全部</option><option value="single">单模型检测</option></select></label></template>
            <label v-if="logCategory!=='analyses'">结果<select v-model="logFilters.outcome"><option value="">全部</option><option value="success">成功</option><option value="failure">失败</option><option value="error">错误</option></select></label>
            <div class="actions"><button class="primary" @click="loadLogs(1)">查询</button><button class="ghost" @click="resetLogFilters">重置</button></div>
          </div>
          <div class="log-summary"><span>共 {{logData.total}} 条</span><span>第 {{logData.page}} / {{logPages}} 页</span></div>
          <div class="log-table-wrap">
            <table class="log-table">
              <thead><tr v-if="logCategory==='audit'"><th>时间</th><th>用户</th><th>操作</th><th>目标</th><th>结果</th><th></th></tr><tr v-else-if="logCategory==='analyses'"><th>时间</th><th>摄像头</th><th>模式</th><th>状态</th><th>置信度</th><th>耗时</th><th></th></tr><tr v-else><th>时间</th><th>摄像头</th><th>阶段 / 模型</th><th>模式</th><th>结果</th><th>耗时</th><th></th></tr></thead>
              <tbody>
                <template v-for="row in logData.items" :key="row.id">
                  <tr v-if="logCategory==='audit'"><td>{{formatTime(row.created_at)}}</td><td><b>{{row.actor_display_name||row.actor_username||'未知用户'}}</b><small>{{row.actor_username}}</small></td><td>{{row.action}}<small>{{row.method}} {{row.path}}</small></td><td>{{row.target_type}} {{row.target_id}}</td><td><span class="log-result" :class="row.outcome">{{logResultText(row)}} · {{row.status_code}}</span></td><td><button class="link" @click="toggleLogDetail(row)">详情</button></td></tr>
                  <tr v-else-if="logCategory==='analyses'"><td>{{formatTime(row.created_at)}}</td><td><b>{{row.camera_name||row.camera_id||'已删除摄像头'}}</b><small>{{row.camera_id}}</small></td><td>{{modeName(row.mode)}}</td><td><span class="log-result" :class="row.status">{{row.status}}</span></td><td>{{Math.round(row.confidence*100)}}%</td><td>{{row.latency_ms}} ms</td><td><button class="link" @click="toggleLogDetail(row)">详情</button></td></tr>
                  <tr v-else><td>{{formatTime(row.created_at)}}</td><td><b>{{row.camera_name||row.camera_id||'已删除摄像头'}}</b><small>{{row.camera_id}}</small></td><td><b>{{row.stage==='single'?'单模型':row.stage==='economy'?'经济':'增强'}}</b><small>{{row.model}}</small></td><td>{{row.modes?.map((m:Mode)=>modeName(m)).join('、')}}</td><td><span class="log-result" :class="row.outcome">{{logResultText(row)}}<template v-if="row.http_status"> · {{row.http_status}}</template></span></td><td>{{row.latency_ms}} ms</td><td><button class="link" @click="toggleLogDetail(row)">详情</button></td></tr>
                  <tr v-if="expandedLogId===row.id" class="log-detail-row"><td :colspan="logCategory==='audit'?6:7"><pre>{{prettyLog(logDetails[logDetailKey(row.id)]||row)}}</pre></td></tr>
                </template>
                <tr v-if="!logData.items.length"><td :colspan="7" class="empty-cell">{{logLoading?'正在加载…':'暂无日志'}}</td></tr>
              </tbody>
            </table>
          </div>
          <div class="log-pagination"><button class="ghost" :disabled="logData.page<=1||logLoading" @click="loadLogs(logData.page-1)">上一页</button><span>{{logData.page}} / {{logPages}}</span><button class="ghost" :disabled="logData.page>=logPages||logLoading" @click="loadLogs(logData.page+1)">下一页</button></div>
        </section>
      </section>

      <!-- ============ 账号管理 ACCOUNT MANAGEMENT ============ -->
      <section v-else-if="active==='users'" class="page account-page">
        <section class="panel account-create">
          <div class="section-head"><div><h2>添加普通用户</h2><span class="head-en">CREATE USER</span><p>系统不开放注册，仅管理员可创建账号</p></div></div>
          <label>用户名<input v-model.trim="userForm.username" placeholder="operator01" autocomplete="off"><small class="field-hint">3-64 位字母、数字或 . _ -</small></label>
          <label>显示名称<input v-model.trim="userForm.display_name" placeholder="值班人员"></label>
          <label>初始密码<input v-model="userForm.password" type="password" placeholder="至少 8 位" autocomplete="new-password"></label>
          <button class="primary wide" @click="createUserAccount"><TechIcon name="plus" :size="13"/>创建普通用户</button>
        </section>
        <section class="panel account-list">
          <div class="section-head"><div><h2>账号列表</h2><span class="head-en">ACCOUNTS · {{users.length}}</span><p>停用、重置密码或删除账号会立即注销其会话</p></div></div>
          <article v-for="user in users" :key="user.id" :class="{disabled:!user.enabled}">
            <div class="account-avatar">{{user.display_name.slice(0,1)}}</div>
            <div><h3>{{user.display_name}} <span>{{user.role==='admin'?'管理员':'普通用户'}}</span></h3><code>{{user.username}}</code><small>创建于 {{formatTime(user.created_at)}}</small></div>
            <div class="actions"><button class="ghost" @click="editUserName(user)">改名称</button><button v-if="user.id!==currentUser.id" class="ghost" @click="resetUserPassword(user)">重置密码</button><button v-if="user.id!==currentUser.id" class="ghost" @click="toggleUser(user)">{{user.enabled?'停用':'启用'}}</button><button v-if="user.role!=='admin'" class="danger" @click="deleteUserAccount(user)">删除</button></div>
          </article>
        </section>
      </section>

      <!-- ============ 系统配置 SYSTEM SETTINGS ============ -->
      <section v-else-if="active==='settings'" class="page settings-grid">
        <section class="panel settings-card settings-card-display">
          <div class="section-head"><div><h2>人流数据展示</h2><span class="head-en">DATA DISPLAY SETTINGS</span><p>仅控制界面展示，不影响人员检测、统计任务和历史数据。</p></div></div>
          <div class="display-settings">
            <div class="inline-setting"><div><b>显示人流报表</b><small>控制左侧“人流报表”菜单及页面访问</small><span class="switch-label">{{displaySettings.show_traffic_report?'ENABLED':'DISABLED'}}</span></div><label class="switch"><input v-model="displaySettings.show_traffic_report" type="checkbox" @change="saveDisplaySettings"><span></span></label></div>
            <div class="inline-setting"><div><b>显示当前画面人数</b><small>控制监控总览中的当前人数模块</small><span class="switch-label">{{displaySettings.show_current_store_count?'ENABLED':'DISABLED'}}</span></div><label class="switch"><input v-model="displaySettings.show_current_store_count" type="checkbox" @change="saveDisplaySettings"><span></span></label></div>
          </div>
        </section>

        <section class="panel settings-card settings-card-retention">
          <div class="section-head"><div><h2>数据保留</h2><span class="head-en">DATA RETENTION</span><p>到期自动清理告警记录与证据图片</p></div><label class="switch"><input v-model="retentionSettings.auto_cleanup_enabled" type="checkbox"><span></span></label></div>
          <label>告警保留天数<input v-model.number="retentionSettings.alert_retention_days" type="number" min="1" max="365"><small class="field-hint">超过该天数的告警与证据会被自动清理；也可在告警中心手动清理</small></label>
          <label>日志保留天数<input v-model.number="retentionSettings.log_retention_days" type="number" min="1" max="365"><small class="field-hint">用户操作、摄像头分析和大模型调用日志统一保留，默认 30 天</small></label>
          <button class="primary" @click="saveRetention">保存保留策略</button>
        </section>

        <section class="panel settings-card settings-card-model">
          <div class="section-head"><div><h2>外部视觉大模型</h2><span class="head-en">VISION LANGUAGE MODEL</span><p>用于玩手机、人员吸烟检测与离岗告警终审</p></div><span class="status-dot" :class="{ready:modelSettings.api_key_configured||modelSettings.provider==='mock',warn:!modelSettings.api_key_configured&&modelSettings.provider!=='mock'}">{{modelSettings.api_key_configured?'● CONFIGURED':modelSettings.provider==='mock'?'● MOCK 模式':'● 未配置'}}</span></div>
          <label>提供商<select v-model="modelSettings.provider"><option value="openai_compatible">OpenAI 兼容接口</option><option value="mock">模拟模式</option></select></label>
          <label>Base URL<input v-model.trim="modelSettings.base_url" placeholder="http://192.168.1.100:8000/v1"><small class="field-hint">支持 HTTP / HTTPS：内网模型服务可使用 HTTP，公网服务建议 HTTPS。地址需包含 /v1，末尾斜杠可省略。HTTP 明文传输存在 API Key 泄露风险，建议仅用于受信内网。</small></label>
          <label>API Key<input v-model="modelSettings.api_key" type="password" placeholder="留空表示保持现有密钥"><small class="field-hint">首次配置必须填写；保存后留空表示继续使用现有密钥。密钥不会回显。</small></label>
          <label>检测模型<input v-model.trim="modelSettings.economy_model"></label>
          <div class="actions"><button class="primary" @click="saveModels">保存</button><button class="ghost" :disabled="testing" @click="testModels"><TechIcon name="zap" :size="13"/>{{testing?'TESTING…':'测试已保存配置'}}</button></div>
        </section>

        <section class="panel settings-card settings-card-detector">
          <div class="section-head"><div><h2>本地检测器</h2><span class="head-en">LOCAL DETECTORS</span><p>通用 YOLO 与烟火模型独立加载</p></div></div>
          <div class="detector-status">
            <article><span>通用 YOLO</span><b>{{detectorSettings.runtime?.general?.status||'unknown'}}</b><small>{{detectorSettings.runtime?.general?.latency_ms||0}} ms</small></article>
            <article :class="{dangerbox:detectorSettings.runtime?.fire_smoke?.status!=='ready'}"><span>烟火模型</span><b>{{detectorSettings.runtime?.fire_smoke?.status||'unknown'}}</b><small>{{detectorSettings.runtime?.fire_smoke?.detail||detectorSettings.runtime?.fire_smoke?.latency_ms+' ms'}}</small></article>
          </div>
          <div class="form-row"><label>通用模型<input v-model="detectorSettings.general_model"></label><label>运行设备<input v-model="detectorSettings.general_device"></label></div>
          <div class="form-row"><label>烟火权重路径<input v-model="detectorSettings.fire_smoke_model"></label><label>运行设备<input v-model="detectorSettings.fire_smoke_device"></label></div>
          <label>烟火权重 SHA256<input v-model="detectorSettings.model_sha256" readonly></label>
          <label>许可证<input v-model="detectorSettings.license_name" readonly></label>
          <button class="primary" @click="saveDetectors">保存并重载</button>
        </section>

        <section class="panel settings-card settings-card-capabilities">
          <div class="section-head"><div><h2>能力注册表</h2><span class="head-en">CAPABILITY REGISTRY</span><p>计划能力只展示，不会进入检测任务</p></div></div>
          <div class="capability-list"><span v-for="item in capabilities" :key="item.id"><b>{{item.name}}</b><em :class="item.availability">{{item.availability}}</em></span></div>
        </section>
      </section>
    </main>

    <!-- ============ 实时预览 LIVE PREVIEW ============ -->
    <div v-if="preview" class="modal" role="dialog" aria-modal="true" @click.self="closePreview">
      <section class="preview-modal">
        <header class="modal-head">
          <div><h2>{{preview.name}}</h2><span class="head-en">LIVE PREVIEW</span><p>按需低帧率预览 · 关闭后立即释放 FFmpeg</p></div>
          <button type="button" aria-label="关闭实时预览" @click.stop="closePreview"><TechIcon name="close" :size="16"/></button>
        </header>
        <div class="live">
          <div v-if="previewLoading" class="preview-state"><b><TechIcon name="refresh" :size="17"/>CONNECTING</b><p>正在临时启动该路 FFmpeg，请稍候…</p></div>
          <div v-else-if="previewError" class="preview-state error"><b><TechIcon name="alert" :size="17"/>无法打开实时预览</b><p>{{previewError}}</p><button @click="openPreview(preview)">重新连接</button></div>
          <img v-else-if="previewStreamUrl" :src="previewStreamUrl" :alt="`${preview.name} 实时视频`">
          <span v-if="previewStreamUrl" class="live-badge">LIVE</span>
          <span v-if="previewStreamUrl" class="person-legend">YOLO 实时检测 · 人员框 / Track ID / 置信度</span>
        </div>
      </section>
    </div>

    <!-- ============ 摄像头编辑 EDITOR ============ -->
    <div v-if="editor" class="modal" @click.self="editor=null">
      <section class="editor-modal">
        <header class="modal-head">
          <div><h2>{{editor.name}} · 编辑摄像头</h2><span class="head-en">CAMERA CONFIGURATION</span><p>基础信息、检测能力、策略与区域可一次保存</p></div>
          <button type="button" aria-label="关闭编辑" @click.stop="editor=null"><TechIcon name="close" :size="16"/></button>
        </header>
        <div class="editor-body">
          <div class="editor-left">
            <CameraBasicFields :model="editForm" editing />
            <label>所属目录<select v-model="editForm.directory_id"><option :value="null">未分组</option><option v-for="directory in cameraDirectories" :key="directory.id" :value="directory.id">{{directory.name}}</option></select></label>
            <div class="scene-picker compact-scenes">
              <button v-for="scene in (['workstation','customer_area','security_area','custom'] as SceneType[])" :key="scene" :class="{selected:editForm.scene_type===scene}" @click="editorTemplate(scene)"><TechIcon :name="sceneInfo[scene].icon" :size="18"/><b>{{sceneInfo[scene].name}}</b></button>
            </div>
            <div class="mode-picker compact">
              <button v-for="(info,mode) in modeInfo" :key="mode" :class="{selected:editForm.modes.includes(mode)}" @click="toggleMode(editForm,mode)"><TechIcon :name="info.icon" :size="16"/><span><b>{{info.name}}</b></span></button>
            </div>
            <div class="draw-actions">
              <button :class="{active:drawLayer==='post_roi'}" class="ghost" @click="drawLayer='post_roi'">岗位区域</button>
              <button :class="{active:drawLayer==='flow_roi'}" class="ghost" @click="drawLayer='flow_roi'">人流区域</button>
              <button :class="{active:drawLayer==='intrusion_zone'}" class="ghost" @click="drawLayer='intrusion_zone'">禁区</button>
              <button v-if="drawLayer==='flow_roi'" class="ghost" @click="fullScreenFlow">恢复全屏</button>
              <button v-if="drawLayer==='intrusion_zone'" class="ghost" @click="fullScreenIntrusion">恢复全屏</button>
              <button class="danger" @click="clearLayer"><TechIcon name="trash" :size="12"/>清空当前图层</button>
            </div>
            <div ref="canvasRef" class="geometry-stage" @click="canvasClick">
              <img :src="snapshotUrl(editor)">
              <svg viewBox="0 0 100 100" preserveAspectRatio="none">
                <polygon v-if="pointsFor('post_roi').length>=3" :points="polygon(pointsFor('post_roi'))" class="post-zone"/>
                <polygon v-if="pointsFor('flow_roi').length>=3" :points="polygon(pointsFor('flow_roi'))" class="flow-zone"/>
                <polygon v-if="pointsFor('intrusion_zone').length>=3" :points="polygon(pointsFor('intrusion_zone'))" class="intrusion-zone"/>
                <g v-for="(p,index) in pointsFor(drawLayer)" :key="index"><circle :cx="p[0]*100" :cy="p[1]*100" r="1.1"/><text :x="p[0]*100+1.5" :y="p[1]*100-1">{{index+1}}</text></g>
              </svg>
            </div>
            <p class="hint">岗位区域、人流 ROI 和禁区至少需要 3 个点。人流 ROI 与新建禁区默认为全屏。当前图层已有 {{pointsFor(drawLayer).length}} 个点。</p>
          </div>
          <aside class="editor-right">
            <label>抽帧频率<select v-model.number="editForm.frame_interval_seconds"><option v-for="seconds in frameIntervalOptions" :key="seconds" :value="seconds">每 {{seconds}} 秒抓取一帧</option></select></label>
            <label v-if="editForm.modes.includes('intrusion')">禁区名称<input v-model="editForm.zone_name"></label>
            <template v-if="editForm.modes.includes('intrusion')"><div class="field-title">区域入侵时段 <small>每天，支持跨午夜</small></div><div class="form-row"><label>开始时间<input v-model="intrusionShift.start" type="time" @change="syncIntrusionShift"></label><label>结束时间<input v-model="intrusionShift.end" type="time" @change="syncIntrusionShift"></label></div></template>
            <div class="field-title">普通模式排班</div>
            <div class="schedule-summary">
              <span><b>{{scheduleSummary}}</b><small>时区：{{editForm.schedule?.timezone||'Asia/Shanghai'}}</small></span>
              <button type="button" class="ghost" @click="openCameraSchedule"><TechIcon name="clock" :size="12"/>配置排班</button>
            </div>
            <div v-if="editForm.modes.includes('off_duty')" class="config-note"><b>离岗大模型终审</b><p>同时启用玩手机且其检测间隔小于离岗判定时间时，将在行为检测中复核离岗；其他情况使用独立大模型终审。两种路径都必须先达到本地持续无人阈值。</p></div>
            <label v-if="usesPersonDetection">人员检测置信度<input v-model.number="editForm.options.person_confidence" type="number" min="0" max="1" step="0.05"><small class="field-hint">低于此置信度的人员不参与在岗、离岗、人流和入侵判定，默认 0.30。</small></label>
            <div v-if="editForm.modes.includes('people_flow')" class="config-note"><b>人流 ROI 有向穿越</b><p>同一 Track ID 从区域外稳定进入 ROI 后累计一次；首次出现在 ROI 内不会计入人流。</p></div>
            <label v-if="editForm.modes.includes('people_flow')">稳定确认帧数<input v-model.number="editForm.options.flow_min_stable_frames" type="number" min="2" max="30"></label>
            <label v-if="editForm.modes.includes('people_flow')" class="inline-setting"><span>人流 Debug 标注</span><span class="switch"><input v-model="editForm.options.flow_debug" type="checkbox"><span></span></span></label>
            <div v-if="editForm.modes.includes('phone_use') || editForm.modes.includes('smoking')" class="config-note"><b>行为联合检测</b><p>按配置频率抽取当前单帧，玩手机需连续每次确认才会告警；任意非确认或失败均重新计时。</p></div>
            <div v-if="editForm.modes.includes('phone_use') || editForm.modes.includes('smoking')" class="form-row"><label>大模型检测间隔（分钟）<input :value="optionMinutes('behavior_interval_seconds')" type="number" min="1" max="60" @input="setOptionMinutes('behavior_interval_seconds',$event)"></label><label v-if="editForm.modes.includes('phone_use')">玩手机判定时间（分钟）<input :value="optionMinutes('phone_use_seconds')" type="number" min="1" max="1440" @input="setOptionMinutes('phone_use_seconds',$event)"></label></div>
            <div class="form-row"><label>火焰阈值<input v-model.number="editForm.options.fire_confidence" type="number" min="0" max="1" step=".05"></label><label>烟雾阈值<input v-model.number="editForm.options.smoke_confidence" type="number" min="0" max="1" step=".05"></label></div>
            <label>入侵置信度<input v-model.number="editForm.options.intrusion_confidence" type="number" min="0" max="1" step=".05"></label>
            <div class="config-note"><b>安全模式</b><p>烟火和黑屏始终运行；区域入侵仅在上方独立时段内运行。</p></div>
            <button class="primary wide" @click="saveEditor">保存策略</button>
          </aside>
        </div>
      </section>
    </div>

    <!-- ============ 单路摄像头排班 CAMERA SCHEDULE ============ -->
    <div v-if="cameraScheduleModal" class="modal schedule-modal-layer" role="dialog" aria-modal="true" @click.self="cameraScheduleModal=false">
      <section class="confirm-modal off-duty-modal">
        <header class="modal-head"><div><h2>{{editForm.name}} · 普通模式排班</h2><span class="head-en">CAMERA SCHEDULE</span><p>配置该摄像头的生效星期、时段和离岗判定时间</p></div><button type="button" aria-label="关闭" @click="cameraScheduleModal=false"><TechIcon name="close" :size="16"/></button></header>
        <div class="confirm-body">
          <label>生效星期</label>
          <div class="weekday"><button v-for="d in weekdays" :key="d[0]" type="button" :class="{selected:cameraScheduleDraft.days.includes(d[0])}" @click="toggleScheduleDay(d[0])">{{d[1]}}</button></div>
          <div v-for="(shift,index) in cameraScheduleDraft.shifts" :key="index" class="form-row schedule-row">
            <label>开始时间<input v-model="shift.start" type="time"></label>
            <label>结束时间<input v-model="shift.end" type="time"></label>
            <label v-if="editForm.modes.includes('off_duty')">离岗判定（分钟）<input :value="Math.round(Number(shift.off_duty_seconds||editForm.options.off_duty_seconds)/60)" type="number" min="1" max="1440" @input="setShiftMinutes(shift,$event)"></label>
            <button type="button" class="ghost" @click="removeScheduleShift(index)">删除</button>
          </div>
          <button type="button" class="ghost wide" @click="addScheduleShift">添加排班时段</button>
          <p>此排班由当前摄像头的离岗、在岗、玩手机和吸烟等普通模式共用；确认后仍需在摄像头编辑页保存策略。</p>
        </div>
        <footer class="confirm-foot"><button class="ghost" @click="cameraScheduleModal=false">取消</button><button class="primary" @click="applyCameraSchedule">应用到当前摄像头</button></footer>
      </section>
    </div>

    <!-- ============ 批量离岗排班 BATCH OFF-DUTY SCHEDULE ============ -->
    <div v-if="offDutyScheduleModal" class="modal" role="dialog" aria-modal="true" @click.self="offDutyScheduleModal=false">
      <section class="confirm-modal off-duty-modal">
        <header class="modal-head"><div><h2>统一设置离岗时间</h2><span class="head-en">BATCH OFF-DUTY SCHEDULE</span><p>将覆盖 {{cameras.filter(camera=>camera.modes.includes('off_duty')).length}} 个离岗检测摄像头的普通模式排班</p></div><button type="button" aria-label="关闭" @click="offDutyScheduleModal=false"><TechIcon name="close" :size="16"/></button></header>
        <div class="confirm-body">
          <label>生效星期</label>
          <div class="weekday"><button v-for="d in weekdays" :key="d[0]" type="button" :class="{selected:offDutyScheduleForm.days.includes(d[0])}" @click="offDutyScheduleForm.days=offDutyScheduleForm.days.includes(d[0])?offDutyScheduleForm.days.filter(day=>day!==d[0]):[...offDutyScheduleForm.days,d[0]]">{{d[1]}}</button></div>
          <div v-for="(shift,index) in offDutyScheduleForm.shifts" :key="index" class="form-row schedule-row">
            <label>开始时间<input v-model="shift.start" type="time"></label>
            <label>结束时间<input v-model="shift.end" type="time"></label>
            <label>离岗判定（分钟）<input :value="Math.round(shift.off_duty_seconds/60)" type="number" min="1" max="1440" @input="setBatchShiftMinutes(shift,$event)"></label>
            <button type="button" class="ghost" @click="removeBatchOffDutyShift(index)">删除</button>
          </div>
          <button type="button" class="ghost wide" @click="addBatchOffDutyShift">添加离岗时段</button>
          <p>离岗检测只在所选时段内运行；同一摄像头的在岗、玩手机等普通模式也会共用此排班。</p>
        </div>
        <footer class="confirm-foot"><button class="ghost" :disabled="offDutyScheduleSaving" @click="offDutyScheduleModal=false">取消</button><button class="primary" :disabled="offDutyScheduleSaving" @click="saveOffDutySchedules">{{offDutyScheduleSaving?'保存中…':'统一应用'}}</button></footer>
      </section>
    </div>

    <!-- ============ 手动发送 Webhook ============ -->

    <div v-if="sendModal" class="modal" role="dialog" aria-modal="true" @click.self="sendModal=false">
      <section class="confirm-modal">
        <header class="modal-head"><div><h2>发送告警到企业微信</h2><span class="head-en">MANUAL DELIVERY</span><p>已选择 {{selectedAlertIds.length}} 条告警</p></div><button @click="sendModal=false"><TechIcon name="close" :size="16"/></button></header>
        <div class="confirm-body target-picker">
          <label v-for="target in enabledWebhookTargets" :key="target.id"><input v-model="selectedTargetIds" type="checkbox" :value="target.id"><span><b>{{target.name}}</b><small v-if="target.url">{{target.url}}</small></span></label>
          <p v-if="!enabledWebhookTargets.length">没有已启用且配置完整的企业微信机器人。</p>
        </div>
        <footer class="confirm-foot"><button class="ghost" @click="sendModal=false">取消</button><button class="primary" :disabled="!selectedTargetIds.length" @click="manualSend">确认发送</button></footer>
      </section>
    </div>

    <!-- ============ 修改本人密码 ============ -->
    <div v-if="passwordModal" class="modal" role="dialog" aria-modal="true" @click.self="passwordModal=false">
      <section class="confirm-modal">
        <header class="modal-head"><div><h2>修改密码</h2><span class="head-en">CHANGE PASSWORD</span><p>修改成功后需要重新登入</p></div><button @click="passwordModal=false"><TechIcon name="close" :size="16"/></button></header>
        <div class="confirm-body"><label>原密码<input v-model="passwordForm.old_password" type="password" autocomplete="current-password"></label><label>新密码<input v-model="passwordForm.new_password" type="password" autocomplete="new-password"></label><label>确认新密码<input v-model="passwordForm.confirm" type="password" autocomplete="new-password"></label></div>
        <footer class="confirm-foot"><button class="ghost" @click="passwordModal=false">取消</button><button class="primary" @click="changePassword">保存新密码</button></footer>
      </section>
    </div>

    <!-- ============ 统一确认对话框 CONFIRM ============ -->
    <div v-if="confirmState.show" class="modal" role="dialog" aria-modal="true" @click.self="resolveConfirm(null)">
      <section class="confirm-modal">
        <header class="modal-head"><div><h2>{{confirmState.title}}</h2></div><button type="button" aria-label="关闭" @click.stop="resolveConfirm(null)"><TechIcon name="close" :size="16"/></button></header>
        <div class="confirm-body">
          <p>{{confirmState.message}}</p>
          <label v-if="confirmState.input">{{confirmState.inputLabel}}<input v-model="confirmState.inputValue" :type="confirmState.inputType" :min="confirmState.inputType==='number'?1:undefined" :max="confirmState.inputType==='number'?365:undefined" @keyup.enter="resolveConfirm(confirmState.input?confirmState.inputValue:true)"></label>
        </div>
        <footer class="confirm-foot">
          <button class="ghost" @click="resolveConfirm(null)">取消</button>
          <button :class="confirmState.danger?'danger':'primary'" @click="resolveConfirm(confirmState.input?confirmState.inputValue:true)">{{confirmState.confirmText}}</button>
        </footer>
      </section>
    </div>

    <BatchCameraModal v-if="batchModal" :existing-ids="cameras.map(camera=>camera.id)" @close="batchModal=false" @created="batchCreated" @failed="notify($event,'error')" />

    <Transition name="toast">
      <div v-if="toast.show" class="toast" :class="toast.kind"><span><TechIcon :name="toast.kind==='ok'?'check':'alert'" :size="13"/></span>{{toast.message}}</div>
    </Transition>
  </div>
</template>
