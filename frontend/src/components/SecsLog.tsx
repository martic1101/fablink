import { useState } from 'react'
import type { SecsEntry } from '../types'
import { timeOf } from '../lib/health'

const NAMES: Record<string, string> = {
  S1F1: 'Are you there',
  S1F2: 'On line data',
  S1F11: 'Status variable namelist request',
  S1F12: 'Status variable namelist',
  S1F13: 'Establish communications',
  S1F14: 'Establish communications ack',
  S2F41: 'Host command',
  S2F42: 'Host command ack',
  S5F1: 'Alarm report',
  S5F2: 'Alarm ack',
  S6F11: 'Event report',
  S6F12: 'Event report ack',
}

export function SecsLog({ entries, tool }: { entries: SecsEntry[]; tool: string | null }) {
  const [onlyTool, setOnlyTool] = useState(false)
  const [open, setOpen] = useState<string | null>(null)
  const rows = entries.filter((e) => !onlyTool || e.tool === tool).slice(-80).reverse()

  return (
    <div className="secs">
      <label className="check">
        <input type="checkbox" checked={onlyTool} onChange={(e) => setOnlyTool(e.target.checked)} disabled={!tool} />
        Only {tool ?? 'selected tool'}
      </label>
      <ol>
        {rows.map((e) => {
          const key = `${e.ts}${e.tool}${e.dir}${e.sf}`
          return (
            <li key={key}>
              <button type="button" className="secs__row" aria-expanded={open === key} onClick={() => setOpen(open === key ? null : key)}>
                <span className="num muted">{timeOf(e.ts)}</span>
                <span>{e.tool}</span>
                <span className={`secs__dir secs__dir--${e.dir}`}>{e.dir === 'in' ? 'tool to host' : 'host to tool'}</span>
                <span className="secs__sf">{e.sf}</span>
                <span className="muted">{NAMES[e.sf] ?? ''}</span>
              </button>
              {open === key && <pre className="secs__sml">{e.sml}</pre>}
            </li>
          )
        })}
      </ol>
    </div>
  )
}
