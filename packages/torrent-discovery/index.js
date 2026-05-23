import EventEmitter from 'events'
import * as httpTracker from './lib/http.js'
import * as udpTracker  from './lib/udp.js'

export default class Client extends EventEmitter {
  constructor ({ infoHash, peerId, announce, port }) {
    super()

    if (!infoHash) throw new Error('infoHash required')
    if (!peerId)   throw new Error('peerId required')
    if (!port)     throw new Error('port required')

    this.infoHash  = Buffer.isBuffer(infoHash) ? infoHash : Buffer.from(infoHash, 'hex')
    this.peerId    = Buffer.isBuffer(peerId)   ? peerId   : Buffer.from(peerId,   'hex')

    if (this.infoHash.length !== 20) throw new Error('infoHash must be 20 bytes')
    if (this.peerId.length   !== 20) throw new Error('peerId must be 20 bytes')

    this.announce  = Array.isArray(announce) ? announce : []
    this.port      = port
    this.destroyed = false
  }

  start ()    { this._announce({ event: 'started'   }) }
  stop ()     { this._announce({ event: 'stopped'   }) }
  complete () { this._announce({ event: 'completed' }) }

  // Bug 4 fix: destructure với default {} để client.update() không throw
  update ({ uploaded, downloaded, left } = {}) {
    this._announce({ event: '', uploaded, downloaded, left })
  }

  destroy () { this.destroyed = true }

  _announce (opts) {
    for (const url of this.announce) this._announceOne(url, opts)
  }

  _announceOne (url, opts) {
    if (this.destroyed) return
    try {
      if (url.startsWith('http:') || url.startsWith('https:')) {
        httpTracker.announce(this, url, opts)
      } else if (url.startsWith('udp:')) {
        udpTracker.announce(this, url, opts)
      } else {
        this.emit('warning', new Error(`unsupported scheme: ${url}`))
      }
    } catch (err) {
      this.emit('warning', err)
    }
  }
}