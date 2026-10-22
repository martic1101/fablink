import type { Health } from '../types'
import { projectToLimit } from '../lib/health'

const W = 640
const H = 260
const PAD = { l: 48, r: 16, t: 16, b: 30 }

/** Per-wafer health feature since the last maintenance, with SPC limits from
 *  the post-PM baseline, the spec limit, and the trend projected to it. */
export function HealthChart({ health }: { health: Health }) {
  const hist = health.history
  const limit = health.spec_limit ?? 1
  const nominal = health.nominal ?? 0
  const proj = projectToLimit(hist, limit)

  const xMax = Math.max(20, hist.length - 1, proj ? Math.ceil(proj.toX) : 0)
  const yLo = Math.min(nominal, ...hist, health.lcl ?? Infinity)
  const yHi = Math.max(limit, ...hist, health.ucl ?? -Infinity)
  const span = yHi - yLo || 1
  const y0 = yLo - span * 0.08
  const y1 = yHi + span * 0.1

  const x = (i: number) => PAD.l + (i / xMax) * (W - PAD.l - PAD.r)
  const y = (v: number) => PAD.t + (1 - (v - y0) / (y1 - y0)) * (H - PAD.t - PAD.b)
  const fmt = (v: number) => (Math.abs(v) >= 10 ? v.toFixed(0) : v.toFixed(2))

  const ticks = [y0 + (y1 - y0) * 0.1, (y0 + y1) / 2, y1 - (y1 - y0) * 0.1]
  const path = hist.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join('')

  return (
    <figure className="chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${health.indicator} per wafer since last maintenance`}>
        {ticks.map((t) => (
          <g key={t}>
            <line className="chart__grid" x1={PAD.l} x2={W - PAD.r} y1={y(t)} y2={y(t)} />
            <text className="chart__tick" x={PAD.l - 8} y={y(t) + 4} textAnchor="end">
              {fmt(t)}
            </text>
          </g>
        ))}

        {health.ucl !== null && health.lcl !== null && (
          <rect
            className="chart__band"
            x={PAD.l}
            width={W - PAD.l - PAD.r}
            y={y(health.ucl)}
            height={Math.max(1, y(health.lcl) - y(health.ucl))}
          >
            <title>Control limits (±3σ of the post-maintenance baseline)</title>
          </rect>
        )}

        <line className="chart__spec" x1={PAD.l} x2={W - PAD.r} y1={y(limit)} y2={y(limit)} />
        <text className="chart__label chart__label--spec" x={W - PAD.r} y={y(limit) - 6} textAnchor="end">
          Spec limit {fmt(limit)} {health.units}
        </text>

        {proj && (
          <>
            <line className="chart__proj" x1={x(proj.fromX)} y1={y(proj.fromY)} x2={x(proj.toX)} y2={y(proj.toY)} />
            {proj.toY >= limit - 1e-9 && <circle className="chart__hit" cx={x(proj.toX)} cy={y(limit)} r={5} />}
          </>
        )}

        <path className="chart__line" d={path} />
        {hist.map((v, i) => (
          <circle
            key={i}
            className={`chart__pt${health.ucl !== null && v > health.ucl ? ' is-out' : ''}`}
            cx={x(i)}
            cy={y(v)}
            r={2.6}
          />
        ))}

        <text className="chart__tick" x={PAD.l} y={H - 8}>
          Wafers since maintenance
        </text>
        <text className="chart__tick" x={W - PAD.r} y={H - 8} textAnchor="end">
          {xMax}
        </text>
      </svg>
      {hist.length === 0 && <figcaption className="muted">No wafers processed since the last maintenance.</figcaption>}
    </figure>
  )
}
