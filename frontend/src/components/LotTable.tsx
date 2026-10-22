import { useState, type FormEvent } from 'react'
import { api } from '../api'
import type { Lot } from '../types'

const ORDER: Record<string, number> = { HOLD: 0, RUNNING: 1, DISPATCHED: 2, QUEUED: 3, COMPLETED: 4 }
const STATUS_LABEL: Record<string, string> = {
  HOLD: 'On hold',
  RUNNING: 'Running',
  DISPATCHED: 'Dispatched',
  QUEUED: 'Queued',
  COMPLETED: 'Completed',
}

export function LotTable({ lots }: { lots: Lot[] }) {
  const [error, setError] = useState<string | null>(null)
  const sorted = [...lots]
    .sort((a, b) => ORDER[a.status] - ORDER[b.status] || a.priority - b.priority || (b.created_at ?? '').localeCompare(a.created_at ?? ''))
    .slice(0, 40)

  const release = async (id: string) => {
    setError(null)
    try {
      await api.releaseLot(id)
    } catch (e) {
      setError((e as Error).message)
    }
  }

  return (
    <div className="lots">
      <NewLotForm onError={setError} />
      {error && (
        <p className="notice notice--error" role="alert">
          {error}
        </p>
      )}
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th scope="col">Lot</th>
              <th scope="col">Product</th>
              <th scope="col">Recipe</th>
              <th scope="col">Status</th>
              <th scope="col">Tool</th>
              <th scope="col" className="num">
                Wafers
              </th>
              <th scope="col" className="num">
                Scrap
              </th>
              <th scope="col" />
            </tr>
          </thead>
          <tbody>
            {sorted.map((l) => (
              <tr key={l.id} className={`lot--${l.status.toLowerCase()}`}>
                <td>
                  {l.id}
                  {l.priority <= 2 && <span className="hot"> hot</span>}
                </td>
                <td>{l.product}</td>
                <td>{l.recipe}</td>
                <td>
                  <span className="lot__status">{STATUS_LABEL[l.status]}</span>
                  {l.hold_reason && <span className="lot__reason">{l.hold_reason}</span>}
                </td>
                <td>{l.equipment_id ?? '–'}</td>
                <td className="num">
                  {l.wafers_done}/{l.wafers}
                </td>
                <td className="num">{l.wafers_scrapped || ''}</td>
                <td>
                  {l.status === 'HOLD' && (
                    <button type="button" className="small" onClick={() => release(l.id)}>
                      Release remaining wafers
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function NewLotForm({ onError }: { onError: (m: string | null) => void }) {
  const [toolType, setToolType] = useState('ETCH')
  const [wafers, setWafers] = useState(25)
  const [hot, setHot] = useState(true)
  const [busy, setBusy] = useState(false)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    onError(null)
    try {
      await api.createLot({ tool_type: toolType, wafers, priority: hot ? 1 : 5 })
    } catch (err) {
      onError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="newlot" onSubmit={submit}>
      <label>
        Process
        <select value={toolType} onChange={(e) => setToolType(e.target.value)}>
          <option value="ETCH">Etch</option>
          <option value="CVD">CVD</option>
        </select>
      </label>
      <label>
        Wafers
        <input type="number" min={1} max={25} value={wafers} onChange={(e) => setWafers(Number(e.target.value))} />
      </label>
      <label className="check">
        <input type="checkbox" checked={hot} onChange={(e) => setHot(e.target.checked)} />
        Hot lot (dispatch first)
      </label>
      <button type="submit" disabled={busy}>
        Release lot to the line
      </button>
    </form>
  )
}
