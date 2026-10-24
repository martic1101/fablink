import type {
  Activity,
  Alarm,
  Equipment,
  Frame,
  Kpi,
  Lot,
  Modes,
  SecsEntry,
  SimStatus,
  SystemInfo,
  TracePoint,
  WorkOrder,
} from './types'

import { transport } from './connection'

export { ApiError } from './transport'

const request = <T,>(path: string, init?: RequestInit) => transport.request<T>(path, init)

export const api = {
  equipment: () => request<Equipment[]>('/equipment'),
  equipmentDetail: (id: string) => request<Equipment & { trace: TracePoint[] }>(`/equipment/${id}`),
  abortLot: (id: string) =>
    request<{ hcack: string }>(`/equipment/${id}/commands`, {
      method: 'POST',
      body: JSON.stringify({ command: 'ABORT' }),
    }),
  requestWorkOrder: (id: string, kind: 'PM' | 'REPAIR') =>
    request<WorkOrder>(`/equipment/${id}/work-orders`, { method: 'POST', body: JSON.stringify({ kind }) }),
  workOrders: () => request<WorkOrder[]>('/work-orders?limit=20'),
  sendWorkOrder: (woId: number) => request<WorkOrder>(`/work-orders/${woId}/send`, { method: 'POST' }),
  lots: (limit = 80) => request<Lot[]>(`/lots?limit=${limit}`),
  createLot: (body: { tool_type: string; wafers: number; priority: number }) =>
    request<Lot>('/lots', { method: 'POST', body: JSON.stringify(body) }),
  releaseLot: (id: string) => request<Lot>(`/lots/${encodeURIComponent(id)}/release`, { method: 'POST' }),
  alarms: () => request<Alarm[]>('/alarms?limit=40'),
  secsLog: () => request<SecsEntry[]>('/secs-log?limit=120'),
  kpi: () => request<Kpi>('/kpi'),
  shutdown: () => request<{ ok: boolean }>('/system/shutdown', { method: 'POST' }),
  setModes: (m: Partial<Modes>) => request<Modes>('/modes', { method: 'PUT', body: JSON.stringify(m) }),
  activity: () => request<Activity[]>('/activity?limit=300'),
  frame: (id: string) => request<Frame>(`/equipment/${id}/frame`),
  system: () => request<SystemInfo>('/system'),
  simTools: () => request<SimStatus[]>('/sim/tools'),
  simAction: (id: string, action: 'wear' | 'degrade' | 'disconnect' | 'lose_reply', body: Record<string, number>) =>
    request<SimStatus>(`/sim/tools/${id}/${action}`, { method: 'POST', body: JSON.stringify(body) }),
}
