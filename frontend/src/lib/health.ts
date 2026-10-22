/** Same least-squares trend the backend uses, so the chart's projection line
 *  matches the RUL number the dispatcher acts on. */
export function linearFit(ys: number[]): { slope: number; intercept: number } {
  const n = ys.length
  if (n < 2) return { slope: 0, intercept: ys[0] ?? 0 }
  const mx = (n - 1) / 2
  const my = ys.reduce((a, b) => a + b, 0) / n
  let sxx = 0
  let sxy = 0
  ys.forEach((y, x) => {
    sxx += (x - mx) ** 2
    sxy += (x - mx) * (y - my)
  })
  const slope = sxy / sxx
  return { slope, intercept: my - slope * mx }
}

export const TREND_WINDOW = 8

/** Projection from the last point of `history` to the spec limit.
 *  Returns null when the tool isn't degrading or there is too little data. */
export function projectToLimit(
  history: number[],
  limit: number,
  maxAhead = 80,
): { fromX: number; fromY: number; toX: number; toY: number } | null {
  const recent = history.slice(-TREND_WINDOW)
  if (recent.length < 4) return null
  const { slope, intercept } = linearFit(recent)
  if (slope <= 1e-12) return null
  const fromY = intercept + slope * (recent.length - 1)
  const fromX = history.length - 1
  const ahead = Math.max(0, Math.min((limit - fromY) / slope, maxAhead))
  return { fromX, fromY, toX: fromX + ahead, toY: fromY + slope * ahead }
}

export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)} s`
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)
  return s ? `${m} min ${s} s` : `${m} min`
}

export function timeOf(iso: string | null): string {
  if (!iso) return ''
  return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}
