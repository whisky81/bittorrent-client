import parse from "parse-torrent";
import Client from "torrent-discovery";
import crypto from "crypto";
import fs from "fs";
import util from "util";

// main thread
const torrent = parse(fs.readFileSync("./torrent_files/5A0EEC9BFF3FC5969CE5E3B58554C98C672C9BC9.torrent"));

// main thread
const client = new Client({
    infoHash: torrent.infoHashBuffer,
    peerId: crypto.randomBytes(20),
    announce: torrent.announce,
    port: 6881
});

client.on('warning', (err) => {
    console.log(`[WARNING] ${err.name} ${err.scheme} ${err.event}\n\tURL=${err.url}\n\tCODE=${err.code}\n\tMESSAGE=${err.message}`);
});

// announce interval 
client.on('http:response', (response) => {
    console.log(
        "HTTP RESPONSE\n" + JSON.stringify(response, null, 2)
    );
});

// announce interval
// udp retry timeout    15 * 2^k    k in [0, 8]
client.on('udp:response', (response) => {
    console.log(
        "UDP RESPONSE\n" + JSON.stringify(response, null, 2)
    );
})

client.start();
