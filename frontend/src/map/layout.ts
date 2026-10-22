/** Geometry of the System map diagram (SVG user units). */

export type Pt = [number, number]

export interface Box {
  x: number
  y: number
  w: number
  h: number
}

export const VIEW = { w: 1120, h: 530 }
const TOP = 34 // room for the column headings

export const COLUMNS: { x: number; label: string }[] = [
  { x: 20, label: 'Simulated tools' },
  { x: 285, label: 'Equipment gateway' },
  { x: 520, label: 'Host core' },
  { x: 750, label: 'Services' },
  { x: 960, label: 'People and clients' },
]

/** Vertical slot for tool i (up to 4 tools fit). */
function slot(i: number, n: number) {
  const pitch = Math.min(120, (VIEW.h - TOP - 10) / Math.max(n, 1))
  return { top: TOP + i * pitch, cy: TOP + i * pitch + 50 }
}

export function toolBox(i: number, n: number): Box {
  return { x: 20, y: slot(i, n).top, w: 200, h: 100 }
}

export function sessionBox(i: number, n: number): Box {
  return { x: 285, y: slot(i, n).cy - 27, w: 160, h: 54 }
}

export const toolCy = (i: number, n: number) => slot(i, n).cy

export const GATEWAY_GROUP: Box = { x: 270, y: TOP - 8, w: 190, h: VIEW.h - TOP }

export const BOX = {
  host: { x: 520, y: TOP, w: 160, h: 470 },
  analytics: { x: 750, y: TOP, w: 160, h: 80 },
  dispatcher: { x: 750, y: TOP + 120, w: 160, h: 80 },
  mes: { x: 750, y: TOP + 240, w: 160, h: 80 },
  database: { x: 750, y: TOP + 360, w: 160, h: 90 },
  crew: { x: 960, y: TOP, w: 140, h: 90 },
  api: { x: 960, y: TOP + 180, w: 140, h: 80 },
  console: { x: 960, y: TOP + 320, w: 140, h: 80 },
} satisfies Record<string, Box>

const mid = (b: Box): number => b.y + b.h / 2
const right = (b: Box) => b.x + b.w

/** Horizontal level where the host talks to each service (inside its box). */
const lane = {
  analytics: mid(BOX.analytics),
  dispatcher: mid(BOX.dispatcher),
  mes: mid(BOX.mes),
  database: mid(BOX.database),
  api: mid(BOX.api), // passes through the gap between dispatcher and MES
}

export const ROUTES = {
  toolToHost: (i: number, n: number): Pt[] => {
    const y = toolCy(i, n)
    return [
      [220, y],
      [285, y],
      [445, y],
      [BOX.host.x, y],
    ]
  },
  hostToTool: (i: number, n: number): Pt[] => [...ROUTES.toolToHost(i, n)].reverse(),
  /** Dispatcher → (through the host) → gateway session → tool */
  command: (i: number, n: number): Pt[] => {
    const y = toolCy(i, n)
    return [
      [BOX.dispatcher.x, lane.dispatcher],
      [right(BOX.host), lane.dispatcher],
      [BOX.host.x, y],
      [445, y],
      [285, y],
      [220, y],
    ]
  },
  hostTo: (target: 'analytics' | 'dispatcher' | 'mes' | 'database' | 'api'): Pt[] => [
    [right(BOX.host), lane[target]],
    [target === 'api' ? BOX.api.x : BOX[target].x, lane[target]],
  ],
  analyticsToDispatcher: (): Pt[] => [
    [830, BOX.analytics.y + BOX.analytics.h],
    [830, BOX.dispatcher.y],
  ],
  mesToDatabase: (): Pt[] => [
    [830, BOX.mes.y + BOX.mes.h],
    [830, BOX.database.y],
  ],
  /** Work orders leave the host for the (simulated) technicians. */
  dispatcherToCrew: (): Pt[] => [
    [right(BOX.dispatcher), lane.dispatcher],
    [935, lane.dispatcher],
    [935, mid(BOX.crew)],
    [BOX.crew.x, mid(BOX.crew)],
  ],
  apiToConsole: (): Pt[] => [
    [1030, BOX.api.y + BOX.api.h],
    [1030, BOX.console.y],
  ],
}

/** Point at fraction t (0..1) along a polyline. */
export function pointAt(pts: Pt[], t: number): Pt {
  const segs: number[] = []
  let total = 0
  for (let i = 1; i < pts.length; i++) {
    const d = Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
    segs.push(d)
    total += d
  }
  if (total === 0) return pts[0]
  let target = Math.max(0, Math.min(1, t)) * total
  for (let i = 0; i < segs.length; i++) {
    if (target <= segs[i] || i === segs.length - 1) {
      const f = segs[i] ? Math.min(1, target / segs[i]) : 0
      return [pts[i][0] + (pts[i + 1][0] - pts[i][0]) * f, pts[i][1] + (pts[i + 1][1] - pts[i][1]) * f]
    }
    target -= segs[i]
  }
  return pts[pts.length - 1]
}

export const toPath = (pts: Pt[]) => pts.map(([x, y], i) => `${i ? 'L' : 'M'}${x},${y}`).join('')
