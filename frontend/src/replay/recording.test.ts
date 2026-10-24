import { describe, expect, it } from 'vitest'
import { countUpTo, parseRecording, Recording, type RecordingData } from './recording'

const eq = (id: string, state: string) => ({ id, state })

function fixture(): RecordingData {
  return {
    version: 1,
    recorded_at: '2026-10-24T00:00:00+00:00',
    duration: 10,
    initial: {
      '/api/equipment': [eq('ETCH01', 'IDLE'), eq('CVD01', 'IDLE')],
      '/api/equipment/ETCH01': { ...eq('ETCH01', 'IDLE'), trace: [{ ts: 'a', values: [1] }] },
      '/api/lots?limit=80': [{ id: 'L1', status: 'QUEUED', created_at: '2026-01-01' }],
      '/api/activity?limit=300': [{ id: 1, kind: 'old' }],
      '/api/secs-log?limit=120': [],
      '/api/modes': { running: true },
    },
    rest: { '/api/kpi': [[0, { v: 'k0' }], [5, { v: 'k5' }]] },
    ws: [
      [1, 'equipment', eq('ETCH01', 'PROCESSING')],
      [1.5, 'trace', { tool: 'ETCH01', ts: 'b', values: [2] }],
      [2, 'lot', { id: 'L1', status: 'RUNNING', created_at: '2026-01-01' }],
      [3, 'lot', { id: 'L2', status: 'QUEUED', created_at: '2026-01-02' }],
      [4, 'activity', { id: 2, kind: 'new' }],
      [6, 'modes', { running: false }],
    ],
    markers: [{ scenario: 'fault', tool: 'ETCH01', t: 4, end: 9, complete: true }],
  }
}

describe('countUpTo', () => {
  it('counts entries at or before a time', () => {
    const t = [1, 2, 2, 5]
    expect(countUpTo(t, 0)).toBe(0)
    expect(countUpTo(t, 2)).toBe(3)
    expect(countUpTo(t, 9)).toBe(4)
  })
})

describe('Recording queries are answered as of the playback time', () => {
  const rec = new Recording(fixture())

  it('equipment: initial snapshot, then the latest pushed state', () => {
    expect(rec.equipmentAt('ETCH01', 0.5).state).toBe('IDLE')
    expect(rec.equipmentAt('ETCH01', 1).state).toBe('PROCESSING')
    expect(rec.get('/api/equipment', 2)).toEqual([eq('ETCH01', 'PROCESSING'), eq('CVD01', 'IDLE')])
  })

  it('equipment detail includes the trace up to that time', () => {
    const at = (t: number) => (rec.get('/api/equipment/ETCH01', t) as { trace: unknown[] }).trace.length
    expect(at(1)).toBe(1)
    expect(at(2)).toBe(2)
  })

  it('lots are rebuilt from lot messages, newest first', () => {
    expect(rec.lotsAt(1)).toEqual([{ id: 'L1', status: 'QUEUED', created_at: '2026-01-01' }])
    expect(rec.lotsAt(3).map((l) => [l.id, l.status])).toEqual([
      ['L2', 'QUEUED'],
      ['L1', 'RUNNING'],
    ])
  })

  it('activity is the initial log plus messages so far', () => {
    expect((rec.get('/api/activity?limit=300', 3) as unknown[]).length).toBe(1)
    expect((rec.get('/api/activity?limit=300', 4) as unknown[]).length).toBe(2)
  })

  it('polled endpoints return the last recorded response', () => {
    expect(rec.get('/api/kpi', 4.9)).toEqual({ v: 'k0' })
    expect(rec.get('/api/kpi', 5)).toEqual({ v: 'k5' })
    expect(rec.get('/api/nope', 5)).toBeUndefined()
  })

  it('modes follow mode messages', () => {
    expect(rec.modesAt(5)).toEqual({ running: true })
    expect(rec.modesAt(6)).toEqual({ running: false })
  })

  it('finds the message cursor for a seek', () => {
    expect(rec.cursorAt(0)).toBe(0)
    expect(rec.cursorAt(2)).toBe(3)
  })
})

describe('parseRecording', () => {
  it('reads gzip and plain JSON', async () => {
    const json = JSON.stringify(fixture())
    const gz = await new Response(new Blob([json]).stream().pipeThrough(new CompressionStream('gzip'))).arrayBuffer()
    const fromGz = await parseRecording(gz)
    const fromPlain = await parseRecording(new TextEncoder().encode(json).buffer as ArrayBuffer)
    expect(fromGz.markers).toEqual(fromPlain.markers)
    expect(fromGz.duration).toBe(10)
  })
})
