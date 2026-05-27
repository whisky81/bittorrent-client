import os from 'os'
import path from 'path'
import { fileURLToPath } from 'url'
import Protocol from 'bittorrent-protocol'
import net from 'net'
import crypto from 'crypto'

import WorkerPool from './worker-pool.js'
import BitfieldTracker from './bitfield-tracker.js'
import PieceSelector from './piece-selector.js'
import SignleFileStore from './single-file-store.js'
import MultiFileStore from './multi-file-store.js'

import {EventEmitter} from 'events'

const __filename = fileURLToPath(import.meta.url)
const __dirname = path.dirname(__filename)

const BLOCK_SIZE = 16384
const REQUEST_TIMEOUT_MS = 30_000
const RETRY_DELAY_MS = 2_000

class PeerWire {
    constructor({
        wire,
        socket,
        infoHash,
        myPeerId,
        piecesTracker,
        selector,
        store,
        workerPool,
        pieceHashes,
        pieceLength,
        lastPieceLength,
        sessionId,
        peerId
    }) {
        this.wire = wire 
        this.socket = socket
        this.piecesTracker = piecesTracker
        this.selector = selector 
        this.store = store 
        this.workerPool = workerPool 
        this.pieceHashes = pieceHashes 
        this.pieceLength = pieceLength
        this.lastPieceLength = lastPieceLength
        this.sessionId = sessionId
        this.infoHash = infoHash

        this.peerBitfield = Buffer.alloc(Math.ceil(pieceHashes.length / 8))
        this.pendingPiece = null // { index, arrayBuf, writeBuf, view, total, received }
        this.peerId = peerId 

        this.setUpEventListener()
        wire.handshake(infoHash, myPeerId)
        if (this.peerId !== null) {
            wire.bitfield(this.piecesTracker.bitfield)
            wire.unchoke()
            wire.interested()
        }
    }

    setUpEventListener() {
        const { wire, socket } = this;

        wire.setTimeout(REQUEST_TIMEOUT_MS)
        wire.on('timeout', () => {
            console.error('[timeout] no response from peer — check connection')
            socket.destroy()
        })

        wire.setKeepAlive(true)
        wire.on('keep-alive', () => {
            console.log('[keep-alive] received from peer')
        })

        wire.on('handshake', (infoHash, peerId) => {
            console.log(`[handshake] connected to peer=${peerId.slice(0, 8)}`)

            if (infoHash.toLowerCase() !== this.infoHash.toLowerCase()) {
                console.error('[handshake] infoHash mismatch — disconnecting')
                socket.destroy()
                return
            }
            this.peerId = peerId 
            wire.bitfield(this.piecesTracker.bitfield)
            wire.unchoke()
            wire.interested()
        })

        wire.on('bitfield', (bf) => {
            this.peerBitfield = Buffer.from(bf.buffer)
            this.selector.addPeerBitfield(this.peerBitfield, this.pieceHashes.length)
            this._reqNext()
        })

        wire.on('have', (i) => {
            console.log(`[have] peer has piece=${i}`)
            this.peerBitfield[Math.floor(i / 8)] |= (1 << (7 - (i % 8)))
            this.selector.addHave(i)
            this._reqNext()
        })

        wire.on('choke', () => {
            if (this.pendingPiece) {
                this.selector.markFailed(this.pendingPiece.index)
                this.pendingPiece = null
            }
        })

        wire.on('unchoke', () => {
            this._reqNext()
        })
        
        wire.on('interested', () => {
            console.log(`[interested] peer=${this.peerId?.slice(0, 8)}`)
        })

        wire.on('request', (pieceIndex, begin, length, cb) => {
            if (!this.piecesTracker.has(pieceIndex)) {
                console.log(`[request] piece=${pieceIndex} [out of range | don't have] — ignoring`)
                return
            }
            try {
                const block = this.store.read(pieceIndex, begin, length)
                cb(null, block)
            } catch (err) {
                console.error(`[upload] disk read error piece=${pieceIndex}:`, err.message)
            }
        })
    }

    _reqNext() {
        if (this.wire.peerChoking) {
            console.log('[request] peer choking us')
            return 
        }
        if (this.pendingPiece) {
            console.log('[request] avai pending piece')
            return 
        } 
        const index = this.selector.next(this.piecesTracker, this.peerBitfield) 
        if (index === null) {
            console.log('[request] index is null')
            return 
        }
        const reqPieceLength = index === this.pieceHashes.length - 1 ? this.lastPieceLength : this.pieceLength;
        const total         = Math.ceil(reqPieceLength / BLOCK_SIZE)
        const lastBlockSize = reqPieceLength % BLOCK_SIZE || BLOCK_SIZE

        const arrayBuf = new ArrayBuffer(reqPieceLength)
        const writeBuf = Buffer.alloc(reqPieceLength)
        const view = new Uint8Array(arrayBuf)

        this.pendingPiece = {
            index,
            arrayBuf,
            writeBuf,
            view,
            total,
            received: 0
        }        
        this.selector.markInFlight(index)
        console.log(`[download] piece=${index} (${total} blocks)  session=${this.sessionId}`)
        for (let i = 0; i < total; ++i) {
            const offset = i * BLOCK_SIZE
            const length = i === total - 1 ? lastBlockSize : BLOCK_SIZE;
            this.wire.request(index, offset, length, (err, block) => {
                if (err || !this.pendingPiece || this.pendingPiece.index !== index) {
                    console.log('[request callback]', err.message, !this.pendingPiece, 'piece mismatch', this.pendingPiece.index !== index)
                    return 
                }
                this.pendingPiece.writeBuf.set(block, offset)
                this.pendingPiece.view.set(block, offset)
                if (++this.pendingPiece.received < total) return 
                this.workerPool.push(
                    index,
                    this.pendingPiece.arrayBuf,
                    this.pendingPiece.writeBuf,
                    this.pieceHashes[index],
                    this.sessionId
                )
                this.pendingPiece = null
            })
        }

    } 
    notifyPieceDownloaded(index) {
        this.wire.have(index)     
        this._reqNext()       
    }
    getStats() {
        return {    
            peerId      : this.peerId?.slice(0, 8) ?? 'unknown',
            ip: this.socket.remoteAddress,
            port: this.socket.remotePort,
            downloaded  : this.wire.downloaded,
            uploaded    : this.wire.uploaded,
            dlSpeed     : this.wire.downloadSpeed(),
            ulSpeed     : this.wire.uploadSpeed(),
            peerChoking : this.wire.peerChoking,
            amChoking   : this.wire.amChoking,
            pending     : this.pendingPiece?.index ?? null,
        }
    }
}

// ─────────────────────────────────────────────
// TORRENT SESSION
//
// Events (dùng cho Electron / desktop app):
//   'piece'     (pieceIndex, remainingPieces) — mỗi piece verify xong
//   'failed'    (pieceIndex)            — piece SHA1 fail 
//   'done'      ()                 — tất cả pieces xong 
//   'progress'  (stats)            — mỗi khi có piece mới veryfiy thanh cong 
//   'peer:add'  (ip, port, peerId)           — peer mới kết nối, ip và port của nó
//   'peer:drop' (ip, port, peerId)           — peer disconnect
// ─────────────────────────────────────────────

class TorrentSession extends EventEmitter {
    constructor({
        id,
        infoHash,
        pieceHashes,
        pieceLength,
        lastPieceLength,
        totalLength,
        outputPath, // allways dir
        files,
        mode,
        workerPool,
        full = false
    }) {
        super()
        this.setMaxListeners(0)
        this.id = id 
        this.infoHash = infoHash
        this.pieceHashes    = pieceHashes
        this.pieceLength    = pieceLength
        this.lastPieceLength = lastPieceLength
        this.totalLength    = totalLength
        this.outputPath     = outputPath
        this.files          = files 
        this.mode           = mode 
        this.workerPool     = workerPool

        this.peerWires = new Set()
        this.myPeerId = crypto.randomBytes(20)
        this.piecesTracker = new BitfieldTracker(pieceHashes.length, full)
        this.selector = new PieceSelector(mode)
        this.store = (files.length === 1 ? new SignleFileStore(
            outputPath,
            pieceLength,
            lastPieceLength,
            pieceHashes.length
        ) : new MultiFileStore(
            outputPath,
            pieceLength,
            files
        ))
        console.log(`[session-${id}] infoHash=${infoHash.slice(0, 8)}... pieces=${pieceHashes.length} mode=${mode}`)
    }

    addWire(wire, socket, peerId = null) {
        const pw = new PeerWire({
            wire,
            socket,
            infoHash    : this.infoHash,
            myPeerId    : this.myPeerId,
            piecesTracker  : this.piecesTracker,
            selector    : this.selector,
            store       : this.store,
            workerPool  : this.workerPool,
            pieceHashes : this.pieceHashes,
            pieceLength : this.pieceLength,
            lastPieceLength: this.lastPieceLength,
            sessionId   : this.id,
            peerId
        })
        this.peerWires.add(pw)
        this.emit('peer:add', socket.remoteAddress, socket.remotePort, peerId)
        socket.on('close', () => {
            this.peerWires.delete(pw);
            this.emit('peer:drop', pw.socket.remoteAddress, pw.socket.remotePort, pw.peerId)
        })
        socket.on('error', (err) => {
            console.error(`[session-${this.id}] socket error:`, err.message)
            this.peerWires.delete(pw)
        })
        return pw
    }


    onPieceVerified(index, writeBuf) {
        this.store.write(index, writeBuf)
        this.piecesTracker.set(index)
        this.selector.markDone(index)
        for (const pw of this.peerWires) pw.notifyPieceDownloaded(index)
        const remaining = this.piecesTracker.missing().length
        console.log(`[session-${this.id}] piece=${index} ✓  remaining=${remaining}/${this.pieceHashes.length}`)
        if (remaining === 0) {
            console.log(`[session-${this.id}] ALL PIECES DONE`)
            this.emit('done')
        }
        this.emit('piece', index, remaining)
        this.emit('progress', this.getStats())
    }

    onPieceFailed(index) {
        this.selector.markFailed(index)
        console.error(`[session-${this.id}] piece=${index} SHA1 MISMATCH — retrying`)
        this.emit('failed', index)
        setTimeout(() => {
            for (const pw of this.peerWires) pw._reqNext()
        }, RETRY_DELAY_MS)
    }

    waitForPiece(index) {
        // TODO used by HTTP Stream
    }

    getStats() {
        const downloaded = this.piecesTracker.count
        const total      = this.pieceHashes.length
        const percent    = (downloaded / total * 100).toFixed(1)
 
        // tổng speed từ tất cả peers
        let dlSpeed = 0, ulSpeed = 0, dlBytes = 0, ulBytes = 0
        const peers = []
 
        for (const pw of this.peerWires) {
            const s = pw.getStats()
            dlSpeed += s.dlSpeed
            ulSpeed += s.ulSpeed
            dlBytes += s.downloaded
            ulBytes += s.uploaded
            peers.push(s)
        }
 
        return {
            sessionId        : this.id,
            infoHash         : this.infoHash,
            mode             : this.mode,
            totalPieces      : total,
            downloadedPieces : downloaded,
            remainingPieces  : total - downloaded,
            progress         : percent,          // '42.5'
            dlSpeedBps       : dlSpeed,          // bytes/sec tổng
            ulSpeedBps       : ulSpeed,
            dlSpeedKBps      : (dlSpeed / 1024).toFixed(1),
            ulSpeedKBps      : (ulSpeed / 1024).toFixed(1),
            downloadedMB     : (dlBytes / 1024 / 1024).toFixed(2),
            uploadedMB       : (ulBytes / 1024 / 1024).toFixed(2),
            connectedPeers   : this.peerWires.size,
            peers,           // per-peer stats
            workerPool       : this.workerPool.getStats(),
        }
    }


    close() {
        this.store.close()
        for (const pw of [...this.peerWires]) pw.socket.destroy() 
    }
}

class TorrentManager {
    constructor({
        poolSize = Math.min(Math.max(os.cpus().length - 1, 1), 4),
        listenPort = 6881,
        streamPort = 7070,
        workerPath = path.join(__dirname, 'verify-worker.js'),
    } = {}) {
        // this.listenPort = listenPort
        // this.streamPort = streamPort
        this.sessions = new Map()   // id → TorrentSession
        this.nextId = 0
        this.pool = new WorkerPool(workerPath, poolSize, (index, valid, writeBuf, sessionId) => {
            const session = this.sessions.get(sessionId)
            if (!session) return
            if (valid) session.onPieceVerified(index, writeBuf)
            else session.onPieceFailed(index)
        })
        this._tcpServer = net.createServer(socket => {
            const wire = new Protocol()
            socket.pipe(wire).pipe(socket)
            wire.once('handshake', (infoHash, peerId) => {
                const session = this.sessions.values()
                    .find(s => s.infoHash.toLowerCase() === infoHash.toLowerCase())
                if (!session) {
                    console.warn(`[torrent manager] info hash mismatch: ${infoHash.slice(0, 8)} — drop`)
                    socket.destroy()
                    return
                }
                session.addWire(wire, socket, peerId)
            })
            socket.on('error', console.error)
        })
        this._tcpServer.listen(listenPort, () =>
            console.log(`[torrent manager] TCP listening :${listenPort}`)
        )
        // TODO: start stream server
    }
    /**
     * Add a torrent in to manager
     * @param {Object} parsed import parse from parse-torrent
     * @param {String} outputPath file path ~ single file | dir ~ multi file
     * @param {String} stratery sequential | rarest-first
     * @param {String} mode leeching | seeding
     * @returns {number} sessionId
     */
    add({
        parsed,
        outputPath,
        stratery = 'sequential',
        mode = 'leeching'
    }) {
        const id = this.nextId++
        const session = new TorrentSession({
            id,
            infoHash: parsed.infoHash,
            pieceHashes: parsed.pieces,
            pieceLength: parsed.pieceLength,
            lastPieceLength: parsed.lastPieceLength,
            totalLength: parsed.length,
            outputPath,
            files: parsed.files,
            mode: stratery,
            workerPool: this.pool,
            full: mode === 'seeding'
        })
        this.sessions.set(id, session)
        return id
    }
    connect(sessionId, ip, port) {
        const session = this.sessions.get(sessionId)
        if (!session) throw new Error(`[session-${sessionId}] not found`)
        const socket = net.connect(port, ip)
        const wire = new Protocol()
        socket.pipe(wire).pipe(socket)
        session.addWire(wire, socket)
        socket.on('error', (err) =>
            console.error(`[session-${sessionId}] connect error ${ip}:${port}`, err.message)
        )
        console.log(`[session-${sessionId}] connecting → ${ip}:${port}`)
    }
    /**
     * Lấy stats của 1 session (cho Electron / UI polling)
     */
    getStats(sessionId) {
        const session = this.sessions.get(sessionId)
        if (!session) return null
        return session.getStats()
    }
    /**
     * Lấy stats của tất cả sessions
     */
    getAllStats() {
        return [...this.sessions.values()].map(s => s.getStats())
    }
    /**
     * Lắng nghe events của 1 session (cho Electron IPC)
     *
     * Ví dụ dùng trong Electron main process:
     *   manager.onSession(id, 'progress', (stats) => mainWindow.webContents.send('progress', stats))
     *   manager.onSession(id, 'done',     ()      => mainWindow.webContents.send('done', id))
     */
    onSession(sessionId, event, handler) {
        const session = this.sessions.get(sessionId)
        if (!session) throw new Error(`session ${sessionId} not found`)
        session.on(event, handler)
    }
    async shutdown() {
        await this.pool.terminate()
        for (const session of this.sessions.values()) session.close()
        this._tcpServer.close()
    }
}

export default TorrentManager