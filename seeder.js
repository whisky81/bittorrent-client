
import parse from "parse-torrent";
import fs from "fs";
import SwarmManager from './lib/torrent-manager.js'
const TORRENT_FILE_PATH = 'D:\\Downloads\\731B5115366AF8C1D56C87EFFB3889F5891402DB.torrent'
const SEED_FILE_PATH = 'D:\\seed.mkv'

const parsed1 = parse(fs.readFileSync(TORRENT_FILE_PATH))
const swarmManager = new SwarmManager()

// set up seeding 
const id1 = swarmManager.add({
    parsed: parsed1,
    outputPath: SEED_FILE_PATH,
    mode: 'seeding',
    stratery: "rarest-first"
})

process.on('SIGINT', async () => {
    console.log('[graceful shutdown]')
    swarmManager.shutdown()
    process.exit(0)
})
