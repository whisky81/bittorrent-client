import { app, BrowserWindow, ipcMain } from 'electron'
import path from 'path'
import { fileURLToPath } from 'url'
import fs from 'fs'
import { TorrentClient } from './src/client.js'

const __filename = fileURLToPath(import.meta.url)
const __dirname = path.dirname(__filename)

let mainWindow
let client = null

function createWindow () {
  mainWindow = new BrowserWindow({
    width: 1000,
    height: 800,
    webPreferences: {
      nodeIntegration: true,
      contextIsolation: false
    },
    titleBarStyle: 'hiddenInset',
    backgroundColor: '#1a1a2e'
  })

  mainWindow.loadFile(path.join(__dirname, 'src', 'renderer.html'))
  
  // Open DevTools in development
  if (process.env.NODE_ENV === 'development') {
    mainWindow.webContents.openDevTools()
  }

  mainWindow.on('closed', () => {
    mainWindow = null
  })
}

// IPC Handlers
ipcMain.on('load-torrent', async (event, { buffer, strategy }) => {
  try {
    // Stop existing client if any
    if (client) {
      await client.stop()
    }

    // Create new client
    client = new TorrentClient({
      strategy: strategy || 'rarest',
      downloadPath: path.join(app.getPath('downloads'), 'torrents'),
      port: 6881
    })

    // Setup event listeners
    client.on('ready', (data) => {
      event.sender.send('torrent-ready', data)
      
      // Start progress updates
      setInterval(() => {
        if (client && !client.complete) {
          const progress = client.getProgress()
          event.sender.send('progress-update', progress)
        }
      }, 1000)
    })

    client.on('piece-downloaded', (pieceIndex) => {
      event.sender.send('piece-downloaded', pieceIndex)
      
      // Update downloaded bytes
      const pieceLength = client.torrent.pieceLength
      const isLastPiece = pieceIndex === client.torrent.pieces.length - 1
      const bytes = isLastPiece ? client.torrent.lastPieceLength : pieceLength
      client.downloaded += bytes
    })

    client.on('peer-connected', (peerAddr) => {
      event.sender.send('peer-connected', peerAddr)
    })

    client.on('complete', () => {
      event.sender.send('download-complete')
    })

    client.on('stream-ready', (url) => {
      // Construct full streaming URL
      const streamUrl = `http://localhost:8989/stream?file=0`
      event.sender.send('stream-ready', streamUrl)
    })

    client.on('warning', (err) => {
      event.sender.send('warning', err.message)
    })

    client.on('peer-error', (peerAddr, err) => {
      event.sender.send('warning', `Peer ${peerAddr}: ${err.message}`)
    })

    // Load torrent
    await client.load(buffer)
    
  } catch (err) {
    event.sender.send('warning', `Error loading torrent: ${err.message}`)
  }
})

// App lifecycle
app.whenReady().then(createWindow)

app.on('window-all-closed', async () => {
  // Cleanup client before quitting
  if (client) {
    await client.stop()
  }
  
  if (process.platform !== 'darwin') {
    app.quit()
  }
})

app.on('activate', () => {
  if (mainWindow === null) {
    createWindow()
  }
})

// Handle quit
app.on('will-quit', async () => {
  if (client) {
    await client.stop()
  }
})
