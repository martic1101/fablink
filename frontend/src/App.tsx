import { useEffect, useState } from 'react'
import { api } from './api'
import { AlarmList } from './components/AlarmList'
import { LotTable } from './components/LotTable'
import { SecsLog } from './components/SecsLog'
import { StrategyPanel } from './components/StrategyPanel'
import { ToolDetail } from './components/ToolDetail'
import { ToolPanel } from './components/ToolPanel'
import { WorkOrderList } from './components/WorkOrderList'
import { SystemMap } from './map/SystemMap'
import { ReplayBar } from './replay/ReplayBar'
import { DEMO, DEMO_WRITE_MESSAGE } from './transport'
import { useFab } from './useFab'

type Tab = 'lots' | 'secs'
type Page = 'console' | 'map'

const pageFromHash = (): Page => (location.hash.startsWith('#/map') ? 'map' : 'console')

function usePage(): Page {
  const [page, setPage] = useState<Page>(pageFromHash)
  useEffect(() => {
    const on = () => setPage(pageFromHash())
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])
  return page
}

export default function App() {
  const { state, refreshKpi, subscribe, markShutDown } = useFab()
  const page = usePage()
  const tools = Object.values(state.equipment).sort((a, b) => a.id.localeCompare(b.id))
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [tab, setTab] = useState<Tab>('lots')
  const selected = tools.find((t) => t.id === selectedId) ?? tools[0]
  const online = tools.filter((t) => t.connected).length
  const predictive = state.modes?.predictive ?? true
  const running = state.modes?.running ?? true
  const [runBusy, setRunBusy] = useState(false)
  // Only a local run can be shut down from here; the cloud deployment can't.
  const [canShutDown, setCanShutDown] = useState(false)
  // While not connected, ask the API why, so a misconfigured deployment says so.
  const [hostProblem, setHostProblem] = useState<string | null>(null)
  useEffect(() => {
    if (DEMO) return
    const check = () =>
      api
        .status()
        .then((s) => {
          setCanShutDown(Boolean(s.capabilities?.shutdown))
          setHostProblem(null)
        })
        .catch((e: Error) => {
          setCanShutDown(false)
          setHostProblem(e.message)
        })
    check()
    if (state.live) return
    const timer = window.setInterval(check, 5000)
    return () => window.clearInterval(timer)
  }, [state.live])

  const setRunning = async (value: boolean) => {
    setRunBusy(true)
    try {
      await api.setModes({ running: value })
    } finally {
      setRunBusy(false)
    }
  }

  const shutDown = async () => {
    const ok = window.confirm(
      'Shut down FabLink?\n\nThis stops the simulator and the host. To start again, run "make demo" in a terminal.',
    )
    if (!ok) return
    try {
      await api.shutdown()
      markShutDown()
    } catch (e) {
      window.alert(`Could not shut down: ${(e as Error).message}`)
    }
  }

  if (state.shutDown) {
    return (
      <div className="app">
        <main className="stopped">
          <h1>FabLink is shut down</h1>
          <p>The simulator and the host have stopped. To start them again, run this in a terminal from the project folder:</p>
          <pre>
            <code>make demo</code>
          </pre>
          <p className="muted">Then reload this page.</p>
        </main>
      </div>
    )
  }

  const setStrategy = async (value: boolean) => {
    await api.setModes({ predictive: value })
    refreshKpi()
  }

  return (
    <div className={`app ${predictive ? 'is-predictive' : 'is-reactive'}`}>
      <header className="topbar">
        <div className="brand">
          <span className="brand__name">FabLink</span>
          <nav className="nav" aria-label="Pages">
            <a href="#/" aria-current={page === 'console' ? 'page' : undefined}>
              Console
            </a>
            <a href="#/map" aria-current={page === 'map' ? 'page' : undefined}>
              System map
            </a>
          </nav>
        </div>
        <div
          className="strategy-switch"
          role="radiogroup"
          aria-label="Maintenance strategy"
          title={DEMO ? DEMO_WRITE_MESSAGE : undefined}
        >
          <button type="button" role="radio" aria-checked={predictive} disabled={DEMO} onClick={() => setStrategy(true)}>
            Predictive maintenance
          </button>
          <button type="button" role="radio" aria-checked={!predictive} disabled={DEMO} onClick={() => setStrategy(false)}>
            Run to failure
          </button>
        </div>
        {!DEMO && (
        <div className="runctl">
          <span className={`runctl__state ${state.live && running ? 'is-running' : 'is-paused'}`}>
            {!state.live ? 'Not connected' : running ? 'Running' : 'Paused'}
          </span>
          <button type="button" disabled={!state.live || runBusy} onClick={() => setRunning(!running)}>
            {running ? 'Pause simulation' : 'Start simulation'}
          </button>
        </div>
        )}
        <div className={`link ${state.live ? 'is-live' : ''}`}>
          {state.live ? `${online} of ${tools.length} tools online` : 'Reconnecting to host'}
        </div>
        {canShutDown && (
          <button type="button" className="quiet" disabled={!state.live} onClick={shutDown}>
            Shut down
          </button>
        )}
      </header>
      {DEMO && <ReplayBar />}
      {!DEMO && state.live && !running && (
        <div className="pausebar" role="status">
          <span>
            Simulation paused: no simulated time passes and nothing is dispatched. Tools stay connected.
          </span>
          <button type="button" disabled={runBusy} onClick={() => setRunning(true)}>
            Start simulation
          </button>
        </div>
      )}

      <main>
        {page === 'map' ? (
          <SystemMap state={state} subscribe={subscribe} />
        ) : tools.length === 0 ? (
          <p className="empty">
            {DEMO ? (
              'Loading the recorded demo…'
            ) : (
              <>
                Waiting for the FabLink host. Locally, start it with <code>make demo</code>; this page connects on
                its own.
                {hostProblem && (
                  <>
                    <br />
                    The API says: {hostProblem}
                  </>
                )}
              </>
            )}
          </p>
        ) : (
          <>
            <section className="bay" aria-label="Tool bay">
              {tools.map((t) => (
                <ToolPanel
                  key={t.id}
                  tool={t}
                  slots={state.slots[t.id]?.lot === t.lot_id ? state.slots[t.id]?.results : undefined}
                  availability={state.kpi?.availability[t.id]?.availability}
                  selected={t.id === selected?.id}
                  onSelect={() => setSelectedId(t.id)}
                />
              ))}
            </section>

            <div className="split">
              {selected && <ToolDetail tool={selected} trace={state.traces[selected.id] ?? []} />}
              <aside className="side">
                {state.kpi && (
                  <StrategyPanel strategies={state.kpi.strategies} active={predictive ? 'predictive' : 'reactive'} />
                )}
                <WorkOrderList orders={state.workOrders} />
                <AlarmList alarms={state.alarms} />
              </aside>
            </div>

            <section className="tabs">
              <div role="tablist" aria-label="Fab data">
                <button type="button" role="tab" aria-selected={tab === 'lots'} onClick={() => setTab('lots')}>
                  Lots
                </button>
                <button type="button" role="tab" aria-selected={tab === 'secs'} onClick={() => setTab('secs')}>
                  SECS/GEM messages
                </button>
              </div>
              <div role="tabpanel">
                {tab === 'lots' ? (
                  <LotTable lots={Object.values(state.lots)} />
                ) : (
                  <SecsLog entries={state.secs} tool={selected?.id ?? null} />
                )}
              </div>
            </section>
          </>
        )}
      </main>
    </div>
  )
}
