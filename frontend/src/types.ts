export type Mode =
  | 'black_screen'
  | 'off_duty'
  | 'on_duty'
  | 'people_flow'
  | 'phone_use'
  | 'smoking'
  | 'fire_smoke'
  | 'intrusion'

export type SceneType = 'workstation' | 'customer_area' | 'security_area' | 'custom'
export type Point = [number, number]
export type DrawLayer = 'post_roi' | 'flow_line' | 'intrusion_zone'

export interface Camera {
  id: string
  name: string
  source: string
  enabled: boolean
  online: boolean
  camera_online: boolean
  frame_interval_seconds: 1 | 5 | 10 | 20 | 30 | 60 | 120
  last_frame_at?: string
  last_analysis_at?: string
  preview_active: boolean
  preview_started_at?: string
  last_error?: string
  scene_type: SceneType
  modes: Mode[]
  geometry: any
  schedule: any
  options: any
}

export interface SceneTemplate {
  scene_type: SceneType
  name: string
  description: string
  modes: Mode[]
  schedule: any
  options: any
  required_geometry: string[]
}

export interface TrafficTrendPoint {
  time: string
  current_people: number
}

export interface TrafficCameraSummary {
  camera_id: string
  camera_name: string
  online: boolean
  current_count: number
  entered_today: number
  exited_today: number
  last_stat_at?: string
}

export interface TrafficSummary {
  date: string
  timezone: string
  total_flow_today: number
  current_people: number
  entered_today: number
  exited_today: number
  flow_camera_count: number
  store_trend: TrafficTrendPoint[]
  cameras: TrafficCameraSummary[]
  current_ranking: TrafficCameraSummary[]
  flow_ranking: TrafficCameraSummary[]
}
