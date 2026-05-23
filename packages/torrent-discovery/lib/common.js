import crypto from 'crypto'

export const REQUEST_TIMEOUT   = 15000
export const CONNECTION_ID_TTL = 60000
export const MAX_RESPONSE_SIZE = 64 * 1024  // 64KB — tracker response never needs more

export const CONNECTION_ID = Buffer.allocUnsafe(8)
CONNECTION_ID.writeBigUInt64BE(0x41727101980n, 0)

export const ACTIONS = { CONNECT: 0, ANNOUNCE: 1, ERROR: 3 }
export const EVENTS  = { started: 2, stopped: 3, completed: 1, '': 0 }

export const toUInt32 = (n) => { const b = Buffer.allocUnsafe(4); b.writeUInt32BE(n >>> 0, 0); return b }
export const toUInt16 = (n) => { const b = Buffer.allocUnsafe(2); b.writeUInt16BE(n & 0xffff, 0); return b }

// Guard against NaN/undefined before BigInt conversion
export const toUInt64 = (n) => {
  const b = Buffer.allocUnsafe(8)
  b.writeBigUInt64BE(BigInt(Number.isFinite(n) ? Math.max(0, Math.floor(n)) : 0), 0)
  return b
}

export const randomBytes4 = () => crypto.randomBytes(4)

export function encodeBin (buf) {
  let str = ''
  for (const b of buf) {
    if ((b >= 0x41 && b <= 0x5A) || (b >= 0x61 && b <= 0x7A) ||
        (b >= 0x30 && b <= 0x39) || b === 0x2D || b === 0x5F ||
        b === 0x2E || b === 0x7E) str += String.fromCharCode(b)
    else str += '%' + b.toString(16).padStart(2, '0').toUpperCase()
  }
  return str
}

// Parse compact peer list — filter port=0 (invalid, non-listening peers)
export function parseCompact (buf) {
  const peers = []
  for (let i = 0; i + 6 <= buf.length; i += 6) {
    const port = buf.readUInt16BE(i + 4)
    if (port === 0) continue  // port 0 = peer not listening
    peers.push(`${buf[i]}.${buf[i+1]}.${buf[i+2]}.${buf[i+3]}:${port}`)
  }
  return peers
}