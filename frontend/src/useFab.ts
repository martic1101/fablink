import { useCallback, useEffect, useReducer, useRef } from 'react'
import { api, wsUrl } from './api'
import type {
  Activity,
  Alarm,
  Equipment,
  Kpi,
  Lot,
  Modes,
  SecsEntry,
  TracePoint,
  WaferEvent,
  WorkOrder,
} from './types'

const TRACE_KEEP = 160
const SECS_KEEP = 150
const ACTIVITY_KEEP = 300

export type Listener = (topic: string, data: unknown) => void

export interface FabState {
  live: boolean
  /** the host was shut down from the console */
  shutDown: boolean
  equipment: Record<string, Equipment>
  lots: Record<string, Lot>
  alarms: Alarm[]
  secs: SecsEntry[]
  traces: Record<string, TracePoint[]>
  /** per tool: slot results of the lot currently on it */
  slots: Record<string, { lot: string; results: Record<number, 'OK' | 'SCRAP'> }>
  modes: Modes | null
  kpi: Kpi | null
  activity: Activity[]
  workOrders: WorkOrder[]
}

type Action =
  | { type: 'live'; live: boolean }
  | { type: 'shutDown' }
  | { type: 'equipment'; data: Equipment }
  | { type: 'lot'; data: Lot }
  | { type: 'lots'; data: Lot[] }
  | { type: 'alarms'; data: Alarm[] }
  | { type: 'secs'; data: SecsEntry }
  | { type: 'secsAll'; data: SecsEntry[] }
  | { type: 'trace'; tool: string; point: TracePoint }
  | { type: 'traceAll'; tool: string; points: TracePoint[] }
  | { type: 'wafer'; data: WaferEvent }
  | { type: 'modes'; data: Modes }
  | { type: 'kpi'; data: Kpi }
  | { type: 'activity'; data: Activity }
  | { type: 'activityAll'; data: Activity[] }
  | { type: 'workOrders'; data: WorkOrder[] }

const initial: FabState = {
  live: false,
  shutDown: false,
  equipment: {},
  lots: {},
  alarms: [],
  secs: [],
  traces: {},
  slots: {},
  modes: null,
  kpi: null,
  activity: [],
  workOrders: [],
}

function reducer(s: FabState, a: Action): FabState {
  switch (a.type) {
    case 'live':
      return { ...s, live: a.live }
    case 'shutDown':
      return { ...s, shutDown: true, live: false }
    case 'equipment':
      return { ...s, equipment: { ...s.equipment, [a.data.id]: a.data } }
    case 'lot':
      return { ...s, lots: { ...s.lots, [a.data.id]: a.data } }
    case 'lots':
      return { ...s, lots: Object.fromEntries(a.data.map((l) => [l.id, l])) }
    case 'alarms':
      return { ...s, alarms: a.data }
    case 'secs':
      return { ...s, secs: [...s.secs.slice(-SECS_KEEP + 1), a.data] }
    case 'secsAll':
      return { ...s, secs: a.data }
    case 'trace': {
      const prev = s.traces[a.tool] ?? []
      return { ...s, traces: { ...s.traces, [a.tool]: [...prev.slice(-TRACE_KEEP + 1), a.point] } }
    }
    case 'traceAll':
      return { ...s, traces: { ...s.traces, [a.tool]: a.points.slice(-TRACE_KEEP) } }
    case 'wafer': {
      const cur = s.slots[a.data.tool]
      const results = cur && cur.lot === a.data.lot ? cur.results : {}
      return {
        ...s,
        slots: {
          ...s.slots,
          [a.data.tool]: { lot: a.data.lot, results: { ...results, [a.data.slot]: a.data.result } },
        },
      }
    }
    case 'modes':
      return { ...s, modes: a.data }
    case 'kpi':
      return { ...s, kpi: a.data }
    case 'activity':
      return { ...s, activity: [...s.activity.slice(-ACTIVITY_KEEP + 1), a.data] }
    case 'activityAll':
      return { ...s, activity: a.data }
    case 'workOrders':
      return { ...s, workOrders: a.data }
  }
}

/** Snapshot over REST, then live updates over one WebSocket (with reconnect). */
export function useFab() {
  const [state, dispatch] = useReducer(reducer, initial)
  const alarmTimer = useRef<number | undefined>(undefined)
  // Components that react to individual messages (the System map animation,
  // scenario steps) subscribe here instead of diffing state.
  const listeners = useRef(new Set<Listener>())
  const subscribe = useCallback((fn: Listener) => {
    listeners.current.add(fn)
    return () => {
      listeners.current.delete(fn)
    }
  }, [])

  const refreshAlarms = useCallback(() => {
    // Alarms come in bursts (set + clear); debounce the refetch.
    window.clearTimeout(alarmTimer.current)
    alarmTimer.current = window.setTimeout(() => {
      api.alarms().then((d) => dispatch({ type: 'alarms', data: d }), () => {})
    }, 300)
  }, [])

  const woTimer = useRef<number | undefined>(undefined)
  const refreshWorkOrders = useCallback(() => {
    window.clearTimeout(woTimer.current)
    woTimer.current = window.setTimeout(() => {
      api.workOrders().then((d) => dispatch({ type: 'workOrders', data: d }), () => {})
    }, 300)
  }, [])

  const refreshKpi = useCallback(() => {
    api.kpi().then((d) => dispatch({ type: 'kpi', data: d }), () => {})
  }, [])

  useEffect(() => {
    let ws: WebSocket | null = null
    let retry: number | undefined
    let closed = false

    const loadSnapshot = () => {
      api.lots().then((d) => dispatch({ type: 'lots', data: d }), () => {})
      api.alarms().then((d) => dispatch({ type: 'alarms', data: d }), () => {})
      api.secsLog().then((d) => dispatch({ type: 'secsAll', data: d }), () => {})
      api.activity().then((d) => dispatch({ type: 'activityAll', data: d }), () => {})
      api.workOrders().then((d) => dispatch({ type: 'workOrders', data: d }), () => {})
      api.equipment().then(
        (list) =>
          list.forEach((e) =>
            api.equipmentDetail(e.id).then(
              (d) => dispatch({ type: 'traceAll', tool: e.id, points: d.trace }),
              () => {},
            ),
          ),
        () => {},
      )
      refreshKpi()
    }

    const connect = () => {
      ws = new WebSocket(wsUrl())
      ws.onopen = () => {
        dispatch({ type: 'live', live: true })
        loadSnapshot()
      }
      ws.onclose = () => {
        dispatch({ type: 'live', live: false })
        if (!closed) retry = window.setTimeout(connect, 1500)
      }
      ws.onmessage = (ev) => {
        const { topic, data } = JSON.parse(ev.data)
        listeners.current.forEach((fn) => fn(topic, data))
        switch (topic) {
          case 'equipment':
            dispatch({ type: 'equipment', data })
            break
          case 'lot':
            dispatch({ type: 'lot', data })
            break
          case 'trace':
            dispatch({ type: 'trace', tool: data.tool, point: { ts: data.ts, values: data.values } })
            break
          case 'wafer':
            dispatch({ type: 'wafer', data })
            break
          case 'secs':
            dispatch({ type: 'secs', data })
            break
          case 'alarm':
            refreshAlarms()
            break
          case 'modes':
            dispatch({ type: 'modes', data })
            break
          case 'maintenance':
            refreshKpi()
            break
          case 'activity':
            dispatch({ type: 'activity', data })
            break
          case 'work_order':
            refreshWorkOrders()
            break
          case 'shutdown':
            closed = true // don't try to reconnect to a host that was stopped on purpose
            dispatch({ type: 'shutDown' })
            break
        }
      }
    }
    connect()
    const kpiTimer = window.setInterval(refreshKpi, 5000)
    return () => {
      closed = true
      window.clearTimeout(retry)
      window.clearInterval(kpiTimer)
      ws?.close()
    }
  }, [refreshAlarms, refreshKpi, refreshWorkOrders])

  const markShutDown = useCallback(() => dispatch({ type: 'shutDown' }), [])

  return { state, refreshKpi, subscribe, markShutDown }
}
