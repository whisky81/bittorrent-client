import dgram from 'dgram'
import { CONNECTION_ID, ACTIONS, EVENTS, REQUEST_TIMEOUT,
         toUInt32, toUInt16, toUInt64,
         randomBytes4, parseCompact } from './common.js'
import TrackerError from "./TrackerError.js"

export function announce (client, url, opts) {
  // Bug 1 fix: URL.port is always a string — must convert to Number
  const parsed   = new URL(url.replace(/^udp:/, 'http:'))
  const hostname = parsed.hostname
  const port     = Number(parsed.port)

  if (!port) return client.emit('warning', new TrackerError('missing a port in tracker url', opts.event, 'udp', url, 'MISSING_PORT'))

  let socket
  let timer
  let transactionId = randomBytes4()

  function cleanup () {
    clearTimeout(timer)
    timer = null
    if (!socket) return
    socket.removeListener('message', onMessage)
    socket.on('error', () => {})
    try { socket.close() } catch (_) {}
    socket = null
  }

  function onError (err) {
    cleanup()
    client.emit('warning', err)
  }

  function onMessage (msg) {
    if (msg.length < 8 || msg.readUInt32BE(4) !== transactionId.readUInt32BE(0)) return

    const action = msg.readUInt32BE(0)

    switch (action) {
      case ACTIONS.CONNECT: {
        if (msg.length < 16) return onError(new TrackerError('invalid connect response', opts.event, 'udp', url, 'RESPONSE_ERROR'))
        const connectionId = msg.slice(8, 16)
        transactionId = randomBytes4()
        sendAnnounce(connectionId)
        break
      }

      case ACTIONS.ANNOUNCE: {
        cleanup()
        if (msg.length < 20) return client.emit('warning', new TrackerError('invalid announce response', opts.event, 'udp', url, 'RESPONSE_ERROR'))
        client.emit('udp:response', {
          event: opts.event,
          url,
          peers:      parseCompact(msg.slice(20)),
          intervalMs: msg.readUInt32BE(8) * 1000,
          leechers:   msg.readUInt32BE(12),
          seeders:    msg.readUInt32BE(16),
          received:   Date.now()
        })
        break
      }

      case ACTIONS.ERROR: {
        cleanup()
        client.emit('warning', new TrackerError(msg.slice(8).toString(), opts.event, 'udp', url, 'UNKNOWN_ERROR'))
        break
      }
    }
  }

  function send (msg) {
    if (!socket) return
    socket.send(msg, 0, msg.length, port, hostname, (err) => {
      if (err) onError(new TrackerError(err.message, opts.event, 'udp', url, 'SEND_ERROR'))
    })
  }

  function sendConnect () {
    transactionId = randomBytes4()
    send(Buffer.concat([CONNECTION_ID, toUInt32(ACTIONS.CONNECT), transactionId]))
  }

  function sendAnnounce (connectionId) {
    send(Buffer.concat([
      connectionId,
      toUInt32(ACTIONS.ANNOUNCE),
      transactionId,
      client.infoHash,
      client.peerId,
      toUInt64(opts.downloaded ?? 0),
      toUInt64(opts.left       ?? 0),
      toUInt64(opts.uploaded   ?? 0),
      toUInt32(EVENTS[opts.event] ?? 0),
      toUInt32(0),
      toUInt32(0),
      toUInt32(opts.numwant ?? 50),
      toUInt16(client.port)
    ]))
  }

  socket = dgram.createSocket('udp4')
  socket.on('message', onMessage)
  socket.on('error', onError)
  socket.bind(() => {
    timer = setTimeout(() => onError(new TrackerError('timeout', opts.event, 'udp', url, 'TIMEOUT')), REQUEST_TIMEOUT)
    if (timer.unref) timer.unref()
    sendConnect()
  })
}