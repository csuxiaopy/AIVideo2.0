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
  frame_interval_seconds: 5 | 10 | 20 | 30 | 60 | 120
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
