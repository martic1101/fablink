import { describe, expect, it } from 'vitest'
import { formatDuration, linearFit, projectToLimit } from './health'

describe('linearFit', () => {
  it('recovers an exact line', () => {
    const { slope, intercept } = linearFit([1, 3, 5, 7])
    expect(slope).toBeCloseTo(2)
    expect(intercept).toBeCloseTo(1)
  })
})

describe('projectToLimit', () => {
  it('extends the recent trend to the spec limit', () => {
    const history = Array.from({ length: 10 }, (_, i) => 20 + i) // 20..29, slope 1
    const p = projectToLimit(history, 40)!
    expect(p.fromX).toBe(9)
    expect(p.fromY).toBeCloseTo(29)
    expect(p.toX).toBeCloseTo(20) // 11 wafers to go
    expect(p.toY).toBeCloseTo(40)
  })

  it('returns null for a flat or improving signal', () => {
    expect(projectToLimit([5, 5, 5, 5, 5], 10)).toBeNull()
    expect(projectToLimit([9, 8, 7, 6, 5], 10)).toBeNull()
  })

  it('needs at least four wafers', () => {
    expect(projectToLimit([1, 2, 3], 10)).toBeNull()
  })

  it('caps how far ahead it draws', () => {
    const p = projectToLimit([1, 1.001, 1.002, 1.003], 100, 50)!
    expect(p.toX - p.fromX).toBe(50)
  })
})

describe('formatDuration', () => {
  it('formats seconds and minutes', () => {
    expect(formatDuration(42)).toBe('42 s')
    expect(formatDuration(120)).toBe('2 min')
    expect(formatDuration(75)).toBe('1 min 15 s')
  })
})
