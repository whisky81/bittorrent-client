
import http from 'http'
import bencode from 'bencode'
import parse from "parse-torrent";
import fs from "fs";
import SwarmManager from './lib/torrent-manager.js'

const TORRENT_FILE_PATH = 'D:\\Downloads\\731B5115366AF8C1D56C87EFFB3889F5891402DB.torrent'
const SEED_FILE_PATH = 'D:\\seed.mkv'
const HOST = '127.0.0.1';
const PORT = 8080;

const parsed1 = parse(fs.readFileSync(TORRENT_FILE_PATH))
const swarmManager = new SwarmManager({
    listenPort: 16881
})

// set up seeding 
const id1 = swarmManager.add({
    parsed: parsed1,
    outputPath: SEED_FILE_PATH,
    mode: 'seeding',
    stratery: "rarest-first"
})



function compactPeer(ip, port) {
  const parts = ip.split('.').map(Number);
  const buf = Buffer.allocUnsafe(6);
  buf[0] = parts[0];
  buf[1] = parts[1];
  buf[2] = parts[2];
  buf[3] = parts[3];
  buf.writeUInt16BE(port, 4);
  return buf;
}

const server = http.createServer((req, res) => {
  const responseDict = {
    interval: 1800,                  
    peers: compactPeer('127.0.0.1', 16881)
  };
  const responseBody = bencode.encode(responseDict);
  res.writeHead(200, {
    'Content-Type': 'text/plain',   // many clients ignore this
    'Content-Length': responseBody.length
  });
  res.end(responseBody);
});
server.listen(PORT, HOST, () => {
  console.log(`\nTracker listening on http://${HOST}:${PORT}/announce\n`);
});

process.on('SIGINT', async () => {
    console.log('[graceful shutdown]')
    // seeding peer 
    const stats = swarmManager.getAllStats()
    console.log(JSON.stringify(stats, null, 2))
    swarmManager.shutdown()
    // http tracker server 
    server.close()
    process.exit(0)
})