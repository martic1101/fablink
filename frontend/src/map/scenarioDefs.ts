import type { Activity, Block, Equipment, Modes } from '../types'

export interface StepDef {
  id: string
  block: Block
  title: string
  /** why it happens, or what is simplified compared to a real fab */
  note?: string
  /** optional steps may never happen (e.g. nothing to spool while idle) */
  optional?: boolean
  match: (e: Activity) => boolean
}

export type SimAction =
  | { action: 'wear'; body: { wear: number } }
  | { action: 'degrade'; body: { multiplier: number } }
  | { action: 'disconnect'; body: { seconds: number } }
  | { action: 'lose_reply'; body: Record<string, never> }

export interface ScenarioDef {
  id: string
  title: string
  summary: string
  sim?: SimAction
  /** returns a reason the scenario can't run right now, or null */
  blocker: (tool: Equipment, modes: Modes | null, simAvailable: boolean) => string | null
  /** extra guidance that doesn't block the run */
  hint?: (tool: Equipment, modes: Modes | null) => string | null
  steps: StepDef[]
}

const kind = (k: string) => (e: Activity) => e.kind === k
const state = (s: string) => (e: Activity) => e.kind === 'state' && e.data.state === s
const control = (s: string) => (e: Activity) => e.kind === 'control' && e.data.state === s
const health = (...st: string[]) => (e: Activity) =>
  e.kind === 'analytics.health' && st.includes(String(e.data.status))

const needsLink = (t: Equipment) => (t.connected ? null : `${t.id} is offline.`)
const needsSim = (ok: boolean) => (ok ? null : 'Simulator control is not reachable, so this scenario cannot be triggered.')
const needsRunnable = (t: Equipment) =>
  t.state === 'IDLE' || t.state === 'PROCESSING' ? null : `${t.id} is in ${t.state}; wait until it is idle or processing.`

export const SCENARIOS: ScenarioDef[] = [
  {
    id: 'wafer',
    title: 'Read one wafer',
    summary: 'Follow a single wafer from the tool’s first report to the browser.',
    blocker: (t) => needsLink(t),
    hint: (t) => (t.state === 'PROCESSING' ? null : 'Waits for the next wafer; the dispatcher loads a lot when one is queued.'),
    steps: [
      {
        id: 'start',
        block: 'host',
        title: 'Tool reports S6F11 WAFER_STARTED',
        note: 'A collection event: the tool tells the host something happened. The host replies S6F12.',
        match: kind('wafer.started'),
      },
      {
        id: 'trace',
        block: 'host',
        title: 'Tool streams S6F1 trace data',
        note: 'Trace values arrive as a bare list of numbers. The host knows which number is which sensor because it asked with S1F11 when it connected.',
        match: kind('trace.first'),
      },
      {
        id: 'done',
        block: 'host',
        title: 'Tool reports S6F11 WAFER_COMPLETED',
        match: kind('wafer.completed'),
      },
      {
        id: 'feature',
        block: 'analytics',
        title: 'Trace reduced to one number per wafer',
        note: 'The mean of the health sensor while the wafer was in the chamber (an FDC summary statistic).',
        match: kind('analytics.feature'),
      },
      {
        id: 'health',
        block: 'analytics',
        title: 'Health model updated',
        note: 'EWMA, SPC rules and the estimated wafers left before the spec limit.',
        match: kind('analytics.health'),
      },
      { id: 'db', block: 'database', title: 'Row written to wafer_results', match: kind('db.wafer') },
      { id: 'ws', block: 'api', title: 'Pushed to the browser over WebSocket', match: kind('ws.pushed') },
    ],
  },
  {
    id: 'drift',
    title: 'Wear speeds up',
    summary: 'The part starts wearing 3× faster. Can the host spot it from sensor data alone?',
    sim: { action: 'degrade', body: { multiplier: 3 } },
    blocker: (t, _m, sim) => needsLink(t) ?? needsSim(sim) ?? needsRunnable(t),
    hint: (_t, m) =>
      m?.predictive === false
        ? 'Run to failure is selected, so the dispatcher will not step in and the tool will eventually break.'
        : null,
    steps: [
      {
        id: 'inject',
        block: 'simulator',
        title: 'Simulator: wear rate ×3',
        note: 'Hidden from the host, like a bad batch of parts would be.',
        match: kind('sim.degrade'),
      },
      {
        id: 'watch',
        block: 'analytics',
        title: 'Trend picks up: status WATCH',
        note: 'The line through the last 8 wafers gets steeper, so the estimated wafers left drops.',
        optional: true,
        match: health('WATCH'),
      },
      {
        id: 'pmreq',
        block: 'analytics',
        title: 'PM recommended',
        note: 'Predicted to cross the spec limit within the next lot plus a 3-wafer margin.',
        match: health('PM_REQUIRED'),
      },
      {
        id: 'wo',
        block: 'dispatcher',
        title: 'PM work order raised, tool held',
        note: 'No new lots go to the tool; the lot already running finishes.',
        match: (e) => e.kind === 'wo.opened' && e.data.kind === 'PM',
      },
      {
        id: 'sent',
        block: 'dispatcher',
        title: 'Work order handed to the technicians',
        match: kind('wo.sent'),
      },
      {
        id: 'local',
        block: 'crew',
        title: 'Technician takes the tool: ONLINE LOCAL',
        note: 'Once the lot is done. The tool reports the control state change itself, and refuses remote commands while local.',
        match: control('ONLINE_LOCAL'),
      },
      {
        id: 'started',
        block: 'host',
        title: 'Tool reports MAINT_STARTED',
        match: kind('maint.started'),
      },
      {
        id: 'maint',
        block: 'host',
        title: 'PM done: work order closed, health model reset',
        match: (e) => e.kind === 'maint.completed' && e.data.kind === 'PM',
      },
    ],
  },
  {
    id: 'fault',
    title: 'Part fails suddenly',
    summary: 'The part is one wafer from failure, with no trend that would warn about it.',
    sim: { action: 'wear', body: { wear: 0.995 } },
    blocker: (t, _m, sim) => needsLink(t) ?? needsSim(sim) ?? needsRunnable(t),
    steps: [
      {
        id: 'inject',
        block: 'simulator',
        title: 'Simulator: wear set to 99.5%',
        note: 'A sudden failure. Predictive maintenance cannot catch everything, so the reactive path has to work too.',
        match: kind('sim.wear'),
      },
      {
        id: 'alarm',
        block: 'host',
        title: 'Tool raises S5F1 alarm',
        note: 'Stored in the alarms table and shown on the Console.',
        match: (e) => e.kind === 'alarm.set' && Number(e.data.severity) <= 3,
      },
      {
        id: 'hold',
        block: 'mes',
        title: 'Lot aborted and put on HOLD',
        note: 'An engineer later decides what happens to the unprocessed wafers.',
        match: kind('lot.hold'),
      },
      { id: 'down', block: 'host', title: 'Tool reports DOWN', match: state('DOWN') },
      {
        id: 'wo',
        block: 'dispatcher',
        title: 'Repair work order raised',
        note: 'The tool is held until the work order is closed.',
        match: (e) => e.kind === 'wo.opened' && e.data.kind === 'REPAIR',
      },
      {
        id: 'repair',
        block: 'database',
        title: 'Downtime counted from the failure',
        note: 'Not from when the technician arrives: travel time is part of the cost of a failure.',
        match: kind('repair.opened'),
      },
      { id: 'sent', block: 'dispatcher', title: 'Work order handed to the technicians', match: kind('wo.sent') },
      {
        id: 'local',
        block: 'crew',
        title: 'Technician arrives: ONLINE LOCAL',
        note: 'The repair happens at the tool, not over SECS. The tool reports what is going on.',
        match: control('ONLINE_LOCAL'),
      },
      { id: 'started', block: 'host', title: 'Tool reports MAINT_STARTED', match: kind('maint.started') },
      { id: 'clear', block: 'host', title: 'Alarm cleared when the part is replaced', match: kind('alarm.cleared') },
      {
        id: 'done',
        block: 'host',
        title: 'Repair done: work order closed',
        match: kind('maint.completed'),
      },
      {
        id: 'back',
        block: 'dispatcher',
        title: 'Back in ONLINE REMOTE and IDLE: lots resume',
        note: 'The held lot still waits for an engineer to release it on the Console.',
        match: state('IDLE'),
      },
    ],
  },
  {
    id: 'network',
    title: 'Network drops',
    summary: 'Cut the link to the tool for 8 seconds. Nothing should be lost.',
    sim: { action: 'disconnect', body: { seconds: 8 } },
    blocker: (t, _m, sim) => needsLink(t) ?? needsSim(sim),
    hint: (t) =>
      t.state === 'PROCESSING' ? null : 'Tip: run this while the tool is processing so it has reports to spool.',
    steps: [
      {
        id: 'cut',
        block: 'simulator',
        title: 'Simulator: link cut for 8 s',
        match: kind('sim.disconnect'),
      },
      {
        id: 'down',
        block: 'gateway',
        title: 'Gateway sees the session close',
        note: 'Tool shown OFFLINE; an OFFLINE interval goes into state_history.',
        match: kind('link.down'),
      },
      {
        id: 'retry',
        block: 'gateway',
        title: 'Gateway retries with backoff',
        note: '0.5 s, 1 s, 2 s, 4 s … up to 10 s, so a dead tool is not hammered.',
        match: kind('link.retry'),
      },
      {
        id: 'spool',
        block: 'simulator',
        title: 'Tool spools reports it cannot deliver',
        note: 'GEM spooling: the tool keeps events and alarms until the host is back.',
        optional: true,
        match: kind('sim.spool'),
      },
      {
        id: 'up',
        block: 'gateway',
        title: 'Link back: Select, S1F13, S1F11',
        note: 'Same startup sequence as the first connection.',
        match: kind('link.up'),
      },
      {
        id: 'replay',
        block: 'host',
        title: 'Spooled reports replayed',
        note: 'The tool holds them until the host asks with S6F23, then sends them in order.',
        match: kind('spool.replayed'),
      },
    ],
  },
  {
    id: 'reply',
    title: 'Reply gets lost',
    summary: 'The host’s acknowledgement of one report never reaches the tool.',
    sim: { action: 'lose_reply', body: {} },
    blocker: (t, _m, sim) => needsLink(t) ?? needsSim(sim),
    hint: (t) => (t.state === 'PROCESSING' ? null : 'Affects the next report the tool sends; run it while processing.'),
    steps: [
      {
        id: 'inject',
        block: 'simulator',
        title: 'Simulator: next reply will be lost',
        match: kind('sim.lose_reply'),
      },
      {
        id: 'dup',
        block: 'host',
        title: 'Resent report recognised and ignored',
        note: 'The tool timed out (T3) and sent the report again, as it should: delivery is at-least-once. The host had already processed it, recognises the DATAID, acknowledges and does not apply it twice.',
        match: kind('event.duplicate'),
      },
    ],
  },
]

export type StepStatus = 'pending' | 'done' | 'skipped'

export interface Run {
  scenarioId: string
  tool: string
  steps: { status: StepStatus; at?: string; text?: string }[]
}

export function startRun(def: ScenarioDef, tool: string): Run {
  return { scenarioId: def.id, tool, steps: def.steps.map(() => ({ status: 'pending' })) }
}

export const isComplete = (run: Run) => run.steps.every((s) => s.status !== 'pending')

/** Feed one activity entry. Steps complete in order; an optional step that is
 *  still pending when a later step matches is marked skipped. */
export function advance(def: ScenarioDef, run: Run, e: Activity): Run {
  if (e.tool !== run.tool) return run
  const steps = run.steps.slice()
  for (let i = 0; i < def.steps.length; i++) {
    if (steps[i].status !== 'pending') continue
    if (def.steps[i].match(e)) {
      for (let j = 0; j < i; j++) if (steps[j].status === 'pending') steps[j] = { status: 'skipped' }
      steps[i] = { status: 'done', at: e.ts, text: e.text }
      return { ...run, steps }
    }
    if (!def.steps[i].optional) break // can't look past a required pending step
  }
  return run
}
