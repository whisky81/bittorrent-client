/**
 * LEECHER MODE 
 */
import Protocol from 'bittorrent-protocol'
import net from 'net'
import crypto from 'crypto'
import fs from 'fs'
import parse from 'parse-torrent'

import BitfieldTracker from './lib/bitfield-tracker.js'
import SignleFileStore from './lib/single-file-store.js'
import PieceSelector from './lib/piece-selector.js'
import WorkerPool from './lib/worker-pool.js'

const TORRENT_FILE_PATH = 'D:\\Downloads\\731B5115366AF8C1D56C87EFFB3889F5891402DB.torrent'
const PEER_IP = '127.0.0.1'
const PEER_PORT = 6881
const BLOCK_SIZE = 16384
const REQUEST_TIMEOUT_MS = 30_000
const STATS_INTERVAL_MS = 5_000

const parsed = parse(fs.readFileSync(TORRENT_FILE_PATH))
const myPeerId = crypto.randomBytes(20)


const piecesTracker = new BitfieldTracker(parsed.pieces.length)
const store = new SignleFileStore(parsed.files[0].path, parsed.pieceLength, parsed.lastPieceLength, parsed.pieces.length)
const selector = new PieceSelector('sequential')
let peerBitfield = null
let statsTimer = null;



console.log('\t\t\tLEECHER MODE\t\t\t')
console.log('[config] my peer id\t', myPeerId)
console.log('[config] info hash\t', parsed.infoHash)
console.log('[config] total pieces\t', parsed.pieces.length)
console.log('[config] piece length\t', Math.ceil(parsed.pieceLength / 1024), 'KB')
console.log('[config] file size\t', (parsed.length / 1024 / 1024 / 1024).toFixed(2), 'GB')
console.log('[config] output path\t', parsed.files[0].path)
console.log('[config] download strategy\t', selector.mode)


const socket = net.connect(PEER_PORT, PEER_IP)
const wire = new Protocol()
socket.pipe(wire).pipe(socket)

const startTime = Date.now()
function printStats(wire) {
    const elapsed = ((Date.now() - startTime) / 1000).toFixed(1)
    const dlSpeed = (wire.downloadSpeed() / 1024).toFixed(1)
    const ulSpeed = (wire.uploadSpeed() / 1024).toFixed(1)
    const dlTotal = (wire.downloaded / 1024 / 1024).toFixed(2)
    const progress = ((piecesTracker.count / parsed.pieces.length) * 100).toFixed(1)
    const eta = wire.downloadSpeed() > 0
        ? (((parsed.pieces.length - piecesTracker.count) * parsed.pieceLength) / wire.downloadSpeed()).toFixed(0)
        : '∞'

    console.log(
        `[stats] ↓ ${dlSpeed} KB/s  ↑ ${ulSpeed} KB/s  ` +
        `downloaded=${dlTotal} MB  progress=${progress}%  ` +
        `ETA=${eta}s  elapsed=${elapsed}s`
    )
}

function fetchPiece(wire, pieceIndex, done) {
    const pieceLength = pieceIndex === parsed.pieces.length - 1 ? parsed.lastPieceLength : parsed.pieceLength;
    const noOfBlocks = Math.ceil(pieceLength / BLOCK_SIZE)
    const lastBlock = pieceLength % BLOCK_SIZE || BLOCK_SIZE
    const buf = Buffer.allocUnsafe(pieceLength)
    const arrBf = new ArrayBuffer(pieceLength)
    const view = new Uint8Array(arrBf)

    let received = 0

    for (let i = 0; i < noOfBlocks; i++) {
        const offset = i * BLOCK_SIZE
        const length = i === noOfBlocks - 1 ? lastBlock : BLOCK_SIZE
        wire.request(pieceIndex, offset, length, (err, block) => {
            if (err) return done(err)
            buf.set(block, offset)
            view.set(block, offset)
            if (++received < noOfBlocks) return
            pool.push(
                pieceIndex,
                arrBf,
                buf,
                parsed.pieces[pieceIndex],
                0
            )
        })
    }
}

function drain(wire) {
    if (piecesTracker.count === parsed.pieces.length) {
        clearInterval(statsTimer)
        printStats(wire)
        socket.destroy()
        return
    }
    const pieceIndex = selector.next(piecesTracker, peerBitfield)
    if (pieceIndex === null) {
        console.log('next piece index is null')
        setTimeout(() => {
            drain(wire)
        }, 3000)
        return
    }
    selector.markInFlight(pieceIndex)
    fetchPiece(wire, pieceIndex, (err) => {
        console.log('[request block error]', err.message)
        selector.markFailed(pieceIndex)  // trả piece về pool
        drain(wire)  
    })
}




const pool = new WorkerPool(
    './lib/verify-worker.js',
    3,
    (index, valid, writeBuf, sessionId) => {
        console.log(`[verify sha1] session=${sessionId} piece=${index} status=${valid ? 'match' : 'mismatch'}`)
        if (!valid) {
            selector.markFailed(index)
            return 
        } 
        store.write(index, writeBuf)
        selector.markDone(index)
        piecesTracker.set(index)
        wire.have(index)
        drain(wire)
    },
    (index, errMsg, writeBuf, sessionId) => {
        console.log('[verify error] index=', index, 'msg=', errMsg, 'session=', sessionId)
        selector.markFailed(index)
        drain(wire)
    }
)


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

    if (infoHash.toLowerCase() !== parsed.infoHash.toLowerCase()) {
        console.error('[handshake] infoHash mismatch — disconnecting')
        socket.destroy()
        return
    }

    wire.interested()
    statsTimer = setInterval(() => printStats(wire), STATS_INTERVAL_MS)
})

wire.on('choke', () => {
    console.warn('[choke] peer is choking us — paused')
})

wire.on('unchoke', () => {
    console.log('[unchoke] peer unchoked us — starting download')
    drain(wire)
})

wire.on('bitfield', (bf) => {
    console.log(`[bitfield] received (${bf.length} bytes)`)
    peerBitfield = Buffer.from(bf.buffer)
    console.log(peerBitfield.constructor.name, peerBitfield)
    selector.addPeerBitfield(peerBitfield, parsed.pieces.length)
    
})

wire.on('have', (index) => {
    console.log(`[have] peer has piece=${index}`)
    selector.addHave(index)
})

socket.on('error', (err) => {
    console.error('[socket error]', err.message)
})

socket.on('close', () => {
    console.log('[socket] connection closed')
    process.kill(process.pid, 'SIGINT')
})

process.on('SIGINT', async () => {
    console.log('[graceful shutdown]')
    socket.destroy()
    store.close()
    await pool.terminate()
    process.exit(0)
})

wire.handshake(parsed.infoHash, myPeerId)