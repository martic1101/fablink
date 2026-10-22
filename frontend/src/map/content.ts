import type { Block } from '../types'

export interface ToolTypeInfo {
  name: string
  short: string
  what: string
  wears: string
  alarm: string
}

export const TOOL_TYPES: Record<string, ToolTypeInfo> = {
  ETCH: {
    name: 'Plasma etcher',
    short: 'plasma etch',
    what: 'Removes material from the wafer with a plasma. RF power drives the plasma, and a matching network keeps the power that bounces back (reflected power) low.',
    wears: 'The RF matching network wears with every wafer, so reflected power creeps up. Past the spec limit wafers start to be scrapped; at the end of its life the network fails.',
    alarm: 'Alarm 1001: RF reflected power high',
  },
  CVD: {
    name: 'Chemical vapour deposition',
    short: 'film deposition',
    what: 'Grows a thin film on the wafer from gases (here silane, SiH4) at about 400 °C under vacuum.',
    wears: 'The vacuum pump degrades, so the foreline pressure between chamber and pump rises. Eventually the pump fails.',
    alarm: 'Alarm 2001: foreline pressure high',
  },
}

export const SENSOR_INFO: Record<string, string> = {
  RF_FWD_POWER: 'Power delivered to the plasma',
  RF_REFL_POWER: 'Power reflected back by an imperfect match. Health indicator.',
  CHAMBER_PRESSURE: 'Process pressure in the chamber',
  ESC_TEMP: 'Electrostatic chuck temperature (the chuck holds the wafer)',
  HEATER_TEMP: 'Wafer heater temperature',
  FORELINE_PRESSURE: 'Pressure between chamber and vacuum pump. Health indicator.',
  SIH4_FLOW: 'Silane gas flow',
}

export interface BlockInfo {
  title: string
  short: string
  source: string
  does: string[]
  note?: string
}

export const BLOCKS: Record<Exclude<Block, 'simulator'>, BlockInfo> = {
  gateway: {
    title: 'Equipment gateway',
    short: 'One HSMS session per tool',
    source: 'backend/app/equipment/gateway.py, backend/app/secs/',
    does: [
      'Connects to each tool over TCP. The host is the active side; tools listen, as real equipment does.',
      'Startup: HSMS Select, S1F13 establish communications, S1F11 to learn the name and unit of every status variable, then S6F23 to ask for anything the tool spooled while the link was down.',
      'Keeps the link alive with a linktest every 10 s and reconnects with exponential backoff.',
      'Sends remote commands (S2F41 START and ABORT) and gives up after the T3 reply timeout (5 s) instead of hanging.',
    ],
  },
  host: {
    title: 'FabHost',
    short: 'Turns tool messages into fab state',
    source: 'backend/app/host.py',
    does: [
      'Receives every primary message from every tool and acknowledges it (S6F12, S5F2).',
      'S6F11 events update lot tracking, E10-style state history and maintenance records.',
      'S6F1 trace data is collected per wafer for the health sensor.',
      'S5F1 alarms go to the alarms table.',
      'Drops reports the tool resends after a lost reply (same DATAID), so nothing is counted twice.',
      'Tracks the GEM control state: while a tool is in ONLINE LOCAL, someone is working at it.',
      'After a restart, rebuilds each health model from the database.',
    ],
  },
  analytics: {
    title: 'Analytics',
    short: 'SPC, health score, wafers left',
    source: 'backend/app/analytics/',
    does: [
      'Reduces each wafer’s trace to one feature: the mean of the health sensor.',
      'SPC: control limits from the 8 wafers after a maintenance, checked with the Western Electric rules.',
      'Health score from an EWMA of the feature.',
      'Wafers left before the spec limit, from a straight line through the last 8 wafers.',
    ],
    note: 'The host never sees the simulator’s wear value. Everything here is inferred from sensor data.',
  },
  dispatcher: {
    title: 'Dispatcher',
    short: 'Lots and work orders',
    source: 'backend/app/host.py (dispatch loop, work orders)',
    does: [
      'Every second, picks the next lot for each idle tool: hot lots first, then oldest.',
      'Asks the health model first. If the lot would push the tool past its spec limit, it raises a PM work order instead.',
      'Raises a repair work order the moment a tool fails.',
      'Holds every tool with an open work order or in local control: no new lots.',
      'Hands open work orders to the technicians.',
    ],
    note: 'The host never performs maintenance. It raises a work order, stops dispatching to the tool and waits for the tool to report MAINT_STARTED and MAINT_COMPLETED. In a fab, work orders go to a maintenance system and people; here they go to a simulated crew.',
  },
  mes: {
    title: 'MES-lite',
    short: 'Lot tracking and holds',
    source: 'backend/app/mes/service.py',
    does: [
      'Lot lifecycle: QUEUED, DISPATCHED, RUNNING, COMPLETED.',
      'A lot aborted by a tool goes on HOLD with the alarm text as the reason.',
      'Releasing a held lot requeues only the wafers that were not processed.',
    ],
  },
  database: {
    title: 'Database',
    short: 'SQLite here, PostgreSQL in Docker',
    source: 'backend/app/models.py',
    does: [
      'One row per wafer (wafer_results) with its health feature, not raw trace data.',
      'Alarms, maintenance records and E10-style state intervals for availability.',
      'Every collection event in event_log for traceability.',
    ],
  },
  api: {
    title: 'REST + WebSocket',
    short: 'FastAPI',
    source: 'backend/app/api/',
    does: [
      'REST endpoints for equipment, lots, alarms, KPIs and this map.',
      'One WebSocket that pushes every change to the browser.',
      'Proxies the simulator’s scenario controls (demo only).',
    ],
  },
  crew: {
    title: 'Technicians',
    short: 'Simulated maintenance crew',
    source: 'backend/simulator/crew.py',
    does: [
      'Receive work orders from the host.',
      'Walk to the tool (24 simulated seconds).',
      'For a PM, wait until the current lot finishes; for a repair the tool is already down.',
      'Switch the tool to ONLINE LOCAL and do the work there. The tool itself reports MAINT_STARTED and MAINT_COMPLETED over SECS.',
      'Hand the tool back in ONLINE REMOTE.',
    ],
    note: 'Simulated. The crew lives in the simulator process, not in FabLink.',
  },
  console: {
    title: 'Browser',
    short: 'React console',
    source: 'frontend/src/',
    does: ['Console: tool bay, health trends, lots and the SECS message log.', 'System map: this page.'],
  },
}

export const BLOCK_LABEL: Record<Block, string> = {
  simulator: 'Simulator',
  gateway: 'Gateway',
  host: 'Host',
  analytics: 'Analytics',
  dispatcher: 'Dispatcher',
  mes: 'MES',
  database: 'Database',
  api: 'API',
  crew: 'Technicians',
  console: 'Browser',
}
