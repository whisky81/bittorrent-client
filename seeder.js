/**
 * SEEDER MODE
 */
import parse from "parse-torrent";
import fs from "fs";
import net from 'net'
import crypto from 'crypto'
import Protocol from 'bittorrent-protocol'


import BitfieldTracker from './lib/bitfield-tracker.js'
import SignleFileStore from './lib/single-file-store.js'

const TORRENT_FILE_PATH = 'D:\\Downloads\\731B5115366AF8C1D56C87EFFB3889F5891402DB.torrent'
const SEED_FILE_PATH = 'D:\\seed.mkv'
const LISTEN_PORT = 6881
const STATS_INTERVAL_MS  = 5_000
const REQUEST_TIMEOUT_MS = 30_000
// KEEPALIVE_INTERVAL_MS = 60_000

const parsed = parse(fs.readFileSync(TORRENT_FILE_PATH))
const piecesTracker = new BitfieldTracker(parsed.pieces.length, true);
const myPeerId = crypto.randomBytes(20)
const store = new SignleFileStore(SEED_FILE_PATH, parsed.pieceLength, parsed.lastPieceLength, parsed.pieces.length);
const stats = {
    connections: 0,
    uploaded: new Map(),
    startTime: Date.now(),
    print: function (wire = null, peerId = null) {
        const elapsed = ((Date.now() - this.startTime) / 1000).toFixed(1)
        if (wire === null && wire === peerId) {
            console.log(`[stats] connections=${this.connections} total=${this.totalUploaded()} elapsed=${elapsed}`)
            return
        }
        const currUploaded = wire.uploaded;
        this.uploaded.set(peerId, currUploaded)
        const speed = (wire.uploadSpeed() / 1024).toFixed(1)
        const total = (currUploaded / 1024 / 1024).toFixed(2)
        console.log(
            `[stats] peer=${peerId.slice(0, 8)} ` +
            `↑ speed=${speed} KB/s  total=${total} MB  elapsed=${elapsed}s`
        )
    },
    totalUploaded: function () {
        let total = this.uploaded.values().reduce((a, c) => a + c, 0)
        return (total / 1024 / 1024).toFixed(2) + ' MB'
    }
}

console.log('\t\t\tSEEDER MODE\t\t\t')
console.log('[config] my peer id\t', myPeerId)
console.log('[config] info hash\t', parsed.infoHash)
console.log('[config] total pieces\t', parsed.pieces.length)
console.log('[config] piece length\t', Math.ceil(parsed.pieceLength / 1024), 'KB')
console.log('[config] file size\t', (parsed.length / 1024 / 1024 / 1024).toFixed(2), 'GB')
console.log('[config] seed file path\t', SEED_FILE_PATH)


const handleOnePeerConn = (socket) => {
    const wire = new Protocol()
    socket.pipe(wire).pipe(socket)

    let peerId = 'unknown'
    let statsTimer = null
    ++stats.connections;

    wire.setTimeout(REQUEST_TIMEOUT_MS)
    wire.on('timeout', () => {
        console.warn(`[timeout] peer=${peerId.slice(0, 8)} — disconnecting`)
        socket.destroy()
    })

    wire.setKeepAlive(true)
    wire.on('keep-alive', () => {
        console.log(`[keep-alive] peer=${peerId.slice(0, 8)}`)
    })

    wire.on('handshake', (infoHash, remotePeerId) => {
        peerId = remotePeerId

        if (infoHash.toLowerCase() !== parsed.infoHash.toLowerCase()) {
            console.warn(`[handshake] infoHash mismatch from ${peerId.slice(0, 8)} — dropping`)
            socket.destroy()
            return
        }

        stats.print()
        wire.handshake(parsed.infoHash, myPeerId)
        wire.bitfield(piecesTracker.bitfield)
        wire.unchoke() // allow download
        // wire.interested() // i want download from u

        // stats printer mỗi 5s
        statsTimer = setInterval(() => stats.print(wire, peerId), STATS_INTERVAL_MS)
    })

    wire.on('have', (index) => {
        console.log(
            `[have] peer=${peerId.slice(0, 8)} have=${index}`
        )
    })

    wire.on('interested', () => {
        console.log(`[interested] peer=${peerId.slice(0, 8)}`)
        wire.unchoke()
    })

    wire.on('uninterested', () => {
        console.log(`[uninterested] peer=${peerId.slice(0, 8)}`)
        wire.choke()
    })

    wire.on('request', (pieceIndex, begin, length, cb) => {
        if (!piecesTracker.has(pieceIndex)) {
            console.warn(`[request] piece=${pieceIndex} [out of range | don't have] — ignoring`)
            return
        }
        try {
            const block = store.read(pieceIndex, begin, length)
            cb(null, block)
        } catch (err) {
            console.error(`[upload] disk read error:`, err.message)
        }
    })

    wire.on('choke', () => {
        console.log(`[choke] peer=${peerId.slice(0, 8)} choked us`)
    })
    wire.on('unchoke', () => {
        console.log(`[unchoke] peer=${peerId.slice(0, 8)} unchoked us`)
        // TODO: start downloading missing piece
    })

    socket.on('close', () => {
        --stats.connections
        clearInterval(statsTimer)
        stats.print(wire, peerId)
        console.log(`[disconnect] peer=${peerId.slice(0, 8)}  connections=${stats.connections}`)
    })

    socket.on('error', (err) => {
        console.error(`[socket error] peer=${peerId.slice(0, 8)}:`, err.message)
    })
}

const server = net.createServer(handleOnePeerConn)

server.listen(LISTEN_PORT, () => {
    console.log(`[seeder] host=127.0.0.1 port=${LISTEN_PORT}`)
})

server.on('error', (err) => {
    console.error('[server error]', err.message)
    process.exit(1)
})

process.on('SIGINT', () => {
    console.log('[graceful shutdown]')
    stats.print()
    server.close()
    store.close()
    process.exit(0)
})