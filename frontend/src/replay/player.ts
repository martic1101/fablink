import { ApiError, DEMO_WRITE_MESSAGE, type Handlers, type Transport } from '../transport'
import { parseRecording, Recording, type Marker } from './recording'

const TICK_MS = 100

/**
 * Plays a recording back as if it were the live host: answers REST calls as of
 * the playback time and delivers the recorded WebSocket messages on schedule.
 * Loops at the end. Writes (POST/PUT) are refused.
 */
export class Player implements Transport {
  rec: Recording | null = null
  error: string | null = null
  t = 0
  rate = 1
  playing = true
  readonly ready: Promise<void>
  private cursor = 0
  private handlers = new Set<Handlers>()
  private clock = new Set<() => void>()
  private timer: number | undefined
  private last = 0

  constructor(url: string) {
    this.ready = this.load(url)
  }

  private async load(url: string) {
    try {
      const res = await fetch(url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      this.rec = new Recording(await parseRecording(await res.arrayBuffer()))
    } catch (e) {
      this.error = `Could not load the recording (${(e as Error).message}).`
    }
    this.notify()
  }

  get duration(): number {
    return this.rec?.duration ?? 0
  }

  get markers(): Marker[] {
    return this.rec?.data.markers ?? []
  }

  // ---- Transport ----------------------------------------------------------------------------
  async request<T>(path: string, init?: RequestInit): Promise<T> {
    if (init?.method && init.method !== 'GET') throw new ApiError(403, DEMO_WRITE_MESSAGE)
    await this.ready
    const body = this.rec?.get(`/api${path}`, this.t)
    if (body === undefined) throw new ApiError(404, 'Not in the recording.')
    return body as T
  }

  connect(h: Handlers): () => void {
    this.handlers.add(h)
    this.ready.then(() => {
      if (!this.rec || !this.handlers.has(h)) return
      h.onOpen()
      this.greet(h)
      this.start()
    })
    return () => {
      this.handlers.delete(h)
    }
  }

  /** What the live host sends right after a WebSocket connects. */
  private greet(h: Handlers) {
    if (!this.rec) return
    h.onMessage('modes', this.rec.modesAt(this.t))
    for (const id of this.rec.tools) h.onMessage('equipment', this.rec.equipmentAt(id, this.t))
  }

  // ---- playback -------------------------------------------------------------------------------
  private start() {
    if (this.timer !== undefined) return
    this.last = performance.now()
    this.timer = window.setInterval(() => this.tick(), TICK_MS)
  }

  private tick() {
    const now = performance.now()
    const dt = (now - this.last) / 1000
    this.last = now
    if (!this.playing || !this.rec) return
    const next = this.t + dt * this.rate
    if (next >= this.rec.duration) {
      this.seek(0) // loop
      return
    }
    this.advanceTo(next)
  }

  private advanceTo(time: number) {
    const rec = this.rec!
    for (let e = rec.ws(this.cursor); e && e[0] <= time; e = rec.ws(++this.cursor)) {
      for (const h of this.handlers) h.onMessage(e[1], e[2])
    }
    this.t = time
    this.notify()
  }

  seek(time: number) {
    if (!this.rec) return
    this.t = Math.max(0, Math.min(time, this.rec.duration))
    this.cursor = this.rec.cursorAt(this.t)
    for (const h of this.handlers) {
      h.onReset?.()
      this.greet(h)
    }
    this.notify()
  }

  play() {
    this.playing = true
    this.notify()
  }

  pause() {
    this.playing = false
    this.notify()
  }

  setRate(rate: number) {
    this.rate = rate
    this.notify()
  }

  subscribe(fn: () => void): () => void {
    this.clock.add(fn)
    return () => {
      this.clock.delete(fn)
    }
  }

  private notify() {
    for (const fn of this.clock) fn()
  }
}

let player: Player | null = null

/** The demo build's single player. */
export function getPlayer(): Player {
  if (!player) player = new Player(`${import.meta.env.BASE_URL}recording.json.gz`)
  return player
}
