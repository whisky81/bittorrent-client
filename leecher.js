
import parse from "parse-torrent";
import fs from "fs";
import Client from 'torrent-discovery';
import SwarmManager from './lib/torrent-manager.js'
import net from 'net'
const TORRENT_FILE_PATH = 'C:\\Users\\whisky\\OneDrive - ptit.edu.vn\\Desktop\\C3839BDEA4156F0978032F56F848C4FDAF106DFD.torrent'
const PEER_RETRY_TIME = 2_000

const swarmManager = new SwarmManager({
    listenPort: 6882
})

const parsed1 = parse(fs.readFileSync(TORRENT_FILE_PATH)) 
const id1 = swarmManager.add({
    parsed: parsed1,
    outputPath: "D:\\" + parsed1.files[0].name,
    mode: 'leeching',
    stratery: "rarest-first"
})

const myPeerId = swarmManager.sessions.get(id1).myPeerId
const client = new Client({
    infoHash: parsed1.infoHash,
    peerId: myPeerId,
    announce: parsed1.announce,
    port: 6882
})

client.on('warning', (err) => {
    console.log(`[WARNING TORRENT DISCOVERY] EVENT=${err.event} URL=${err.url} MESSAGE=${err.message}`)
})

client.on('http:response', (response) => {
    for (const peer of response.peers) {
        let [ip, port] = peer.split(":");
        swarmManager.connect(id1, ip, parseInt(port))
    }
})

client.on('udp:response', (response) => {
    for (const peer of response.peers) {
        let [ip, port] = peer.split(":");
        swarmManager.connect(id1, ip, parseInt(port))
    }
})


swarmManager.onSession(id1, 'done', () => {
    console.log('---Downloaded successfully---')
    client.complete()
})

swarmManager.onSession(id1, 'piece', (pieceIndex, remainingPieces) => {
    console.log(`[piece] piece=${pieceIndex} remaining pieces=${remainingPieces}`)
})

swarmManager.onSession(id1, 'failed', (pieceIndex) => {
    console.log(`[failed] piece=${pieceIndex}`)
})

swarmManager.onSession(id1, 'peer:add', (ip, port, peerId) => {
    console.log(`[peer:add] ip=${ip} port=${port} peerId=${peerId}`)
})

swarmManager.onSession(id1, 'peer:drop', (ip, port, peerId) => {
    console.log(`[peer:drop] ip=${ip} port=${port} peerId=${peerId}`)
    if (!net.isIPv4(ip) || !(Number.isInteger(port) && port >= 1 && port <= 65535)) return 
    setTimeout(() => {
        swarmManager.connect(id1, ip, port)
    }, PEER_RETRY_TIME)
})

swarmManager.onSession(id1, 'progress', (stats) => {
    console.log(`progress ${stats.progress}% downloaded=${stats.downloadedMB}MB uploaded=${stats.uploadedMB} peer=${stats.noOfCPeer}`)
})

client.start()

process.on('SIGINT', async () => {
    console.log('[graceful shutdown]')
    const stats = swarmManager.getAllStats()
    console.log(JSON.stringify(stats, null, 2))
    swarmManager.shutdown()
    process.exit(0)
})
