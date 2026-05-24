import EventEmitter from 'events'
import Protocol from 'bittorrent-protocol'
import net from 'net'
import crypto from 'crypto'

/**
 * Peer connection manager using bittorrent-protocol
 * Handles peer wire protocol communication
 */
export class Peer extends EventEmitter {
  constructor (peerId, infoHash, storage, strategy = 'rarest') {
    super()
    this.peerId = peerId
    this.infoHash = infoHash
    this.storage = storage
    this.strategy = strategy // 'rarest' or 'sequential'
    
    this.wire = null
    this.socket = null
    this.amChoking = true
    this.amInterested = false
    this.peerChoking = true
    this.peerInterested = false
    this.peerPieces = new BitField()
    
    this.requests = new Map() // Track outstanding requests
    this.maxRequests = 5
    
    // Piece tracking
    this.pieceRequests = new Map() // pieceIndex -> Set of peers requesting
    this.pieceAvailability = new Map() // pieceIndex -> count of peers having it
  }

  /**
   * Connect to a peer
   */
  connect (ip, port) {
    return new Promise((resolve, reject) => {
      const socket = net.connect(port, ip)
      
      socket.on('connect', () => {
        this.socket = socket
        this.setupWire()
        resolve(this)
      })
      
      socket.on('error', (err) => {
        this.emit('error', err)
        reject(err)
      })
      
      socket.on('close', () => {
        this.emit('close')
        this.cleanup()
      })
    })
  }

  /**
   * Setup the bittorrent protocol wire handler
   */
  setupWire () {
    const wire = new Protocol()
    this.wire = wire
    
    // Pipe socket to wire protocol
    this.socket.pipe(wire).pipe(this.socket)
    
    // Send handshake
    wire.handshake(this.infoHash, this.peerId, {
      dht: true,
      extensions: {}
    })
    
    // Handle incoming handshake
    wire.on('handshake', (infoHash, peerId, extensions) => {
      this.emit('handshake', { infoHash, peerId, extensions })
    })
    
    // Handle keep-alive
    wire.on('keep-alive', () => {
      this.emit('keep-alive')
    })
    
    // Handle choke/unchoke
    wire.on('choke', () => {
      this.peerChoking = true
      this.cancelOutstandingRequests()
      this.emit('choke')
    })
    
    wire.on('unchoke', () => {
      this.peerChoking = false
      this.emit('unchoke')
      this.updateInterested()
    })
    
    // Handle interested/not interested
    wire.on('interested', () => {
      this.peerInterested = true
      this.emit('interested')
    })
    
    wire.on('not-interested', () => {
      this.peerInterested = false
      this.emit('not-interested')
    })
    
    // Handle have messages (peer has a piece)
    wire.on('have', (pieceIndex) => {
      this.peerPieces.set(pieceIndex)
      this.updatePieceAvailability(pieceIndex, 1)
      this.emit('have', pieceIndex)
    })
    
    // Handle bitfield (initial piece availability)
    wire.on('bitfield', (bitfield) => {
      this.peerPieces = bitfield
      // Update availability for all pieces
      for (let i = 0; i < bitfield.length; i++) {
        if (bitfield.get(i)) {
          this.updatePieceAvailability(i, 1)
        }
      }
      this.emit('bitfield', bitfield)
      this.updateInterested()
    })
    
    // Handle request
    wire.on('request', (pieceIndex, offset, length) => {
      this.handleRequest(pieceIndex, offset, length)
    })
    
    // Handle piece data
    wire.on('piece', async (pieceIndex, offset, data) => {
      const requestKey = `${pieceIndex}-${offset}`
      this.requests.delete(requestKey)
      
      try {
        await this.storage.write(pieceIndex, offset, data)
        this.emit('piece', pieceIndex, offset, data)
        
        // Verify complete piece when we have all blocks
        this.checkPieceComplete(pieceIndex)
      } catch (err) {
        this.emit('error', err)
      }
    })
    
    // Handle cancel
    wire.on('cancel', (pieceIndex, offset, length) => {
      const requestKey = `${pieceIndex}-${offset}`
      this.requests.delete(requestKey)
      this.emit('cancel', pieceIndex, offset, length)
    })
    
    // Send initial messages
    wire.unchoke()
    wire.interested()
  }

  /**
   * Update piece availability tracking
   */
  updatePieceAvailability (pieceIndex, delta) {
    const current = this.pieceAvailability.get(pieceIndex) || 0
    this.pieceAvailability.set(pieceIndex, current + delta)
  }

  /**
   * Update interested status based on available pieces
   */
  updateInterested () {
    if (!this.amInterested && this.hasInterestingPieces()) {
      this.wire.interested()
      this.amInterested = true
    }
  }

  /**
   * Check if peer has pieces we need
   */
  hasInterestingPieces () {
    const { pieces } = this.storage.torrent
    for (let i = 0; i < pieces.length; i++) {
      if (!pieces[i] && this.peerPieces.get(i)) {
        return true
      }
    }
    return false
  }

  /**
   * Request a piece based on strategy
   */
  async requestPiece (pieceIndex) {
    if (this.peerChoking || this.requests.size >= this.maxRequests) {
      return
    }

    const { pieceLength } = this.storage.torrent
    const isLastPiece = pieceIndex === this.storage.torrent.pieces.length - 1
    const length = isLastPiece ? this.storage.torrent.lastPieceLength : pieceLength
    
    // Request in blocks (typically 16KB)
    const blockSize = 16384
    for (let offset = 0; offset < length; offset += blockSize) {
      const blockLength = Math.min(blockSize, length - offset)
      const requestKey = `${pieceIndex}-${offset}`
      
      if (!this.requests.has(requestKey)) {
        this.requests.set(requestKey, {
          pieceIndex,
          offset,
          length: blockLength,
          startTime: Date.now()
        })
        this.wire.request(pieceIndex, offset, blockLength)
      }
    }
  }

  /**
   * Get next piece to download based on strategy
   */
  getNextPiece () {
    const { pieces } = this.storage.torrent
    const availablePieces = []
    
    // Find pieces this peer has that we don't
    for (let i = 0; i < pieces.length; i++) {
      if (!pieces[i] && this.peerPieces.get(i)) {
        availablePieces.push({
          index: i,
          availability: this.pieceAvailability.get(i) || 0
        })
      }
    }
    
    if (availablePieces.length === 0) {
      return null
    }
    
    if (this.strategy === 'sequential') {
      // Return the lowest index piece (sequential download)
      availablePieces.sort((a, b) => a.index - b.index)
      return availablePieces[0].index
    } else {
      // Rarest first - return piece with lowest availability
      availablePieces.sort((a, b) => a.availability - b.availability)
      return availablePieces[0].index
    }
  }

  /**
   * Handle incoming piece request from peer
   */
  async handleRequest (pieceIndex, offset, length) {
    try {
      const data = await this.storage.read(pieceIndex, offset, length)
      if (data.length > 0) {
        this.wire.piece(pieceIndex, offset, data)
      }
    } catch (err) {
      this.emit('error', err)
    }
  }

  /**
   * Cancel all outstanding requests
   */
  cancelOutstandingRequests () {
    for (const [key, req] of this.requests) {
      this.wire.cancel(req.pieceIndex, req.offset, req.length)
    }
    this.requests.clear()
  }

  /**
   * Check if a piece is complete
   */
  async checkPieceComplete (pieceIndex) {
    const verified = await this.storage.verifyPiece(pieceIndex)
    if (verified) {
      this.emit('piece-complete', pieceIndex)
      // Notify other peers that we have this piece
      this.wire.have(pieceIndex)
    }
  }

  /**
   * Cleanup on disconnect
   */
  cleanup () {
    this.cancelOutstandingRequests()
    if (this.socket) {
      this.socket.destroy()
      this.socket = null
    }
    this.wire = null
  }

  /**
   * Disconnect from peer
   */
  disconnect () {
    this.cleanup()
    this.emit('disconnect')
  }
}

/**
 * Simple BitField implementation for tracking piece availability
 */
class BitField {
  constructor (bufferOrLength) {
    if (typeof bufferOrLength === 'number') {
      this.buffer = Buffer.alloc(Math.ceil(bufferOrLength / 8))
    } else if (Buffer.isBuffer(bufferOrLength)) {
      this.buffer = bufferOrLength
    } else {
      this.buffer = Buffer.alloc(0)
    }
  }

  get (index) {
    const byteIndex = index >> 3
    const bitMask = 1 << (7 - (index & 7))
    return byteIndex < this.buffer.length && (this.buffer[byteIndex] & bitMask) !== 0
  }

  set (index, value = true) {
    const byteIndex = index >> 3
    const bitMask = 1 << (7 - (index & 7))
    
    if (byteIndex >= this.buffer.length) {
      const newBuffer = Buffer.alloc(byteIndex + 1)
      this.buffer.copy(newBuffer)
      this.buffer = newBuffer
    }
    
    if (value) {
      this.buffer[byteIndex] |= bitMask
    } else {
      this.buffer[byteIndex] &= ~bitMask
    }
  }

  get length () {
    return this.buffer.length * 8
  }
}

export default Peer
