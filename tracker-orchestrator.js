/**
 * TrackerOrchestrator
 *
 * Wraps the torrent-tracker Client to provide:
 *   1. Automatic re-announce at intervalMs from tracker response
 *   2. UDP exponential-backoff retry: delay = 15 * 2^k seconds, k = 0..8
 *   3. De-duplicated peer connection — each ip:port connected only once per session
 *   4. EventEmitter interface for Electron IPC
 *
 * Events:
 *   'peer'       (ip: string, port: number)  — new peer discovered & connected
 *   'announce'   (url: string, peers: number, intervalMs: number) — tracker response received
 *   'warning'    (err: TrackerError)          — non-fatal tracker error
 *   'retry'      (url: string, k: number, delayMs: number)        — UDP retry scheduled
 *   'exhausted'  (url: string)                — UDP tracker gave up after k=8
 */

import crypto from 'crypto'
import { EventEmitter } from 'events'
import Client from 'torrent-discovery'   // torrent-tracker Client

const UDP_BASE_MS    = 15_000          // 15 s base
const MAX_K          = 8               // max retries: 15 * 2^8 = 64 min
const DEFAULT_REANNOUNCE_MS = 30_000   // fallback if tracker omits interval

class TrackerOrchestrator extends EventEmitter {
  /**
   * @param {Object}        opts
   * @param {Object}        opts.parsed       - Output of parse() from parse-torrent
   * @param {number}        opts.sessionId    - ID returned by swarmManager.add()
   * @param {TorrentManager} opts.swarmManager - TorrentManager instance
   * @param {number}        opts.port         - Local TCP listen port (same as TorrentManager listenPort)
   */
  constructor ({ parsed, sessionId, swarmManager, port }) {
    super()
    this.parsed       = parsed
    this.sessionId    = sessionId
    this.swarmManager = swarmManager
    this.port         = port

    /** @type {Client|null} */
    this.client = null

    /** Map<url: string, Timer> — pending re-announce / retry timers */
    this._timers = new Map()

    /** Map<url: string, number> — current retry k per UDP tracker URL */
    this._udpRetryK = new Map()

    /** Set<string> — 'ip:port' strings already connected this session */
    this._seen = new Set()

    this._destroyed = false
  }

  // ─── Public API ───────────────────────────────────────────────────────────

  /**
   * Start tracker discovery. Calls client.start() (event='started').
   * Safe to call only once.
   */
  start () {
    if (this.client) throw new Error('TrackerOrchestrator already started')

    this.client = new Client({
      infoHash : this.parsed.infoHashBuffer,
      peerId   : crypto.randomBytes(20),
      announce : this.parsed.announce,
      port     : this.port,
    })

    this.client.on('http:response', res => this._onResponse(res))
    this.client.on('udp:response',  res => this._onResponse(res))
    this.client.on('warning',       err => this._onWarning(err))

    this.client.start()
  }

  /**
   * Send 'stopped' event to all trackers and tear down all timers.
   * After calling stop() this instance is unusable.
   */
  stop () {
    this._destroyed = true
    this._clearAllTimers()
    if (this.client) {
      this.client.stop()
      this.client.destroy()
      this.client = null
    }
  }

  /**
   * Send 'completed' event to all trackers (call when download finishes).
   */
  complete () {
    if (this.client) this.client.complete()
  }

  // ─── Internal ─────────────────────────────────────────────────────────────

  /**
   * Handle a successful tracker response (HTTP or UDP).
   *
   * @param {Object}   res
   * @param {string}   res.url        - Tracker URL
   * @param {string[]} res.peers      - Compact peer list [ 'ip:port', ... ]
   * @param {number}   res.intervalMs - Re-announce interval in milliseconds
   * @param {string}   res.event      - 'started' | 'stopped' | 'completed' | ''
   */
  _onResponse ({ url, peers, intervalMs }) {
    if (this._destroyed) return

    // Successful response → reset UDP retry counter for this URL
    this._udpRetryK.delete(url)

    // Connect newly-seen peers
    let newCount = 0
    for (const peer of (peers || [])) {
      if (this._seen.has(peer)) continue
      this._seen.add(peer)

      const colonIdx = peer.lastIndexOf(':')
      const ip   = peer.slice(0, colonIdx)
      const port = Number(peer.slice(colonIdx + 1))
      if (!ip || !port) continue

      try {
        this.swarmManager.connect(this.sessionId, ip, port)
        this.emit('peer', ip, port)
        newCount++
      } catch (err) {
        console.error(`[orchestrator] connect ${peer} failed:`, err.message)
      }
    }

    const delay = intervalMs || DEFAULT_REANNOUNCE_MS
    this.emit('announce', url, newCount, delay)
    this._scheduleAnnounce(url, delay)
  }

  /**
   * Handle a tracker warning (non-fatal error).
   * For UDP timeouts / send errors, schedule exponential-backoff retry.
   *
   * @param {TrackerError} err
   */
  _onWarning (err) {
    if (this._destroyed) return
    this.emit('warning', err)

    const isUdpRetryable = err.scheme === 'udp' &&
      (err.code === 'TIMEOUT' || err.code === 'SEND_ERROR' || err.code === 'RESPONSE_ERROR')

    if (!isUdpRetryable) return

    const url = err.url
    const k   = this._udpRetryK.get(url) ?? 0

    if (k > MAX_K) {
      this._udpRetryK.delete(url)
      this.emit('exhausted', url)
      console.warn(`[orchestrator] UDP exhausted k=${MAX_K} for ${url}`)
      return
    }

    const delayMs = UDP_BASE_MS * Math.pow(2, k)   // 15s, 30s, 60s … 3840s (64min)
    this._udpRetryK.set(url, k + 1)

    this.emit('retry', url, k, delayMs)
    console.log(`[orchestrator] UDP retry k=${k} in ${delayMs / 1000}s → ${url}`)
    this._scheduleAnnounce(url, delayMs)
  }

  /**
   * Schedule a re-announce for one tracker URL.
   * Replaces any existing pending timer for that URL.
   *
   * @param {string} url
   * @param {number} delayMs
   */
  _scheduleAnnounce (url, delayMs) {
    if (this._destroyed) return

    const existing = this._timers.get(url)
    if (existing) clearTimeout(existing)

    const t = setTimeout(() => {
      if (this._destroyed) return
      this._timers.delete(url)
      // _announceOne is a private method on Client — call with empty event for 'update'
      this.client._announceOne(url, { event: '' })
    }, delayMs)

    if (t.unref) t.unref()   // don't keep the Node process alive
    this._timers.set(url, t)
  }

  _clearAllTimers () {
    for (const t of this._timers.values()) clearTimeout(t)
    this._timers.clear()
  }
}

export default TrackerOrchestrator