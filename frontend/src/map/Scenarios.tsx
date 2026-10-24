import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { timeOf } from '../lib/health'
import { getPlayer } from '../replay/player'
import { DEMO } from '../transport'
import type { Activity, Equipment, Modes, SimStatus } from '../types'
import type { Listener } from '../useFab'
import { BLOCK_LABEL } from './content'
import type { Highlight } from './Diagram'
import { SCENARIOS, advance, isComplete, startRun, type Run } from './scenarioDefs'

interface Props {
  tools: Equipment[]
  tool: string
  onToolChange: (id: string) => void
  modes: Modes | null
  sim: Record<string, SimStatus> | null
  activity: Activity[]
  subscribe: (fn: Listener) => () => void
  onHighlight: (h: Highlight | null) => void
}

export function Scenarios({ tools, tool: chosenTool, onToolChange, modes, sim, activity, subscribe, onHighlight }: Props) {
  const [scenarioId, setScenarioId] = useState('wafer')
  const [run, setRun] = useState<Run | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)
  const spoolSeen = useRef(false)

  const def = SCENARIOS.find((s) => s.id === (run?.scenarioId ?? scenarioId))!
  // In the recorded demo each scenario happened on one tool at one moment.
  const marker = DEMO ? getPlayer().markers.find((m) => m.scenario === def.id) : undefined
  const tool = marker?.tool ?? chosenTool
  const equipment = tools.find((t) => t.id === tool)
  let blocker = equipment ? def.blocker(equipment, modes, sim !== null) : 'Choose a tool.'
  let hint = equipment && def.hint ? def.hint(equipment, modes) : null
  if (DEMO) {
    blocker = marker ? null : 'This scenario is not in the recording.'
    hint = marker ? `Recorded on ${marker.tool}. Running it jumps the replay to that moment.` : null
  }
  const running = run !== null && !isComplete(run)

  // Feed live activity into the running scenario.
  useEffect(
    () =>
      subscribe((topic, data) => {
        if (topic !== 'activity') return
        setRun((r) => (r ? advance(SCENARIOS.find((s) => s.id === r.scenarioId)!, r, data as Activity) : r))
      }),
    [subscribe],
  )

  // Spooling happens inside the tool, so it only shows up in the simulator's status.
  const spooled = run ? (sim?.[run.tool]?.spooled ?? 0) : 0
  useEffect(() => {
    if (!run || run.scenarioId !== 'network' || spooled === 0 || spoolSeen.current) return
    spoolSeen.current = true
    const entry: Activity = {
      id: -1,
      ts: new Date().toISOString(),
      block: 'simulator',
      tool: run.tool,
      kind: 'sim.spool',
      text: `The tool is holding ${spooled} undelivered report(s) in its spool.`,
      data: {},
    }
    setRun((r) => (r ? advance(def, r, entry) : r))
  }, [run, spooled, def])

  const start = async () => {
    setError(null)
    setStarting(true)
    spoolSeen.current = false
    // Start listening before triggering, so the simulator's own entry is caught.
    setRun(startRun(def, tool))
    if (DEMO && marker) {
      const player = getPlayer()
      player.seek(marker.t - 0.3)
      player.play()
      setStarting(false)
      return
    }
    try {
      if (def.sim) await api.simAction(tool, def.sim.action, def.sim.body)
    } catch (e) {
      setRun(null)
      setError((e as Error).message)
    } finally {
      setStarting(false)
    }
  }

  const log = activity.filter((a) => a.tool === tool).slice(-30).reverse()

  return (
    <section className="scenarios" aria-labelledby="scen-title">
      <h2 id="scen-title">Run a scenario</h2>
      <p className="muted">
        Each scenario acts on the simulator only. Everything after the first step is the real system reacting, and each
        step ticks off when the corresponding block reports it.
      </p>

      <div className="scenarios__controls">
        <fieldset className="choice" disabled={running}>
          <legend>Scenario</legend>
          {SCENARIOS.map((s) => (
            <label key={s.id} className={`choice__item ${scenarioId === s.id ? 'is-on' : ''}`}>
              <input
                type="radio"
                name="scenario"
                value={s.id}
                checked={scenarioId === s.id}
                onChange={() => {
                  setScenarioId(s.id)
                  setRun(null)
                  const m = DEMO ? getPlayer().markers.find((x) => x.scenario === s.id) : undefined
                  if (m) onToolChange(m.tool)
                }}
              />
              <span className="choice__title">{s.title}</span>
              <span className="choice__text">{s.summary}</span>
            </label>
          ))}
        </fieldset>

        <div className="scenarios__run">
          <fieldset className="tools-pick" disabled={running || DEMO}>
            <legend>Tool</legend>
            {tools.map((t) => (
              <label key={t.id} className={tool === t.id ? 'is-on' : ''}>
                <input
                  type="radio"
                  name="scen-tool"
                  checked={tool === t.id}
                  onChange={() => {
                    onToolChange(t.id)
                    setRun(null)
                  }}
                />
                {t.id}
              </label>
            ))}
          </fieldset>
          <div className="scenarios__buttons">
            <button type="button" className="primary" disabled={!!blocker || running || starting} onClick={start}>
              {running ? 'Running…' : `${DEMO ? 'Replay' : 'Run'} on ${tool}`}
            </button>
            {run && (
              <button type="button" onClick={() => setRun(null)}>
                {isComplete(run) ? 'Clear' : 'Stop watching'}
              </button>
            )}
          </div>
          {(blocker || hint) && <p className="muted small">{blocker ?? hint}</p>}
          {error && (
            <p className="notice notice--error" role="alert">
              {error}
            </p>
          )}
        </div>
      </div>

      <div className="scenarios__body">
        <ol className="steps" aria-label={`${def.title} steps`}>
          {def.steps.map((step, i) => {
            const st = run?.steps[i]
            const status = st?.status ?? 'idle'
            const isNext = run && st?.status === 'pending' && run.steps.slice(0, i).every((p) => p.status !== 'pending')
            return (
              <li
                key={step.id}
                className={`step step--${status} ${isNext ? 'is-next' : ''}`}
                onMouseEnter={() => onHighlight({ block: step.block, tool })}
                onMouseLeave={() => onHighlight(null)}
              >
                <span className="step__mark" aria-hidden>
                  {status === 'done' ? '✓' : status === 'skipped' ? '–' : ''}
                </span>
                <div>
                  <div className="step__head">
                    <span className={`chip chip--${step.block}`}>{BLOCK_LABEL[step.block]}</span>
                    <span className="step__title">{step.title}</span>
                    {step.optional && <span className="muted small">may not happen</span>}
                  </div>
                  {step.note && <p className="step__note">{step.note}</p>}
                  {st?.status === 'done' && (
                    <p className="step__seen">
                      <span className="num">{timeOf(st.at ?? null)}</span> {st.text}
                    </p>
                  )}
                  {st?.status === 'skipped' && <p className="step__seen muted">Did not happen this time.</p>}
                </div>
              </li>
            )
          })}
        </ol>

        <div className="feed">
          <h3>Everything the system did for {tool}</h3>
          <ol className="feed__list">
            {log.map((a) => (
              <li key={a.id}>
                <span className="num muted">{timeOf(a.ts)}</span>
                <span className={`chip chip--${a.block}`}>{BLOCK_LABEL[a.block]}</span>
                <span>{a.text}</span>
              </li>
            ))}
          </ol>
        </div>
      </div>
    </section>
  )
}
