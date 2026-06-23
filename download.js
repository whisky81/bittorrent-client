import fs from 'node:fs';
import path from 'path';
import parse from 'parse-torrent';
import net from 'net';
import chalk from 'chalk';
import { spawn } from 'node:child_process';
import SwarmManager from './lib/torrent-manager.js';
const PEER_RETRY_TIME = 2_000;
const loggerWorker = spawn('node', ['logger-worker.js'], {
  stdio: ['ignore', 'inherit', 'inherit', 'ipc'],
});
loggerWorker.unref();
function isDirSync(path) {
  try {
    return fs.statSync(path).isDirectory();
  } catch {
    return false;
  }
}
function elapsedTime() {
  const startTime = Date.now();
  return () => {
    const ms = Date.now() - startTime;
    const seconds = Math.floor(ms / 1000);
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = seconds % 60;
    if (hours > 0) return `${hours}h ${minutes}m ${secs}s`;
    if (minutes > 0) return `${minutes}m ${secs}s`;
    return `${secs}s`;
  };
}
if (process.argv.length !== 6) {
  console.log('Invalid argv');
  process.exit(1);
}
const flag1 = process.argv[2];
let torrentFilePath = process.argv[3];
const flag2 = process.argv[4];
let saveDir = process.argv[5];
let savedLoc;
if (flag1 !== '--torrent-file-path' || flag2 !== '--save-dir') {
  process.exit(1);
}
if (!fs.existsSync(torrentFilePath) || !isDirSync(saveDir)) {
  process.exit(1);
}
torrentFilePath = path.resolve(torrentFilePath);
saveDir = path.resolve(saveDir);
console.log('Torrent file path: ', torrentFilePath);
console.log('Save directory: ', saveDir);
const calcElapsedTime = elapsedTime();
const swarmManager = new SwarmManager();
const parsed1 = parse(fs.readFileSync(torrentFilePath));
savedLoc = path.join(saveDir, parsed1.name);
const id1 = swarmManager.add({
  parsed: parsed1,
  outputPath: savedLoc,
  mode: 'leeching',
  stratery: 'rarest-first',
});

swarmManager.onSession(id1, 'logging', (message) => {
  loggerWorker.send({ event: 'logging', data: { message } });
});

swarmManager.onSession(id1, 'done', () => {
  loggerWorker.send({ event: 'done', data: {} });
  process.emit('SIGINT');
});

swarmManager.onSession(id1, 'piece', (pieceIndex, remainingPieces) => {
  loggerWorker.send({ event: 'piece', data: { pieceIndex, remainingPieces } });
});

swarmManager.onSession(id1, 'failed', (pieceIndex) => {
  loggerWorker.send({ event: 'failed', data: { pieceIndex } });
});

swarmManager.onSession(id1, 'peer:add', (ip, port, peerId) => {
  loggerWorker.send({ event: 'peer:add', data: { ip, port, peerId } });
});

swarmManager.onSession(id1, 'peer:drop', (ip, port, peerId) => {
  loggerWorker.send({ event: 'peer:drop', data: { ip, port, peerId } });

  if (!net.isIPv4(ip) || !(Number.isInteger(port) && port >= 1 && port <= 65535)) return;
  const to = setTimeout(() => {
    swarmManager.connect(id1, ip, port);
    clearTimeout(to);
  }, PEER_RETRY_TIME);
});

swarmManager.onSession(id1, 'progress', (stats) => {
  loggerWorker.send({ event: 'progress', data: { stats } });
});

process.on('SIGINT', async () => {
  const stats = swarmManager.getAllStats();
  const elapsed = calcElapsedTime();
  loggerWorker.send({ event: 'shutdown', data: { stats, elapsed } });
  swarmManager.shutdown();
  console.log('Torrent file path: ', torrentFilePath);
  console.log('Saved location: ', savedLoc);
  setTimeout(() => process.exit(0), 100);
});
