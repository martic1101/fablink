import { getPlayer } from './replay/player'
import { DEMO, liveTransport, type Transport } from './transport'

/** Live host, or the recording in the GitHub Pages demo build. */
export const transport: Transport = DEMO ? getPlayer() : liveTransport
