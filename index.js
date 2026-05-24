import parse from "parse-torrent";
import Client from "torrent-discovery";

import fs from "fs";
import crypto from "crypto";

const parsed = parse(fs.readFileSync('D:\\Downloads\\2894B6B38DB5249A2C848EFCB8C62D17BB627501.torrent'))

// console.log(parsed)

const client = new Client({
    infoHash: parsed.infoHashBuffer,
    peerId: crypto.randomBytes(20),
    announce: parsed.announce,
    port: 6881
})

client.on('warning', (err) => {
    console.log(`[WARNING TORRENT DISCOVERY] EVENT=${err.event} URL=${err.url} MESSAGE=${err.message}`)
})

client.on('http:response', (response) => {
    console.log("HTTP RESPONSE")
    console.log(JSON.stringify(response, null, 2))
})

client.on('udp:response', (response) => {
    console.log("UDP RESPONSE")
    console.log(JSON.stringify(response, null, 2))
})

client.start()