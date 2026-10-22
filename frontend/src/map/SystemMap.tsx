import { useEffect, useState } from 'react'
import { api } from '../api'
import type { FabState, Listener } from '../useFab'
import type { SimStatus, SystemInfo } from '../types'
import { Diagram, type Highlight, type NodeId } from './Diagram'
import { Inspector } from './Inspector'
import { Scenarios } from './Scenarios'
import './map.css'

function usePoll<T>(load: () => Promise<T>, ms: number): [T | null, boolean] {
  const [data, setData] = useState<T | null>(null)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    let alive = true
    const tick = () =>
      load().then(
        (d) => alive && (setData(d), setFailed(false)),
        () => alive && (setData(null), setFailed(true)),
      )
    tick()
    const t = window.setInterval(tick, ms)
    return () => {
      alive = false
      window.clearInterval(t)
    }
  }, [load, ms])
  return [data, failed]
}

const loadSim = () => api.simTools()
const loadSystem = () => api.system()

export function SystemMap({ state, subscribe }: { state: FabState; subscribe: (fn: Listener) => () => void }) {
  const tools = Object.values(state.equipment).sort((a, b) => a.id.localeCompare(b.id))
  const [simList] = usePoll<SimStatus[]>(loadSim, 1000)
  const [system] = usePoll<SystemInfo>(loadSystem, 2000)
  const sim = simList ? Object.fromEntries(simList.map((s) => [s.id, s])) : null
  const [selected, setSelected] = useState<NodeId>('host')
  const [scenTool, setScenTool] = useState<string | null>(null)
  const [highlight, setHighlight] = useState<Highlight | null>(null)
  const [followOne, setFollowOne] = useState(true)

  const tool = scenTool ?? tools[0]?.id ?? ''
  const lots = Object.values(state.lots)
  const count = (s: string) => lots.filter((l) => l.status === s).length
  const modelled = tools.filter((t) => t.health).length

  const live: Partial<Record<NodeId, string>> = {
    host: `${state.secs.length} recent messages logged`,
    analytics: `${modelled} tool${modelled === 1 ? '' : 's'} modelled`,
    dispatcher: state.modes?.predictive === false ? 'Run to failure' : 'Predictive maintenance on',
    mes: `${count('QUEUED')} queued, ${count('RUNNING')} running, ${count('HOLD')} held`,
    database: system ? `${system.tables.wafer_results.toLocaleString()} wafer rows` : undefined,
    api: system ? `${system.ws_clients} browser${system.ws_clients === 1 ? '' : 's'} connected` : undefined,
    console: 'This page',
    crew: (() => {
      const n = tools.filter((t) => t.work_order && t.work_order.status !== 'OPEN').length
      return n ? `${n} job${n === 1 ? '' : 's'} in progress` : 'No jobs'
    })(),
  }

  const select = (n: NodeId) => {
    setSelected(n)
    if (n.startsWith('tool:') || n.startsWith('session:')) setScenTool(n.split(':')[1])
  }

  if (!tools.length) {
    return <p className="empty">Waiting for the FabLink host to report its tools.</p>
  }

  return (
    <div className="map">
      <header className="map__intro">
        <h1>System map</h1>
        <p>
          What is simulated, which part of FabLink handles what, and how data moves between them. Dots travel along a
          connection each time a real message does. Click any box to see what it does.
        </p>
        <ul className="legend" aria-label="Legend">
          <li>
            <span className="legend__dot pulse--in" /> report from a tool
          </li>
          <li>
            <span className="legend__dot pulse--trace" /> trace sample
          </li>
          <li>
            <span className="legend__dot pulse--out" /> reply to a tool
          </li>
          <li>
            <span className="legend__dot pulse--cmd" /> command to a tool
          </li>
          <li>
            <span className="legend__dot pulse--svc" /> inside the host
          </li>
          <li>
            <label className="check">
              <input type="checkbox" checked={followOne} onChange={(e) => setFollowOne(e.target.checked)} />
              Only animate {tool}
            </label>
          </li>
        </ul>
      </header>

      <div className="map__top">
        <div className="map__diagram">
          <Diagram
            tools={tools}
            sim={sim}
            live={live}
            selected={selected}
            onSelect={select}
            highlight={highlight}
            focusTool={followOne ? tool : null}
            subscribe={subscribe}
          />
        </div>
        <aside className="map__inspector" aria-label="Details">
          <Inspector node={selected} tools={tools} sim={sim} activity={state.activity} system={system} />
        </aside>
      </div>

      <Scenarios
        tools={tools}
        tool={tool}
        onToolChange={(id) => {
          setScenTool(id)
          setSelected(`tool:${id}`)
        }}
        modes={state.modes}
        sim={sim}
        activity={state.activity}
        subscribe={subscribe}
        onHighlight={setHighlight}
      />
    </div>
  )
}
