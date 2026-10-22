import { describe, expect, it } from 'vitest'
import type { Activity } from '../types'
import { parseFrame } from './frame'
import { pointAt } from './layout'
import { SCENARIOS, advance, isComplete, startRun } from './scenarioDefs'

let id = 0
const act = (kind: string, data: Record<string, unknown> = {}, tool = 'ETCH01'): Activity => ({
  id: ++id,
  ts: new Date().toISOString(),
  block: 'host',
  tool,
  kind,
  text: kind,
  data,
})

const byId = (sid: string) => SCENARIOS.find((s) => s.id === sid)!

describe('scenario steps', () => {
  it('completes the wafer scenario in order and ignores other tools', () => {
    const def = byId('wafer')
    let run = startRun(def, 'ETCH01')
    run = advance(def, run, act('wafer.started', {}, 'CVD01'))
    expect(run.steps[0].status).toBe('pending')
    for (const k of ['wafer.started', 'trace.first', 'wafer.completed', 'analytics.feature', 'analytics.health', 'db.wafer', 'ws.pushed'])
      run = advance(def, run, act(k))
    expect(isComplete(run)).toBe(true)
  })

  it('does not jump past a required step', () => {
    const def = byId('fault')
    let run = startRun(def, 'ETCH01')
    // An IDLE from a lot finishing before the failure must not tick the last step.
    run = advance(def, run, act('state', { state: 'IDLE' }))
    expect(run.steps.every((s) => s.status === 'pending')).toBe(true)
  })

  it('ignores nuisance alarms while waiting for the fault alarm', () => {
    const def = byId('fault')
    let run = startRun(def, 'ETCH01')
    run = advance(def, run, act('sim.wear'))
    run = advance(def, run, act('alarm.set', { severity: 7 }))
    expect(run.steps[1].status).toBe('pending')
    run = advance(def, run, act('alarm.set', { severity: 2 }))
    expect(run.steps[1].status).toBe('done')
  })

  it('skips an optional step when a later one happens', () => {
    const def = byId('network')
    let run = startRun(def, 'ETCH01')
    for (const k of ['sim.disconnect', 'link.down', 'link.retry', 'link.up']) run = advance(def, run, act(k))
    expect(run.steps.map((s) => s.status)).toEqual(['done', 'done', 'done', 'skipped', 'done', 'pending'])
  })
})

describe('parseFrame', () => {
  it('reads the HSMS header fields', () => {
    // length 12, session 0, S6F1 no W-bit, PType 0, SType 0, system 42, body <L[0]>
    const f = parseFrame('0000000c' + '0000' + '0601' + '0000' + '0000002a' + '0100')!
    expect(f).toMatchObject({ length: 12, sessionId: 0, stream: 6, func: 1, wBit: false, system: 42 })
    expect(f.parts[f.parts.length - 1].bytes).toEqual(['01', '00'])
  })

  it('reads the W-bit', () => {
    expect(parseFrame('0000000a0000862900000000000001')!.wBit).toBe(true)
  })

  it('rejects short input', () => {
    expect(parseFrame('0000')).toBeNull()
  })
})

describe('pointAt', () => {
  it('walks along a polyline by length', () => {
    const pts: [number, number][] = [
      [0, 0],
      [10, 0],
      [10, 10],
    ]
    expect(pointAt(pts, 0)).toEqual([0, 0])
    expect(pointAt(pts, 0.25)).toEqual([5, 0])
    expect(pointAt(pts, 0.75)).toEqual([10, 5])
    expect(pointAt(pts, 1)).toEqual([10, 10])
  })
})

describe('maintenance happens at the tool', () => {
  it('walks the failure scenario through the work-order flow', () => {
    const def = SCENARIOS.find((s) => s.id === 'fault')!
    let run = startRun(def, 'ETCH01')
    const feed: [string, Record<string, unknown>][] = [
      ['sim.wear', {}],
      ['alarm.set', { severity: 2 }],
      ['lot.hold', {}],
      ['state', { state: 'DOWN' }],
      ['wo.opened', { kind: 'REPAIR' }],
      ['repair.opened', {}],
      ['wo.sent', {}],
      ['control', { state: 'ONLINE_LOCAL' }],
      ['state', { state: 'DOWN' }], // "REPAIR in progress": must not confuse anything
      ['maint.started', {}],
      ['alarm.cleared', {}],
      ['maint.completed', {}],
      ['control', { state: 'ONLINE_REMOTE' }],
      ['state', { state: 'IDLE' }],
    ]
    for (const [k, d] of feed) run = advance(def, run, act(k, d))
    expect(isComplete(run)).toBe(true)
    expect(run.steps.every((s) => s.status === 'done')).toBe(true)
  })
})
