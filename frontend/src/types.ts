export type ToolState = 'IDLE' | 'PROCESSING' | 'PM' | 'DOWN' | 'OFFLINE'
export type HealthStatus = 'OK' | 'WATCH' | 'PM_REQUIRED'

export interface Sensor {
  svid: number
  name: string
  units: string
  value: number | null
}

export interface Health {
  indicator: string | null
  units: string | null
  nominal: number | null
  spec_limit: number | null
  score: number
  ewma: number | null
  rul_wafers: number | null
  status: HealthStatus
  wafers_since_maint: number
  ucl: number | null
  lcl: number | null
  center: number | null
  violations: string[]
  history: number[]
}

export type WorkOrderStatus = 'OPEN' | 'SENT' | 'IN_PROGRESS' | 'DONE' | 'CANCELLED'

export interface WorkOrder {
  id: number
  tool: string
  kind: 'PM' | 'REPAIR'
  trigger: 'PREDICTIVE' | 'REACTIVE' | 'MANUAL'
  status: WorkOrderStatus
  reason: string | null
  requested_at: string | null
  started_at: string | null
  finished_at: string | null
}

export interface Equipment {
  id: string
  tool_type: string | null
  mdln: string | null
  softrev: string | null
  address: string
  connected: boolean
  state: ToolState
  control_state: 'ONLINE_REMOTE' | 'ONLINE_LOCAL'
  busy: boolean
  maint_active: boolean
  work_order: WorkOrder | null
  lot_id: string | null
  slot: number
  lot_wafers: number
  last_error: string | null
  sensors: Sensor[]
  health: Health | null
}

export type LotStatus = 'QUEUED' | 'DISPATCHED' | 'RUNNING' | 'COMPLETED' | 'HOLD'

export interface Lot {
  id: string
  product: string
  tool_type: string
  recipe: string
  wafers: number
  priority: number
  status: LotStatus
  equipment_id: string | null
  wafers_done: number
  wafers_scrapped: number
  hold_reason: string | null
  created_at: string | null
  started_at: string | null
  finished_at: string | null
}

export interface Alarm {
  id: number
  tool: string
  alid: number
  text: string
  severity: number
  set_at: string
  cleared_at: string | null
}

export interface Strategy {
  mode: 'predictive' | 'reactive'
  wafers: number
  scrapped: number
  scrap_rate: number
  pm_count: number
  repairs: number
  planned_down_s: number
  unplanned_down_s: number
}

export interface Kpi {
  availability: Record<string, { availability: number; states: Record<string, number> }>
  strategies: Strategy[]
}

export interface SecsEntry {
  ts: string
  tool: string
  dir: 'in' | 'out'
  sf: string
  sml: string
}

export interface Modes {
  running: boolean
  predictive: boolean
  auto_dispatch: boolean
  auto_work_orders: boolean
  auto_lots: boolean
}

export interface TracePoint {
  ts: string
  values: number[]
}

export interface WaferEvent {
  tool: string
  lot: string
  slot: number
  result: 'OK' | 'SCRAP'
  value: number | null
  violations: string[]
}

export type Block =
  | 'simulator'
  | 'gateway'
  | 'host'
  | 'analytics'
  | 'dispatcher'
  | 'mes'
  | 'database'
  | 'api'
  | 'crew'
  | 'console'

export interface Activity {
  id: number
  ts: string
  block: Block
  tool: string | null
  kind: string
  text: string
  data: Record<string, unknown>
}

/** Ground truth from the simulator, which the host itself never sees. */
export interface SimStatus {
  id: string
  tool_type: string
  port: number
  state: string
  wear: number
  wear_multiplier: number
  connected: boolean
  communicating: boolean
  spooled: number
  blocked_for: number
  lot: string | null
}

export interface Frame {
  ts: string
  hex: string
  sml: string
  sensors: { svid: number; name: string; units: string; value: number }[]
}

export interface SystemInfo {
  tables: Record<string, number>
  ws_clients: number
  equipment_online: number
  secs_messages_logged: number
}
