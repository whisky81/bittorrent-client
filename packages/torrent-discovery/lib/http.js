import http from 'http'
import https from 'https'
import bencode from 'bencode'
import { REQUEST_TIMEOUT, MAX_RESPONSE_SIZE, encodeBin, parseCompact } from './common.js'
import TrackerError from './TrackerError.js';

export function announce(client, url, opts) {
  const u = new URL(url)
  const params = new URLSearchParams({
    port: client.port,
    uploaded: opts.uploaded ?? 0,
    downloaded: opts.downloaded ?? 0,
    left: opts.left ?? 0,
    compact: 1,
    numwant: opts.numwant ?? 50
  })
  if (opts.event) params.set('event', opts.event)

  const fullUrl = `${url}?info_hash=${encodeBin(client.infoHash)}&peer_id=${encodeBin(client.peerId)}&${params}`
  const transport = u.protocol === 'https:' ? https : http

  const req = transport.get(fullUrl, (res) => {
    if (res.statusCode !== 200) {
      res.resume()
      return client.emit('warning', new TrackerError(`status code: ${res.statusCode}`, opts.event, 'http', url, 'RESPONSE_ERROR'));
    }

    const chunks = []
    let totalBytes = 0

    res.on('data', c => {
      totalBytes += c.length
      if (totalBytes > MAX_RESPONSE_SIZE) {
        req.destroy(new TrackerError('response too large', opts.event, 'http', 'BIG_RESPONSE'));
        return
      }
      chunks.push(c)
    })

    res.on('end', () => {
      try {
        const decoded = bencode.decode(Buffer.concat(chunks))

        if (decoded['failure reason']) {
          return client.emit('warning', new TrackerError(Buffer.from(decoded['failure reason']).toString(), opts.event, 'http', url, 'HTTP_TRACKER_FAILURE_REASON'))
        }

        const rawPeers = decoded.peers
        if (rawPeers && ArrayBuffer.isView(rawPeers)) {
          client.emit('http:response', {
            event: opts.event,
            url,
            peers: parseCompact(Buffer.from(rawPeers)),
            // Bug 3 fix: decoded.interval có thể undefined → NaN
            // dùng nullish coalescing trước khi nhân
            intervalMs: (decoded.interval ?? 0) * 1000,
            received: Date.now()
          })
        }

      } catch (err) {
        client.emit('warning', new TrackerError(err.message, opts.event, 'http', url, 'UNKNOWN_ERROR'))
      }
    })

    res.on('error', err => client.emit('warning', new TrackerError(err.message, opts.event, 'http', url, 'RESPONSE_ERROR')))
  })

  req.setTimeout(REQUEST_TIMEOUT, () => req.destroy(new TrackerError('timeout', opts.event, 'http', url, 'TIMEOUT')))
  req.on('error', err => client.emit('warning', err))
}