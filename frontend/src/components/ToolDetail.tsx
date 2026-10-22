import { useState } from 'react'
import { api } from '../api'
import { woSummary } from '../lib/workOrders'
import type { Equipment, TracePoint } from '../types'
import { HealthChart } from './HealthChart'
import { Sparkline } from './Sparkline'

type Action = 'pm' | 'send' | 'abort'

export function ToolDetail({ tool, trace }: { tool: Equipment; trace: TracePoint[] }) {
  const [pending, setPending] = useState<Action | null>(null)
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null)
  const h = tool.health
  const wo = tool.work_order

  const run = async (action: Action, call: () => Promise<unknown>, done: string) => {
    setPending(action)
    setMessage(null)
    try {
      await call()
      setMessage({ ok: true, text: done })
    } catch (e) {
      setMessage({ ok: false, text: (e as Error).message })
    } finally {
      setPending(null)
    }
  }

  const can = {
    pm: !wo && tool.state !== 'DOWN',
    send: wo?.status === 'OPEN',
    abort: tool.connected && tool.state === 'PROCESSING' && tool.control_state === 'ONLINE_REMOTE',
  }

  return (
    <section className="detail" aria-labelledby="detail-title">
      <header className="detail__head">
        <div>
          <h2 id="detail-title">
            {tool.id} <span className="muted">{h?.indicator?.replace(/_/g, ' ').toLowerCase()}</span>
          </h2>
          {h && (
            <p className="detail__summary">
              {h.rul_wafers === null
                ? `No wear trend yet across ${h.wafers_since_maint} wafers since maintenance.`
                : h.rul_wafers === 0
                  ? 'Past the spec limit. Wafers processed now are at risk.'
                  : `At the current trend this tool reaches its spec limit in about ${Math.floor(h.rul_wafers)} wafers.`}
              {h.violations.length > 0 && <> Out of statistical control: {h.violations.join('; ')}.</>}
            </p>
          )}
          {wo && (
            <p className="detail__wo">
              {woSummary(wo)}. No new lots are sent to {tool.id} until it is closed.
            </p>
          )}
        </div>
        <div className="detail__actions">
          <button
            type="button"
            disabled={!can.pm || pending !== null}
            onClick={() => run('pm', () => api.requestWorkOrder(tool.id, 'PM'), 'PM work order raised; tool held')}
          >
            Request PM
          </button>
          <button
            type="button"
            disabled={!can.send || pending !== null}
            onClick={() => run('send', () => api.sendWorkOrder(wo!.id), 'Sent to the technicians')}
          >
            Send to technicians
          </button>
          <button
            type="button"
            className="danger"
            disabled={!can.abort || pending !== null}
            onClick={() => run('abort', () => api.abortLot(tool.id), 'Lot aborted')}
          >
            Abort lot
          </button>
        </div>
      </header>
      {message && (
        <p className={message.ok ? 'notice' : 'notice notice--error'} role="status">
          {message.text}
        </p>
      )}

      {h ? <HealthChart health={h} /> : <p className="muted">Waiting for the tool to report its status variables.</p>}

      <ul className="sensors">
        {tool.sensors.map((s, i) => (
          <li key={s.svid}>
            <span className="sensors__name">{s.name.replace(/_/g, ' ').toLowerCase()}</span>
            <span className="sensors__value num">
              {s.value === null ? '–' : s.value.toFixed(s.value >= 100 ? 0 : 2)} <small>{s.units}</small>
            </span>
            <Sparkline values={trace.map((p) => p.values[i])} highlight={s.name === h?.indicator} />
          </li>
        ))}
      </ul>
    </section>
  )
}
