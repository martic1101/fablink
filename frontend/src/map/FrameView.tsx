import { useEffect, useState } from 'react'
import { api } from '../api'
import type { Frame } from '../types'
import { parseFrame } from './frame'

const PART_LABEL = {
  length: 'Message length',
  session: 'Session ID',
  sf: 'Stream / function',
  type: 'PType / SType',
  system: 'System bytes',
  body: 'SECS-II body',
} as const

const BODY_SHOWN = 48

/** How the host reads one sensor sample: bytes → SECS-II items → named values. */
export function FrameView({ tool }: { tool: string }) {
  const [frame, setFrame] = useState<Frame | null>(null)
  const [paused, setPaused] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setFrame(null)
    setError(null)
  }, [tool])

  useEffect(() => {
    if (paused) return
    let alive = true
    const load = () =>
      api.frame(tool).then(
        (f) => alive && (setFrame(f), setError(null)),
        (e: Error) => alive && setError(e.message),
      )
    load()
    const t = window.setInterval(load, 1500)
    return () => {
      alive = false
      window.clearInterval(t)
    }
  }, [tool, paused])

  const f = frame ? parseFrame(frame.hex) : null

  return (
    <section className="frame" aria-labelledby="frame-title">
      <div className="frame__head">
        <h3 id="frame-title">How the host reads a sensor sample</h3>
        <button type="button" className="small" onClick={() => setPaused((p) => !p)}>
          {paused ? 'Follow live' : 'Pause'}
        </button>
      </div>
      {!frame && <p className="muted">{error ?? 'Waiting for the next S6F1 trace message.'}</p>}
      {frame && f && (
        <ol className="frame__steps">
          <li>
            <h4>Bytes arrive on the TCP socket</h4>
            <p className="muted">
              Every HSMS message starts with a 4-byte length and a 10-byte header, then the SECS-II body.
            </p>
            <div className="hex" role="img" aria-label={`Raw frame, ${frame.hex.length / 2} bytes`}>
              {f.parts.map((p) => (
                <span key={p.key} className={`hex__part hex--${p.key}`} title={PART_LABEL[p.key]}>
                  {(p.key === 'body' ? p.bytes.slice(0, BODY_SHOWN) : p.bytes).join(' ')}
                  {p.key === 'body' && p.bytes.length > BODY_SHOWN && ` … ${p.bytes.length - BODY_SHOWN} more`}
                </span>
              ))}
            </div>
            <dl className="hex__legend">
              <dt className="hex--length">Length</dt>
              <dd>{f.length} bytes follow</dd>
              <dt className="hex--session">Session</dt>
              <dd>device {f.sessionId}</dd>
              <dt className="hex--sf">Stream / function</dt>
              <dd>
                S{f.stream}F{f.func}, trace data send; W-bit {f.wBit ? 'set (reply expected)' : 'clear (no reply)'}
              </dd>
              <dt className="hex--type">PType / SType</dt>
              <dd>
                {f.pType} / {f.sType}: a SECS-II data message
              </dd>
              <dt className="hex--system">System bytes</dt>
              <dd>{f.system}, the transaction ID a reply would echo</dd>
            </dl>
          </li>
          <li>
            <h4>The body is decoded into SECS-II items</h4>
            <p className="muted">
              Each item is a format byte, a length and a payload. Shown here in SML, the notation tool manuals use.
            </p>
            <pre className="frame__sml">{frame.sml}</pre>
          </li>
          <li>
            <h4>Values get their names from S1F12</h4>
            <p className="muted">
              The trace is a plain list of numbers. At connect time the host asked for the status variable namelist
              (S1F11), so it knows the second number is SVID 2, and so on.
            </p>
            <table>
              <thead>
                <tr>
                  <th scope="col">Position</th>
                  <th scope="col">SVID</th>
                  <th scope="col">Name</th>
                  <th scope="col" className="num">
                    Value
                  </th>
                </tr>
              </thead>
              <tbody>
                {frame.sensors.map((s, i) => (
                  <tr key={s.svid}>
                    <td className="num">{i + 1}</td>
                    <td className="num">{s.svid}</td>
                    <td>{s.name}</td>
                    <td className="num">
                      {s.value.toFixed(s.value >= 100 ? 1 : 3)} {s.units}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </li>
        </ol>
      )}
    </section>
  )
}
