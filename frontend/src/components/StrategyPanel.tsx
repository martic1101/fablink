import type { Strategy } from '../types'
import { formatDuration } from '../lib/health'

const ROWS: { key: keyof Strategy; label: string; fmt?: (v: number) => string }[] = [
  { key: 'wafers', label: 'Wafers processed' },
  { key: 'scrap_rate', label: 'Scrap rate', fmt: (v) => `${v.toFixed(2)}%` },
  { key: 'repairs', label: 'Repairs after failure' },
  { key: 'unplanned_down_s', label: 'Unscheduled downtime (from failure)', fmt: formatDuration },
  { key: 'pm_count', label: 'Preventive maintenances' },
  { key: 'planned_down_s', label: 'PM time at the tool', fmt: formatDuration },
]

/** The experiment: run the same fab with and without predictive maintenance. */
export function StrategyPanel({ strategies, active }: { strategies: Strategy[]; active: 'predictive' | 'reactive' }) {
  const p = strategies.find((s) => s.mode === 'predictive')
  const r = strategies.find((s) => s.mode === 'reactive')
  if (!p || !r) return null
  return (
    <section className="strategy" aria-labelledby="strategy-title">
      <h2 id="strategy-title">Maintenance strategy results</h2>
      <p className="muted">
        Collected since this database was created. Switch strategy in the top bar to compare. These numbers come from
        the simulator, whose failure behaviour is written into it: they show the mechanics working, not real-world
        savings.
      </p>
      <table>
        <thead>
          <tr>
            <th scope="col" />
            <th scope="col" className={active === 'predictive' ? 'is-active' : ''}>
              Predictive
            </th>
            <th scope="col" className={active === 'reactive' ? 'is-active' : ''}>
              Run to failure
            </th>
          </tr>
        </thead>
        <tbody>
          {ROWS.map(({ key, label, fmt }) => (
            <tr key={key}>
              <th scope="row">{label}</th>
              <td className="num">{fmt ? fmt(p[key] as number) : p[key]}</td>
              <td className="num">{fmt ? fmt(r[key] as number) : r[key]}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}
