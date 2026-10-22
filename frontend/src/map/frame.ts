/** Split a raw HSMS frame (hex) into the fields defined by SEMI E37. */

export interface FrameFields {
  length: number
  sessionId: number
  stream: number
  wBit: boolean
  func: number
  pType: number
  sType: number
  system: number
  /** hex groups for display: [label, bytes as "00 3a ..."] */
  parts: { key: 'length' | 'session' | 'sf' | 'type' | 'system' | 'body'; bytes: string[] }[]
}

export function parseFrame(hex: string): FrameFields | null {
  if (hex.length < 28 || hex.length % 2) return null
  const b = hex.match(/../g)!.map((h) => parseInt(h, 16))
  const u32 = (o: number) => ((b[o] << 24) | (b[o + 1] << 16) | (b[o + 2] << 8) | b[o + 3]) >>> 0
  const pairs = hex.match(/../g)!
  return {
    length: u32(0),
    sessionId: (b[4] << 8) | b[5],
    wBit: (b[6] & 0x80) !== 0,
    stream: b[6] & 0x7f,
    func: b[7],
    pType: b[8],
    sType: b[9],
    system: u32(10),
    parts: [
      { key: 'length', bytes: pairs.slice(0, 4) },
      { key: 'session', bytes: pairs.slice(4, 6) },
      { key: 'sf', bytes: pairs.slice(6, 8) },
      { key: 'type', bytes: pairs.slice(8, 10) },
      { key: 'system', bytes: pairs.slice(10, 14) },
      { key: 'body', bytes: pairs.slice(14) },
    ],
  }
}
