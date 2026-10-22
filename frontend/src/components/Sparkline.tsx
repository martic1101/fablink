export function Sparkline({ values, highlight }: { values: number[]; highlight?: boolean }) {
  const W = 160
  const H = 36
  if (values.length < 2) return <svg className="spark" viewBox={`0 0 ${W} ${H}`} aria-hidden />
  const lo = Math.min(...values)
  const hi = Math.max(...values)
  const span = hi - lo || 1
  const d = values
    .map((v, i) => {
      const x = (i / (values.length - 1)) * W
      const y = H - 3 - ((v - lo) / span) * (H - 6)
      return `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join('')
  return (
    <svg className={`spark${highlight ? ' spark--hl' : ''}`} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden>
      <path d={d} />
    </svg>
  )
}
