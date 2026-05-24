import EventEmitter from 'events'
import http from 'http'
import { Storage } from './storage.js'
import { Peer } from './peer.js'
import Discovery from '../packages/torrent-discovery/index.js'
import crypto from 'crypto'

/**
 * HTTP server for streaming downloaded files to VLC
 */
class StreamingServer extends EventEmitter {
  constructor (storage, port = 8989) {
    super()
    this.storage = storage
    this.port = port
    this.server = null
  }

  start () {
    return new Promise((resolve, reject) => {
      this.server = http.createServer(async (req, res) => {
        const url = new URL(req.url, `http://localhost:${this.port}`)
        const fileIndex = parseInt(url.searchParams.get('file') || '0')
        
        if (fileIndex >= this.storage.files.length) {
          res.writeHead(404)
          res.end('File not found')
          return
        }

        const file = this.storage.files[fileIndex]
        const range = req.headers.range
        
        // Support range requests for seeking in VLC
        if (range) {
          const parts = range.replace(/bytes=/, '').split('-')
          const start = parseInt(parts[0], 10)
          const end = parts[1] ? parseInt(parts[1], 10) : file.length - 1
          const chunkSize = end - start + 1

          res.writeHead(206, {
            'Content-Range': `bytes ${start}-${end}/${file.length}`,
            'Accept-Ranges': 'bytes',
            'Content-Length': chunkSize,
            'Content-Type': this.getContentType(file.name)
          })

          try {
            // Read from storage and stream
            let offset = start
            let remaining = chunkSize
            
            while (remaining > 0) {
              const readSize = Math.min(65536, remaining)
              const data = await this.readFromFile(file, offset, readSize)
              if (data.length === 0) break
              
              res.write(data)
              offset += data.length
              remaining -= data.length
            }
            res.end()
          } catch (err) {
            res.writeHead(500)
            res.end('Error reading file')
          }
        } else {
          res.writeHead(200, {
            'Content-Length': file.length,
            'Content-Type': this.getContentType(file.name),
            'Accept-Ranges': 'bytes'
          })

          try {
            let offset = 0
            while (offset < file.length) {
              const readSize = Math.min(65536, file.length - offset)
              const data = await this.readFromFile(file, offset, readSize)
              if (data.length === 0) break
              
              res.write(data)
              offset += data.length
            }
            res.end()
          } catch (err) {
            res.writeHead(500)
            res.end('Error reading file')
          }
        }
      })

      this.server.listen(this.port, () => {
        resolve(`http://localhost:${this.port}`)
      })

      this.server.on('error', reject)
    })
  }

  async readFromFile (file, offset, length) {
    // Calculate piece indices and offsets
    const { pieceLength } = this.storage.torrent
    const absoluteOffset = file.offset + offset
    
    const startPiece = Math.floor(absoluteOffset / pieceLength)
    const endPiece = Math.floor((absoluteOffset + length - 1) / pieceLength)
    
    const buffer = Buffer.alloc(length)
    let bytesRead = 0
    
    for (let piece = startPiece; piece <= endPiece && bytesRead < length; piece++) {
      const pieceStart = piece * pieceLength
      const pieceEnd = pieceStart + pieceLength
      
      // Calculate overlap with requested range
      const readStart = Math.max(absoluteOffset, pieceStart)
      const readEnd = Math.min(absoluteOffset + length, pieceEnd)
      
      if (readStart < readEnd) {
        const pieceOffset = readStart - pieceStart
        const readLength = readEnd - readStart
        
        const data = await this.storage.read(piece, pieceOffset, readLength)
        data.copy(buffer, bytesRead)
        bytesRead += data.length
      }
    }
    
    return buffer.slice(0, bytesRead)
  }

  getContentType (filename) {
    const ext = filename.split('.').pop().toLowerCase()
    const types = {
      'mp4': 'video/mp4',
      'mkv': 'video/x-matroska',
      'avi': 'video/x-msvideo',
      'webm': 'video/webm',
      'mov': 'video/quicktime',
      'mp3': 'audio/mpeg',
      'flac': 'audio/flac',
      'wav': 'audio/wav',
      'pdf': 'application/pdf',
      'zip': 'application/zip',
      'txt': 'text/plain'
    }
    return types[ext] || 'application/octet-stream'
  }

  stop () {
    return new Promise((resolve) => {
      if (this.server) {
        this.server.close(resolve)
      } else {
        resolve()
      }
    })
  }
}

/**
 * Main Torrent Client
 * Combines peer management, storage, discovery, and streaming
 */
export class TorrentClient extends EventEmitter {
  constructor (options = {}) {
    super()
    
    this.torrent = null
    this.storage = null
    this.discovery = null
    this.peers = new Map()
    this.streamingServer = null
    
    this.strategy = options.strategy || 'rarest' // 'rarest' or 'sequential'
    this.downloadPath = options.downloadPath || './downloads'
    this.peerId = this.generatePeerId()
    this.port = options.port || 6881
    
    this.downloaded = 0
    this.uploaded = 0
    this.pieceStatus = [] // Track which pieces we have
    
    this.isSequential = this.strategy === 'sequential'
  }

  /**
   * Generate a random peer ID
   */
  generatePeerId () {
    const prefix = '-WT0001-' // WebTorrent-style prefix
    const random = crypto.randomBytes(12).toString('hex')
    return Buffer.from(prefix + random).slice(0, 20)
  }

  /**
   * Load a torrent file
   */
  async load (torrentBuffer) {
    const parse = (await import('../packages/parse-torrent/index.js')).default
    this.torrent = parse(torrentBuffer)
    
    // Initialize piece status array
    this.pieceStatus = new Array(this.torrent.pieces.length).fill(false)
    
    // Create storage layer
    const downloadDir = `${this.downloadPath}/${this.torrent.name}`
    this.storage = new Storage(this.torrent, downloadDir)
    await this.storage.init()
    
    // Setup streaming server for sequential mode
    if (this.isSequential) {
      this.streamingServer = new StreamingServer(this.storage)
      const streamUrl = await this.streamingServer.start()
      this.emit('stream-ready', streamUrl)
    }
    
    // Start tracker discovery
    this.startDiscovery()
    
    this.emit('ready', {
      name: this.torrent.name,
      infoHash: this.torrent.infoHash,
      files: this.torrent.files,
      totalLength: this.torrent.length
    })
  }

  /**
   * Start peer discovery via trackers
   */
  startDiscovery () {
    if (!this.torrent || !this.torrent.announce.length) {
      return
    }

    this.discovery = new Discovery({
      infoHash: this.torrent.infoHashBuffer,
      peerId: this.peerId,
      announce: this.torrent.announce,
      port: this.port
    })

    this.discovery.on('http:response', (data) => {
      this.handlePeers(data.peers)
    })

    this.discovery.on('udp:response', (data) => {
      this.handlePeers(data.peers)
    })

    this.discovery.on('warning', (err) => {
      this.emit('warning', err)
    })

    // Announce to trackers
    this.discovery.start()
    
    // Periodically update trackers
    setInterval(() => {
      this.discovery.update({
        downloaded: this.downloaded,
        uploaded: this.uploaded,
        left: this.torrent.length - this.downloaded
      })
    }, 900000) // Every 15 minutes
  }

  /**
   * Handle discovered peers
   */
  handlePeers (peerList) {
    for (const peerAddr of peerList) {
      const [ip, portStr] = peerAddr.split(':')
      const port = parseInt(portStr, 10)
      
      if (!this.peers.has(peerAddr)) {
        this.connectToPeer(ip, port, peerAddr)
      }
    }
  }

  /**
   * Connect to a peer
   */
  async connectToPeer (ip, port, peerAddr) {
    try {
      const peer = new Peer(
        this.peerId.toString('hex'),
        this.torrent.infoHash,
        this.storage,
        this.strategy
      )

      peer.on('handshake', () => {
        this.emit('peer-connected', peerAddr)
      })

      peer.on('bitfield', () => {
        this.scheduleDownloads()
      })

      peer.on('piece', (pieceIndex) => {
        this.checkDownloadComplete()
      })

      peer.on('piece-complete', (pieceIndex) => {
        this.pieceStatus[pieceIndex] = true
        this.emit('piece-downloaded', pieceIndex)
        this.scheduleDownloads()
      })

      peer.on('error', (err) => {
        this.emit('peer-error', peerAddr, err)
        this.removePeer(peerAddr)
      })

      peer.on('close', () => {
        this.removePeer(peerAddr)
      })

      await peer.connect(ip, port)
      this.peers.set(peerAddr, peer)
      
    } catch (err) {
      this.emit('peer-error', peerAddr, err)
    }
  }

  /**
   * Remove a peer
   */
  removePeer (peerAddr) {
    const peer = this.peers.get(peerAddr)
    if (peer) {
      peer.disconnect()
      this.peers.delete(peerAddr)
    }
  }

  /**
   * Schedule piece downloads based on strategy
   */
  scheduleDownloads () {
    for (const [addr, peer] of this.peers) {
      const nextPiece = peer.getNextPiece()
      if (nextPiece !== null) {
        peer.requestPiece(nextPiece)
      }
    }
  }

  /**
   * Check if download is complete
   */
  checkDownloadComplete () {
    const allComplete = this.pieceStatus.every(status => status === true)
    if (allComplete && !this.complete) {
      this.complete = true
      this.emit('complete')
      
      // Announce completion to tracker
      if (this.discovery) {
        this.discovery.complete()
      }
    }
  }

  /**
   * Get streaming URL for VLC (sequential mode only)
   */
  getStreamingURL (fileIndex = 0) {
    if (!this.isSequential) {
      throw new Error('Streaming URL only available in sequential mode')
    }
    
    if (this.streamingServer) {
      return `${this.streamingServer.port}/stream?file=${fileIndex}`
    }
    
    return null
  }

  /**
   * Get download progress
   */
  getProgress () {
    const totalPieces = this.pieceStatus.length
    const downloadedPieces = this.pieceStatus.filter(s => s).length
    const percentage = (downloadedPieces / totalPieces) * 100
    
    return {
      downloadedPieces,
      totalPieces,
      percentage: percentage.toFixed(2),
      downloadedBytes: this.downloaded,
      totalBytes: this.torrent?.length || 0,
      peers: this.peers.size
    }
  }

  /**
   * Stop the client
   */
  async stop () {
    // Stop discovery
    if (this.discovery) {
      this.discovery.stop()
      this.discovery.destroy()
    }

    // Disconnect all peers
    for (const [addr, peer] of this.peers) {
      peer.disconnect()
    }
    this.peers.clear()

    // Stop streaming server
    if (this.streamingServer) {
      await this.streamingServer.stop()
    }

    // Close storage
    if (this.storage) {
      await this.storage.close()
    }

    this.emit('stopped')
  }
}

export default TorrentClient
