import type { Equipment } from '../types'
import { FoupStrip } from './FoupStrip'

const STATE_LABEL: Record<string, string> = {
  PROCESSING: 'Processing',
  IDLE: 'Idle',
  PM: 'Preventive maintenance',
  DOWN: 'Down',
  OFFLINE: 'Offline',
}

const HEALTH_LABEL: Record<string, string> = {
  OK: 'Healthy',
  WATCH: 'Watch',
  PM_REQUIRED: 'PM recommended',
}

function stateLabel(t: Equipment): string {
  const wo = t.work_order
  if (t.state === 'DOWN') {
    if (!wo) return 'Down'
    if (wo.status === 'IN_PROGRESS') return 'Down, repair in progress'
    if (wo.status === 'SENT') return 'Down, technician on the way'
    return 'Down, repair not yet requested from technicians'
  }
  if (t.state === 'PM') return 'Preventive maintenance, local control'
  if (wo && (t.state === 'IDLE' || t.state === 'PROCESSING')) return `${STATE_LABEL[t.state]}, held for PM`
  return STATE_LABEL[t.state]
}

interface Props {
  tool: Equipment
  slots?: Record<number, 'OK' | 'SCRAP'>
  availability?: number
  selected: boolean
  onSelect: () => void
}

export function ToolPanel({ tool, slots, availability, selected, onSelect }: Props) {
  const h = tool.health

  return (
    <button
      className={`tool tool--${tool.state.toLowerCase()}`}
      aria-pressed={selected}
      onClick={onSelect}
      type="button"
    >
      <span className="tool__band" />
      <span className="tool__head">
        <span className="tool__id">{tool.id}</span>
        <span className="tool__model">{tool.mdln ?? tool.address}</span>
      </span>
      <span className="tool__state">{stateLabel(tool)}</span>

      <span className="tool__lot">
        {tool.lot_id ? (
          <>
            <span>{tool.lot_id}</span>
            <span className="muted">
              wafer {tool.slot || 1} of {tool.lot_wafers}
            </span>
          </>
        ) : (
          <span className="muted">{tool.connected ? 'No lot loaded' : (tool.last_error ?? 'Connecting')}</span>
        )}
      </span>

      <FoupStrip wafers={tool.lot_id ? tool.lot_wafers : 0} current={tool.slot} results={slots} />

      {h && (
        <span className={`tool__health health--${h.status.toLowerCase()}`}>
          <span className="tool__meter" aria-hidden>
            <span style={{ width: `${h.score}%` }} />
          </span>
          <span className="tool__health-row">
            <span>{HEALTH_LABEL[h.status]}</span>
            <span className="num">
              {h.rul_wafers === null ? 'no wear trend' : `${Math.floor(h.rul_wafers)} wafers left`}
            </span>
          </span>
        </span>
      )}
      {availability !== undefined && (
        <span className="tool__avail muted">Available {availability}% of the last hour</span>
      )}
    </button>
  )
}
