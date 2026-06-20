import parse from 'parse-torrent';
import fs from 'fs';
import SwarmManager from './lib/torrent-manager.js';
import net from 'net';

const TORRENT_FILE_PATH = 'D:\\Downloads\\E2ED88DE112FCB9246335CFA8CE81E8D369C5479.torrent';
const SAVE_DIR = 'D:\\torrent_dow\\';
const PEER_RETRY_TIME = 2_000;
let startTime = Date.now();
function formatElapsedTime(ms) {
  const seconds = Math.floor(ms / 1000);
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const secs = seconds % 60;

  if (hours > 0) {
    return `${hours}h ${minutes}m ${secs}s`;
  } else if (minutes > 0) {
    return `${minutes}m ${secs}s`;
  } else {
    return `${secs}s`;
  }
}
// debugger;
// The OS chooses a free port for incoming connections
const swarmManager = new SwarmManager();

const parsed1 = parse(fs.readFileSync(TORRENT_FILE_PATH));
// download always is leeching
const id1 = swarmManager.add({
  parsed: parsed1,
  outputPath: SAVE_DIR + parsed1.name,
  mode: 'leeching',
  stratery: 'rarest-first',
});

swarmManager.onSession(id1, 'logging', (message) => {
  console.log(message);
});

swarmManager.onSession(id1, 'done', () => {
  console.log('---Downloaded successfully---');
  process.emit('SIGINT');
});

swarmManager.onSession(id1, 'piece', (pieceIndex, remainingPieces) => {
  console.log(`[piece] piece=${pieceIndex} remaining pieces=${remainingPieces}`);
});

swarmManager.onSession(id1, 'failed', (pieceIndex) => {
  console.log(`[failed] piece=${pieceIndex}`);
});

swarmManager.onSession(id1, 'peer:add', (ip, port, peerId) => {
  console.log(`[peer:add] ip=${ip} port=${port} peerId=${peerId}`);
});

swarmManager.onSession(id1, 'peer:drop', (ip, port, peerId) => {
  console.log(`[peer:drop] ip=${ip} port=${port} peerId=${peerId}`);
  if (!net.isIPv4(ip) || !(Number.isInteger(port) && port >= 1 && port <= 65535)) return;
  setTimeout(() => {
    swarmManager.connect(id1, ip, port);
  }, PEER_RETRY_TIME);
});

swarmManager.onSession(id1, 'progress', (stats) => {
  console.log(
    `progress ${stats.progress}% downloaded=${stats.downloadedMB}MB uploaded=${stats.uploadedMB} peer=${stats.noOfCPeer}`
  );
});

process.on('SIGINT', async () => {
  console.log('[graceful shutdown]');
  const elapsedMs = Date.now() - startTime;
  const elapsedStr = formatElapsedTime(elapsedMs);
  console.log(`Elapsed: ${elapsedStr}`);
  const stats = swarmManager.getAllStats();
  console.log(JSON.stringify(stats, null, 2));
  swarmManager.shutdown();
  process.exit(0);
});
