import type { WorkOrder, WorkOrderStatus } from '../types'

export const WO_STATUS_TEXT: Record<WorkOrderStatus, string> = {
  OPEN: 'not yet sent to the technicians',
  SENT: 'technician on the way',
  IN_PROGRESS: 'technician working at the tool',
  DONE: 'done',
  CANCELLED: 'cancelled',
}

export const KIND_TEXT = { PM: 'PM', REPAIR: 'Repair' } as const

export const woSummary = (wo: WorkOrder) => `${KIND_TEXT[wo.kind]} work order #${wo.id}: ${WO_STATUS_TEXT[wo.status]}`
