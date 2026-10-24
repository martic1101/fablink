import { useEffect, useReducer } from 'react'
import { REPO_URL } from '../transport'
import { getPlayer } from './player'

const fmt = (s: number) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`

/** Playback controls for the recorded demo (GitHub Pages build only). */
export function ReplayBar() {
  const player = getPlayer()
  const [, rerender] = useReducer((n: number) => n + 1, 0)
  useEffect(() => player.subscribe(rerender), [player])

  if (player.error) {
    return (
      <div className="replaybar replaybar--error" role="alert">
        {player.error} <a href={REPO_URL}>See the project on GitHub</a>.
      </div>
    )
  }
  const recorded = player.rec ? new Date(player.rec.data.recorded_at).toLocaleDateString() : ''

  return (
    <div className="replaybar" role="region" aria-label="Recorded demo playback">
      <p className="replaybar__note">
        <strong>Recorded demo.</strong> A replay of a real FabLink run{recorded && ` recorded on ${recorded}`}, not a
        video: click around freely. Controls that would change the system are turned off.{' '}
        <a href={`${REPO_URL}#running-it`}>Run it live</a>
      </p>
      <div className="replaybar__controls">
        <button type="button" onClick={() => (player.playing ? player.pause() : player.play())} disabled={!player.rec}>
          {player.playing ? 'Pause' : 'Play'}
        </button>
        <input
          type="range"
          min={0}
          max={player.duration}
          step={0.5}
          value={player.t}
          disabled={!player.rec}
          onChange={(e) => player.seek(Number(e.target.value))}
          aria-label="Playback position"
        />
        <span className="num replaybar__time">
          {fmt(player.t)} / {fmt(player.duration)}
        </span>
        <div className="replaybar__rate" role="radiogroup" aria-label="Playback speed">
          {[1, 2, 4].map((r) => (
            <button key={r} type="button" role="radio" aria-checked={player.rate === r} onClick={() => player.setRate(r)}>
              {r}×
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}
