import type { Activity, Equipment, SimStatus, SystemInfo } from '../types'
import { timeOf } from '../lib/health'
import { BLOCKS, SENSOR_INFO, TOOL_TYPES } from './content'
import type { NodeId } from './Diagram'
import { FrameView } from './FrameView'

// The crew is outside FabLink; what the host sees of it is these reports.
const CREW_KINDS = ['wo.sent', 'control', 'maint.started', 'maint.completed']

interface Props {
  node: NodeId
  tools: Equipment[]
  sim: Record<string, SimStatus> | null
  activity: Activity[]
  system: SystemInfo | null
}

function Recent({ entries }: { entries: Activity[] }) {
  if (!entries.length) return <p className="muted">Nothing yet.</p>
  return (
    <ul className="recent">
      {entries.map((e) => (
        <li key={e.id}>
          <span className="num muted">{timeOf(e.ts)}</span> {e.tool && <strong>{e.tool} </strong>}
          {e.text}
        </li>
      ))}
    </ul>
  )
}

export function Inspector({ node, tools, sim, activity, system }: Props) {
  if (node.startsWith('tool:')) {
    const id = node.slice(5)
    const tool = tools.find((t) => t.id === id)
    if (!tool) return null
    const info = tool.tool_type ? TOOL_TYPES[tool.tool_type] : undefined
    const s = sim?.[id]
    return (
      <div className="inspector">
        <h2>
          {id} <span className="muted">{info?.name}</span>
        </h2>
        {info && (
          <>
            <p>{info.what}</p>
            <p>{info.wears}</p>
          </>
        )}
        <h3>Sensors</h3>
        <ul className="sensor-list">
          {tool.sensors.map((x) => (
            <li key={x.svid}>
              <span>
                <strong>{x.name}</strong> <span className="muted">{SENSOR_INFO[x.name]}</span>
              </span>
              <span className="num">
                {x.value === null ? '–' : x.value.toFixed(x.value >= 100 ? 0 : 2)} {x.units}
              </span>
            </li>
          ))}
        </ul>
        <h3>Inside the simulator</h3>
        {s ? (
          <dl className="truth">
            <dt>Part wear</dt>
            <dd className="num">{(s.wear * 100).toFixed(1)}%</dd>
            <dt>Wear rate</dt>
            <dd className="num">×{s.wear_multiplier}</dd>
            <dt>Spooled reports</dt>
            <dd className="num">{s.spooled}</dd>
            <dt>Link</dt>
            <dd>{s.blocked_for > 0 ? `cut, ${Math.ceil(s.blocked_for)} s left` : s.communicating ? 'up' : 'down'}</dd>
          </dl>
        ) : (
          <p className="muted">Simulator control is not reachable.</p>
        )}
        <p className="muted small">The host never sees these numbers. It has to infer wear from the sensors.</p>
        <FrameView tool={id} />
      </div>
    )
  }

  if (node.startsWith('session:')) {
    const id = node.slice(8)
    const tool = tools.find((t) => t.id === id)
    const info = BLOCKS.gateway
    return (
      <div className="inspector">
        <h2>
          HSMS session <span className="muted">to {id}</span>
        </h2>
        <dl className="truth">
          <dt>Address</dt>
          <dd>{tool?.address}</dd>
          <dt>Status</dt>
          <dd>{tool?.connected ? 'Selected, communicating' : 'Offline, retrying'}</dd>
          {tool?.last_error && (
            <>
              <dt>Last error</dt>
              <dd>{tool.last_error}</dd>
            </>
          )}
        </dl>
        <h3>What the gateway does</h3>
        <ul className="does">
          {info.does.map((d) => (
            <li key={d}>{d}</li>
          ))}
        </ul>
        <p className="source">{info.source}</p>
        <h3>Recent</h3>
        <Recent entries={activity.filter((a) => a.block === 'gateway' && a.tool === id).slice(-6).reverse()} />
      </div>
    )
  }

  const block = node as keyof typeof BLOCKS
  const info = BLOCKS[block]
  return (
    <div className="inspector">
      <h2>
        {info.title} <span className="muted">{info.short}</span>
      </h2>
      <ul className="does">
        {info.does.map((d) => (
          <li key={d}>{d}</li>
        ))}
      </ul>
      {info.note && <p className="note">{info.note}</p>}
      <p className="source">{info.source}</p>
      {block === 'database' && system && (
        <>
          <h3>Rows right now</h3>
          <table>
            <tbody>
              {Object.entries(system.tables).map(([t, n]) => (
                <tr key={t}>
                  <th scope="row">{t}</th>
                  <td className="num">{n.toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      {block !== 'console' && (
        <>
          <h3>Recent</h3>
          <Recent
            entries={activity
              .filter((a) => (block === 'crew' ? CREW_KINDS.includes(a.kind) : a.block === block))
              .slice(-6)
              .reverse()}
          />
        </>
      )}
    </div>
  )
}
