import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import type { Activity, Block, Equipment, SecsEntry, SimStatus } from '../types'
import type { Listener } from '../useFab'
import { TOOL_TYPES } from './content'
import {
  BOX,
  COLUMNS,
  GATEWAY_GROUP,
  ROUTES,
  VIEW,
  pointAt,
  sessionBox,
  toolBox,
  toolCy,
  toPath,
  type Box,
  type Pt,
} from './layout'

export type NodeId = Exclude<Block, 'simulator'> | `tool:${string}` | `session:${string}`

export interface Highlight {
  block: Block
  tool: string
}

interface Props {
  tools: Equipment[]
  sim: Record<string, SimStatus> | null
  live: Partial<Record<NodeId, string>>
  selected: NodeId
  onSelect: (n: NodeId) => void
  highlight: Highlight | null
  /** only animate traffic for this tool (null = all tools) */
  focusTool: string | null
  subscribe: (fn: Listener) => () => void
}

interface Pulse {
  id: number
  pts: Pt[]
  cls: string
  start: number
  dur: number
}

const STATE_LABEL: Record<string, string> = {
  PROCESSING: 'Processing',
  IDLE: 'Idle',
  PM: 'Preventive maintenance',
  DOWN: 'Down',
  OFFLINE: 'Offline',
}

const HOST_HANDLERS: [string, string][] = [
  ['S6F11', 'collection events'],
  ['S6F1', 'trace data'],
  ['S5F1', 'alarms'],
  ['S6F12, S5F2', 'replies'],
  ['S2F41, S6F23', 'requests to the tool'],
]

export function Diagram({ tools, sim, live, selected, onSelect, highlight, focusTool, subscribe }: Props) {
  const n = tools.length
  const pulses = useRef<Pulse[]>([])
  const flashes = useRef(new Map<string, number>())
  const lastTrace = useRef(new Map<string, number>())
  const index = useRef<Record<string, number>>({})
  const focus = useRef(focusTool)
  const raf = useRef(0)
  const seq = useRef(0)
  const [, setTick] = useState(0)
  const reduced = useMemo(() => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false, [])

  useEffect(() => {
    index.current = Object.fromEntries(tools.map((t, i) => [t.id, i]))
  }, [tools])
  useEffect(() => {
    focus.current = focusTool
  }, [focusTool])

  const kick = useCallback(() => {
    if (raf.current) return
    const loop = () => {
      const now = performance.now()
      pulses.current = pulses.current.filter((p) => now - p.start < p.dur)
      for (const [k, until] of flashes.current) if (until < now) flashes.current.delete(k)
      setTick((t) => t + 1)
      raf.current = pulses.current.length || flashes.current.size ? requestAnimationFrame(loop) : 0
    }
    raf.current = requestAnimationFrame(loop)
  }, [])

  useEffect(() => () => cancelAnimationFrame(raf.current), [])

  useEffect(
    () =>
      subscribe((topic, raw) => {
        const now = performance.now()
        const count = Object.keys(index.current).length
        const add = (pts: Pt[], cls: string, dur = 700, delay = 0) => {
          if (reduced || pulses.current.length > 90) return
          pulses.current.push({ id: ++seq.current, pts, cls, start: now + delay, dur })
        }
        const flash = (node: string) => flashes.current.set(node, now + 700)
        const tool = (raw as { tool?: string | null }).tool
        if (focus.current && tool && tool !== focus.current) return

        if (topic === 'secs') {
          const e = raw as SecsEntry
          const i = index.current[e.tool]
          if (i === undefined) return
          if (e.dir === 'in') add(ROUTES.toolToHost(i, count), 'pulse--in')
          else if (e.sf === 'S2F41') {
            add(ROUTES.command(i, count), 'pulse--cmd', 1100)
            flash('dispatcher')
          } else add(ROUTES.hostToTool(i, count), 'pulse--out', 600)
          flash(`session:${e.tool}`)
        } else if (topic === 'trace') {
          if (!tool) return
          const i = index.current[tool]
          if (i === undefined || now - (lastTrace.current.get(tool) ?? 0) < 650) return
          lastTrace.current.set(tool, now)
          add(ROUTES.toolToHost(i, count), 'pulse--trace', 650)
        } else if (topic === 'activity') {
          const a = raw as Activity
          switch (a.block) {
            case 'simulator':
              flash(`tool:${a.tool}`)
              break
            case 'gateway':
              flash(`session:${a.tool}`)
              break
            case 'host':
              flash('host')
              if (a.kind === 'maint.started' || (a.kind === 'control' && a.data.state === 'ONLINE_LOCAL')) {
                flash('crew')
                flash(`tool:${a.tool}`)
              }
              break
            case 'analytics':
            case 'mes':
            case 'database':
              add(ROUTES.hostTo(a.block), 'pulse--svc', 450)
              flash(a.block)
              break
            case 'dispatcher':
              if (a.kind === 'wo.opened' && a.data.trigger === 'PREDICTIVE')
                add(ROUTES.analyticsToDispatcher(), 'pulse--svc', 450)
              if (a.kind === 'wo.sent') {
                add(ROUTES.dispatcherToCrew(), 'pulse--cmd', 700)
                flash('crew')
              }
              flash('dispatcher')
              break
            case 'api':
              add(ROUTES.hostTo('api'), 'pulse--svc', 500)
              add(ROUTES.apiToConsole(), 'pulse--svc', 350, 500)
              flash('api')
              flash('console')
              break
          }
        } else return
        kick()
      }),
    [subscribe, kick, reduced],
  )

  const now = performance.now()
  const isFlash = (id: string) => flashes.current.has(id)
  const dimmed = (id: NodeId): boolean => {
    if (!highlight) return false
    const { block, tool } = highlight
    if (block === 'simulator') return id !== `tool:${tool}`
    if (block === 'gateway') return id !== `session:${tool}`
    return id !== block
  }

  const node = (id: NodeId, box: Box, label: string, children: ReactNode, extra = '') => (
    <g
      key={id}
      className={`node ${extra} ${isFlash(id) ? 'is-flash' : ''} ${selected === id ? 'is-selected' : ''} ${
        dimmed(id) ? 'is-dim' : ''
      }`}
      transform={`translate(${box.x},${box.y})`}
      role="button"
      tabIndex={0}
      aria-label={label}
      aria-pressed={selected === id}
      onClick={() => onSelect(id)}
      onKeyDown={(e: KeyboardEvent) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onSelect(id)
        }
      }}
    >
      <rect className="node__box" width={box.w} height={box.h} rx={2} />
      {children}
    </g>
  )

  const service = (id: keyof typeof BOX & NodeId, title: string, sub: string) =>
    node(
      id,
      BOX[id],
      title,
      <>
        <text className="node__title" x={12} y={26}>
          {title}
        </text>
        <text className="node__sub" x={12} y={44}>
          {sub}
        </text>
        {live[id] && (
          <text className="node__live" x={12} y={64}>
            {live[id]}
          </text>
        )}
      </>,
    )

  return (
    <svg className="diagram" viewBox={`0 0 ${VIEW.w} ${VIEW.h}`} role="group" aria-label="System map">
      {COLUMNS.map((c) => (
        <text key={c.label} className="diagram__col" x={c.x} y={14}>
          {c.label}
        </text>
      ))}

      <rect
        className="diagram__group"
        x={GATEWAY_GROUP.x}
        y={GATEWAY_GROUP.y}
        width={GATEWAY_GROUP.w}
        height={GATEWAY_GROUP.h}
        rx={3}
      />

      {/* connections */}
      {tools.map((t, i) => (
        <g key={t.id}>
          <path className={`edge ${t.connected ? '' : 'edge--down'}`} d={toPath(ROUTES.toolToHost(i, n))} />
          <text className="edge__label" x={252} y={toolCy(i, n) - 7} textAnchor="middle">
            :{sim?.[t.id]?.port ?? t.address.split(':').pop()}
          </text>
        </g>
      ))}
      <path className="edge" d={toPath(ROUTES.hostTo('analytics'))} />
      <path className="edge edge--cmd" d={toPath(ROUTES.hostTo('dispatcher'))} />
      <path className="edge" d={toPath(ROUTES.hostTo('mes'))} />
      <path className="edge" d={toPath(ROUTES.hostTo('database'))} />
      <path className="edge" d={toPath(ROUTES.hostTo('api'))} />
      <path className="edge" d={toPath(ROUTES.analyticsToDispatcher())} />
      <path className="edge" d={toPath(ROUTES.mesToDatabase())} />
      <path className="edge" d={toPath(ROUTES.apiToConsole())} />
      <path className="edge edge--cmd" d={toPath(ROUTES.dispatcherToCrew())} />

      {/* tools */}
      {tools.map((t, i) => {
        const box = toolBox(i, n)
        const s = sim?.[t.id]
        const truth = s
          ? [
              `Wear ${(s.wear * 100).toFixed(0)}%`,
              s.wear_multiplier > 1 ? `×${s.wear_multiplier} rate` : '',
              s.spooled ? `${s.spooled} spooled` : '',
              s.blocked_for > 0 ? `link cut ${Math.ceil(s.blocked_for)} s` : '',
            ]
              .filter(Boolean)
              .join(', ')
          : 'Simulator control off'
        return node(
          `tool:${t.id}`,
          box,
          `${t.id}, ${STATE_LABEL[t.state]}`,
          <>
            <rect className={`node__band band--${t.state.toLowerCase()}`} width={5} height={box.h} />
            <text className="node__title node__title--tool" x={16} y={28}>
              {t.id}
            </text>
            <text className="node__sub" x={16} y={46}>
              {t.mdln ?? 'unknown model'} {t.tool_type ? TOOL_TYPES[t.tool_type]?.short : ''}
            </text>
            <text className={`node__state state--${t.state.toLowerCase()}`} x={16} y={66}>
              {STATE_LABEL[t.state]}
              {t.lot_id ? `, wafer ${t.slot || 1}/${t.lot_wafers}` : ''}
            </text>
            <text className="node__truth" x={16} y={88}>
              <title>Ground truth inside the simulator. The host never sees this.</title>
              {truth}
            </text>
          </>,
        )
      })}

      <text className="diagram__grouplabel" x={GATEWAY_GROUP.x + 12} y={GATEWAY_GROUP.y + GATEWAY_GROUP.h - 10}>
        app/equipment/gateway.py
      </text>

      {/* gateway sessions */}
      {tools.map((t, i) =>
        node(
          `session:${t.id}`,
          sessionBox(i, n),
          `HSMS session to ${t.id}`,
          <>
            <text className="node__title node__title--small" x={12} y={22}>
              HSMS session
            </text>
            <circle className={t.connected ? 'dot dot--up' : 'dot'} cx={16} cy={37} r={4} />
            <text className="node__sub" x={26} y={41}>
              {t.connected ? 'Selected' : 'Offline, retrying'}
            </text>
          </>,
        ),
      )}

      {/* host */}
      {node(
        'host',
        BOX.host,
        'FabHost',
        <>
          <text className="node__title" x={12} y={26}>
            FabHost
          </text>
          <text className="node__sub" x={12} y={44}>
            event handling
          </text>
          {HOST_HANDLERS.map(([sf, what], i) => (
            <g key={sf} transform={`translate(12, ${92 + i * 52})`}>
              <text className="node__sf">{sf}</text>
              <text className="node__sub" y={17}>
                {what}
              </text>
            </g>
          ))}
          {live.host && (
            <text className="node__live" x={12} y={BOX.host.h - 16}>
              {live.host}
            </text>
          )}
        </>,
      )}

      {service('analytics', 'Analytics', 'SPC, health, wafers left')}
      {service('dispatcher', 'Dispatcher', 'lots and maintenance')}
      {service('mes', 'MES-lite', 'lot tracking and holds')}
      {service('database', 'Database', 'SQLite or PostgreSQL')}
      {service('crew', 'Technicians', 'simulated crew')}
      {service('api', 'REST + WebSocket', 'FastAPI')}
      {service('console', 'Browser', 'React console')}

      {pulses.current.map((p) => {
        const t = (now - p.start) / p.dur
        if (t < 0) return null
        const [x, y] = pointAt(p.pts, t)
        return <circle key={p.id} className={`pulse ${p.cls}`} cx={x} cy={y} r={p.cls === 'pulse--trace' ? 3 : 4.5} />
      })}
    </svg>
  )
}
