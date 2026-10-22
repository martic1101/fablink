import type { Alarm } from '../types'
import { timeOf } from '../lib/health'

export function AlarmList({ alarms }: { alarms: Alarm[] }) {
  const active = alarms.filter((a) => !a.cleared_at)
  const recent = alarms.filter((a) => a.cleared_at).slice(0, 8)
  return (
    <section className="alarms" aria-labelledby="alarms-title">
      <h2 id="alarms-title">
        Alarms <span className="count">{active.length} active</span>
      </h2>
      {alarms.length === 0 && <p className="muted">No alarms reported.</p>}
      <ul>
        {[...active, ...recent].map((a) => (
          <li key={a.id} className={a.cleared_at ? 'is-cleared' : a.severity <= 3 ? 'is-major' : 'is-minor'}>
            <span className="alarms__tool">{a.tool}</span>
            <span className="alarms__text">{a.text}</span>
            <span className="alarms__time num">
              {timeOf(a.set_at)}
              {a.cleared_at && <> to {timeOf(a.cleared_at)}</>}
            </span>
          </li>
        ))}
      </ul>
    </section>
  )
}
