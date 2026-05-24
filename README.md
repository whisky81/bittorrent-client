# Tiny BitTorrent Client

A custom BitTorrent client built with Electron, featuring peer wire protocol implementation using `bittorrent-protocol` and a custom storage layer inspired by WebTorrent.

## Features

- **Peer Wire Protocol**: Uses [`bittorrent-protocol`](https://github.com/webtorrent/bittorrent-protocol) for BitTorrent peer communication
- **Custom Storage Layer**: Inspired by WebTorrent's storage system for efficient piece-based file I/O
- **Two Download Modes**:
  - **Rarest First**: Prioritizes downloading the least available pieces first (recommended for faster overall downloads)
  - **Sequential**: Downloads pieces in order (enables streaming while downloading)
- **VLC Streaming**: In sequential mode, get a URL to stream video directly in VLC Media Player
- **Electron UI**: Modern, responsive desktop interface

## Installation

```bash
npm install
```

## Usage

### Development Mode

```bash
npm run dev
```

### Production Mode

```bash
npm start
```

## How to Use

1. **Launch the application**
2. **Select a .torrent file** using the file picker
3. **Choose download mode**:
   - **Rarest First**: Best for completing downloads quickly
   - **Sequential**: Required for streaming support
4. **Click "Load Torrent"** to start downloading
5. **For streaming** (sequential mode only):
   - Copy the streaming URL shown
   - Open VLC Media Player
   - Go to Media → Open Network Stream
   - Paste the URL and click Play

## Architecture

### Core Components

#### 1. Storage Layer (`src/storage.js`)
Custom storage implementation inspired by WebTorrent that:
- Manages piece-based file I/O
- Handles multi-file torrents
- Supports reading/writing at piece granularity
- Provides file handle caching for performance

#### 2. Peer Manager (`src/peer.js`)
Peer connection manager using `bittorrent-protocol`:
- Implements BitTorrent peer wire protocol
- Handles handshake, choke/unchoke, have/bitfield messages
- Supports both rarest-first and sequential piece selection
- Tracks piece availability across peers

#### 3. Client (`src/client.js`)
Main torrent client that combines:
- Peer discovery via trackers (HTTP & UDP)
- Peer management and connection handling
- Streaming server for VLC integration
- Progress tracking and events

## Project Structure

```
/workspace
├── main.js                 # Electron main process
├── package.json            # Dependencies and scripts
├── src/
│   ├── client.js          # Main torrent client
│   ├── peer.js            # Peer connection manager
│   ├── storage.js         # Custom storage layer
│   └── renderer.html      # Electron UI
└── packages/
    ├── parse-torrent/     # Torrent file parser
    └── torrent-discovery/ # Tracker client
```

## VLC Streaming

When using **sequential mode**, the client starts an HTTP server that supports:
- Range requests for seeking
- Proper content-type headers
- Progressive streaming

**To stream in VLC:**
1. Start download in sequential mode
2. Wait for the streaming URL to appear
3. Copy the URL (e.g., `http://localhost:8989/stream?file=0`)
4. In VLC: Media → Open Network Stream → Paste URL → Play

## License

ISC

## Acknowledgments

- [bittorrent-protocol](https://github.com/webtorrent/bittorrent-protocol) - Peer wire protocol implementation
- [WebTorrent](https://github.com/webtorrent/webtorrent) - Inspiration for storage layer design
