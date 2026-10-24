/**
 * How the console talks to FabLink. Live, that is REST + one WebSocket to the
 * host. In the static demo build (VITE_DEMO=1, published on GitHub Pages) the
 * same calls are answered by a recording of a real run (see replay/).
 */

export const DEMO = import.meta.env.VITE_DEMO === '1'

export const REPO_URL = 'https://github.com/martic1101/fablink'

export const DEMO_WRITE_MESSAGE =
  'This is a recorded demo, so nothing can be changed. Run FabLink locally (make demo) to use the controls.'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

export interface Handlers {
  onOpen(): void
  onClose(): void
  onMessage(topic: string, data: unknown): void
  /** Replay only: playback jumped; drop state and load a fresh snapshot. */
  onReset?(): void
}

export interface Transport {
  request<T>(path: string, init?: RequestInit): Promise<T>
  /** Connect and start delivering messages; returns a function that disconnects. */
  connect(handlers: Handlers): () => void
}

export const liveTransport: Transport = {
  async request<T>(path: string, init?: RequestInit): Promise<T> {
    const res = await fetch(`/api${path}`, {
      headers: { 'Content-Type': 'application/json' },
      ...init,
    })
    if (!res.ok) {
      let detail = res.statusText
      try {
        const body = await res.json()
        detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
      } catch {
        /* body was not JSON */
      }
      throw new ApiError(res.status, detail)
    }
    return res.json() as Promise<T>
  },

  connect(h: Handlers) {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${proto}://${location.host}/api/ws`)
    ws.onopen = () => h.onOpen()
    ws.onclose = () => h.onClose()
    ws.onmessage = (ev) => {
      const { topic, data } = JSON.parse(ev.data)
      h.onMessage(topic, data)
    }
    return () => ws.close()
  },
}
