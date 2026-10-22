import { timeOf } from '../lib/health'
import { KIND_TEXT, WO_STATUS_TEXT } from '../lib/workOrders'
import type { WorkOrder } from '../types'

const TRIGGER_TEXT = { PREDICTIVE: 'predicted', REACTIVE: 'after failure', MANUAL: 'requested' } as const

/** Maintenance is done by technicians at the tool; the host only raises and tracks work orders. */
export function WorkOrderList({ orders }: { orders: WorkOrder[] }) {
  const active = orders.filter((w) => w.status !== 'DONE' && w.status !== 'CANCELLED')
  const recent = orders.filter((w) => w.status === 'DONE' || w.status === 'CANCELLED').slice(0, 6)
  return (
    <section className="workorders" aria-labelledby="wo-title">
      <h2 id="wo-title">
        Maintenance work orders <span className="count">{active.length} open</span>
      </h2>
      {orders.length === 0 && <p className="muted">No maintenance requested yet.</p>}
      <ul>
        {[...active, ...recent].map((w) => (
          <li key={w.id} className={`wo wo--${w.status.toLowerCase()} wo--${w.kind.toLowerCase()}`}>
            <span className="wo__tool">{w.tool}</span>
            <span>
              <strong>
                #{w.id} {KIND_TEXT[w.kind]}
              </strong>{' '}
              <span className="muted">{TRIGGER_TEXT[w.trigger]}</span>
              <span className="wo__status">{WO_STATUS_TEXT[w.status]}</span>
              {w.reason && <span className="wo__reason">{w.reason}</span>}
            </span>
            <span className="wo__time num muted">
              {timeOf(w.requested_at)}
              {w.finished_at && <> to {timeOf(w.finished_at)}</>}
            </span>
          </li>
        ))}
      </ul>
    </section>
  )
}
