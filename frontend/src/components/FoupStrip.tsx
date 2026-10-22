/** A FOUP carries up to 25 wafers; one tick per slot, coloured by outcome. */
export function FoupStrip({
  wafers,
  current,
  results = {},
}: {
  wafers: number
  current: number
  results?: Record<number, 'OK' | 'SCRAP'>
}) {
  const done = Object.keys(results).length
  const scrapped = Object.values(results).filter((r) => r === 'SCRAP').length
  return (
    <span
      className="foup"
      role="img"
      aria-label={wafers ? `${done} of ${wafers} wafers processed, ${scrapped} scrapped` : 'Carrier empty'}
    >
      {Array.from({ length: 25 }, (_, i) => {
        const slot = i + 1
        let cls = 'foup__slot'
        if (slot > wafers) cls += ' is-empty'
        else if (results[slot] === 'SCRAP') cls += ' is-scrap'
        else if (results[slot] === 'OK') cls += ' is-done'
        else if (slot === current) cls += ' is-current'
        return <span key={slot} className={cls} />
      })}
    </span>
  )
}
