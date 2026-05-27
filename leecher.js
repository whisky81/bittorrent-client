
import parse from "parse-torrent";
import fs from "fs";
import SwarmManager from './lib/torrent-manager.js'
const TORRENT_FILE_PATH = 'D:\\Downloads\\731B5115366AF8C1D56C87EFFB3889F5891402DB.torrent'

const parsed1 = parse(fs.readFileSync(TORRENT_FILE_PATH))
const swarmManager = new SwarmManager({
    listenPort: 6882
})

// set up leeching 
const id1 = swarmManager.add({
    parsed: parsed1,
    outputPath: "D:\\" + parsed1.files[0].name,
    mode: 'leeching',
    stratery: "rarest-first"
})

swarmManager.connect(id1, '127.0.0.1', 6881)

process.on('SIGINT', async () => {
    console.log('[graceful shutdown]')
    swarmManager.shutdown()
    process.exit(0)
})
