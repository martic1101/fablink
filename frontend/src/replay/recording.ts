/**
 * A recorded FabLink run (written by backend/tools/record_demo.py) and the
 * queries that answer the console's REST calls "as of" a playback time.
 *
 * Most endpoints are rebuilt from the recorded WebSocket messages, so they are
 * exactly consistent with what the page was shown at that moment; endpoints the
 * console polls (KPIs, alarms, work orders, simulator status, trace frames) were
 * recorded each time they changed.
 */

export interface Marker {
  scenario: string
  tool: string
  t: number
  end: number | null
  complete: boolean
}

export type WsEntry = [number, string, unknown]

export interface RecordingData {
  version: number
  recorded_at: string
  duration: number
  initial: Record<string, unknown>
  rest: Record<string, [number, unknown][]>
  ws: WsEntry[]
  markers: Marker[]
}

type Timed<T> = { t: number[]; v: T[] }
type Obj = Record<string, unknown>

const TRACE_KEEP = 160

/** Number of entries in ascending `times` that are <= time. */
export function countUpTo(times: number[], time: number): number {
  let lo = 0
  let hi = times.length
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if (times[mid] <= time) lo = mid + 1
    else hi = mid
  }
  return lo
}

function timed<T>(): Timed<T> {
  return { t: [], v: [] }
}

export class Recording {
  readonly data: RecordingData
  readonly tools: string[]
  private wsTimes: number[]
  private byTopic = new Map<string, Timed<Obj>>()
  private equipment = new Map<string, Timed<Obj>>()
  private trace = new Map<string, Timed<Obj>>()

  constructor(data: RecordingData) {
    this.data = data
    this.wsTimes = data.ws.map((m) => m[0])
    this.tools = (data.initial['/api/equipment'] as Obj[]).map((e) => e.id as string)
    for (const [t, topic, raw] of data.ws) {
      const d = raw as Obj
      if (topic === 'equipment') push(this.equipment, d.id as string, t, d)
      else if (topic === 'trace') push(this.trace, d.tool as string, t, d)
      else {
        if (!this.byTopic.has(topic)) this.byTopic.set(topic, timed())
        const s = this.byTopic.get(topic)!
        s.t.push(t)
        s.v.push(d)
      }
    }
  }

  get duration(): number {
    return this.data.duration
  }

  /** Index of the first WebSocket message after `time`. */
  cursorAt(time: number): number {
    return countUpTo(this.wsTimes, time)
  }

  ws(i: number): WsEntry | undefined {
    return this.data.ws[i]
  }

  equipmentAt(tool: string, time: number): Obj {
    const s = this.equipment.get(tool)
    const n = s ? countUpTo(s.t, time) : 0
    if (s && n > 0) return s.v[n - 1]
    return (this.data.initial['/api/equipment'] as Obj[]).find((e) => e.id === tool)!
  }

  /** Last `keep` items: the initial snapshot followed by messages up to `time`. */
  private tail(topic: string, initialPath: string, time: number, keep: number): Obj[] {
    const s = this.byTopic.get(topic) ?? timed<Obj>()
    const recent = s.v.slice(0, countUpTo(s.t, time))
    return [...((this.data.initial[initialPath] as Obj[]) ?? []), ...recent].slice(-keep)
  }

  lotsAt(time: number): Obj[] {
    const lots = new Map<string, Obj>()
    for (const l of this.data.initial['/api/lots?limit=80'] as Obj[]) lots.set(l.id as string, l)
    const s = this.byTopic.get('lot')
    if (s) for (let i = 0; i < countUpTo(s.t, time); i++) lots.set(s.v[i].id as string, s.v[i])
    return [...lots.values()]
      .sort((a, b) => String(b.created_at ?? '').localeCompare(String(a.created_at ?? '')))
      .slice(0, 80)
  }

  traceAt(tool: string, time: number): Obj[] {
    const initial = ((this.data.initial[`/api/equipment/${tool}`] as Obj)?.trace as Obj[]) ?? []
    const s = this.trace.get(tool)
    const recent = s ? s.v.slice(0, countUpTo(s.t, time)).map((d) => ({ ts: d.ts, values: d.values })) : []
    return [...initial, ...recent].slice(-TRACE_KEEP)
  }

  modesAt(time: number): Obj {
    const s = this.byTopic.get('modes')
    const n = s ? countUpTo(s.t, time) : 0
    return n > 0 ? s!.v[n - 1] : (this.data.initial['/api/modes'] as Obj)
  }

  /** Answer a GET as of `time`, or undefined if the recording has nothing for it. */
  get(path: string, time: number): unknown {
    if (path === '/api/equipment') return this.tools.map((id) => this.equipmentAt(id, time))
    const detail = path.match(/^\/api\/equipment\/([^/?]+)$/)
    if (detail) return { ...this.equipmentAt(detail[1], time), trace: this.traceAt(detail[1], time) }
    if (path.startsWith('/api/lots')) return this.lotsAt(time)
    if (path.startsWith('/api/activity')) return this.tail('activity', '/api/activity?limit=300', time, 300)
    if (path.startsWith('/api/secs-log')) return this.tail('secs', '/api/secs-log?limit=120', time, 120)
    if (path === '/api/modes') return this.modesAt(time)
    const series = this.data.rest[path]
    if (series?.length) {
      const n = countUpTo(
        series.map((e) => e[0]),
        time,
      )
      return series[Math.max(0, n - 1)][1]
    }
    return this.data.initial[path]
  }
}

function push(map: Map<string, Timed<Obj>>, key: string, t: number, v: Obj) {
  if (!map.has(key)) map.set(key, timed())
  const s = map.get(key)!
  s.t.push(t)
  s.v.push(v)
}

/** Read a recording that may or may not still be gzip-compressed (servers differ). */
export async function parseRecording(buf: ArrayBuffer): Promise<RecordingData> {
  const bytes = new Uint8Array(buf)
  let text: string
  if (bytes[0] === 0x1f && bytes[1] === 0x8b) {
    const stream = new Blob([buf]).stream().pipeThrough(new DecompressionStream('gzip'))
    text = await new Response(stream).text()
  } else {
    text = new TextDecoder().decode(bytes)
  }
  return JSON.parse(text) as RecordingData
}
