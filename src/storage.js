import fs from 'fs'
import path from 'path'
import { EventEmitter } from 'events'
import crypto from 'crypto'

/**
 * Custom storage layer inspired by WebTorrent's storage system.
 * Handles piece-based file I/O for torrent downloads.
 */
export class Storage extends EventEmitter {
  constructor (torrent, downloadPath) {
    super()
    this.torrent = torrent
    this.downloadPath = downloadPath
    this.files = []
    this.fdCache = new Map() // Cache file descriptors for performance
    this.closed = false
  }

  /**
   * Initialize storage - create directories and open files
   */
  async init () {
    const { files, pieceLength, length } = this.torrent
    
    // Create download directory
    await fs.promises.mkdir(this.downloadPath, { recursive: true })

    // Initialize file handles
    this.files = await Promise.all(files.map(async (file, index) => {
      const filePath = path.join(this.downloadPath, file.path)
      const dirPath = path.dirname(filePath)
      
      // Create subdirectories if needed
      await fs.promises.mkdir(dirPath, { recursive: true })
      
      // Open or create file
      const fd = await fs.promises.open(filePath, 'w')
      
      // Pre-allocate file size if it's larger than current size
      try {
        const stats = await fd.stat()
        if (stats.size < file.length) {
          await fd.truncate(file.length)
        }
      } catch (err) {
        // File might not exist yet, that's ok
      }

      return {
        index,
        path: filePath,
        relativePath: file.path,
        name: file.name,
        length: file.length,
        offset: file.offset,
        fd
      }
    }))

    this.emit('ready')
  }

  /**
   * Get file by piece index
   */
  getFileByPiece (pieceIndex) {
    const { pieceLength } = this.torrent
    const offset = pieceIndex * pieceLength
    
    for (const file of this.files) {
      const fileEnd = file.offset + file.length
      if (offset < fileEnd && offset + pieceLength > file.offset) {
        return file
      }
    }
    return null
  }

  /**
   * Get all files that a piece spans
   */
  getFilesForPiece (pieceIndex) {
    const { pieceLength } = this.torrent
    const pieceOffset = pieceIndex * pieceLength
    const pieceEnd = pieceOffset + pieceLength
    
    return this.files.filter(file => {
      const fileStart = file.offset
      const fileEnd = file.offset + file.length
      return pieceEnd > fileStart && pieceOffset < fileEnd
    })
  }

  /**
   * Read data from a specific piece
   * @param {number} pieceIndex - Index of the piece to read
   * @param {number} offset - Offset within the piece
   * @param {number} length - Number of bytes to read
   * @returns {Promise<Buffer>}
   */
  async read (pieceIndex, offset, length) {
    if (this.closed) throw new Error('Storage is closed')
    
    const { pieceLength } = this.torrent
    const absoluteOffset = pieceIndex * pieceLength + offset
    const buffer = Buffer.alloc(length)
    
    let bytesRead = 0
    while (bytesRead < length) {
      const file = this.getFileByAbsoluteOffset(absoluteOffset + bytesRead)
      if (!file) break
      
      const fileRelativeOffset = absoluteOffset + bytesRead - file.offset
      const bytesToRead = Math.min(length - bytesRead, file.length - fileRelativeOffset)
      
      const readBuffer = Buffer.alloc(bytesToRead)
      await file.fd.read(readBuffer, 0, bytesToRead, fileRelativeOffset)
      readBuffer.copy(buffer, bytesRead)
      
      bytesRead += bytesToRead
    }
    
    return buffer.slice(0, bytesRead)
  }

  /**
   * Write data to a specific piece
   * @param {number} pieceIndex - Index of the piece to write
   * @param {number} offset - Offset within the piece
   * @param {Buffer} buffer - Data to write
   * @returns {Promise<number>} Bytes written
   */
  async write (pieceIndex, offset, buffer) {
    if (this.closed) throw new Error('Storage is closed')
    
    const { pieceLength } = this.torrent
    const absoluteOffset = pieceIndex * pieceLength + offset
    let bytesWritten = 0
    
    while (bytesWritten < buffer.length) {
      const file = this.getFileByAbsoluteOffset(absoluteOffset + bytesWritten)
      if (!file) break
      
      const fileRelativeOffset = absoluteOffset + bytesWritten - file.offset
      const bytesToWrite = Math.min(
        buffer.length - bytesWritten,
        file.length - fileRelativeOffset
      )
      
      const writeBuffer = buffer.slice(bytesWritten, bytesWritten + bytesToWrite)
      await file.fd.write(writeBuffer, 0, bytesToWrite, fileRelativeOffset)
      
      bytesWritten += bytesToWrite
    }
    
    return bytesWritten
  }

  /**
   * Get file by absolute offset in the torrent
   */
  getFileByAbsoluteOffset (offset) {
    for (const file of this.files) {
      if (offset >= file.offset && offset < file.offset + file.length) {
        return file
      }
    }
    return null
  }

  /**
   * Close all file handles
   */
  async close () {
    if (this.closed) return
    
    this.closed = true
    await Promise.all(this.files.map(async file => {
      try {
        await file.fd.close()
      } catch (err) {
        // Ignore errors during close
      }
    }))
    
    this.fdCache.clear()
    this.emit('close')
  }

  /**
   * Get streaming URL for VLC (for sequential mode)
   * Returns a local HTTP server URL or file path
   */
  getStreamingURL (fileIndex = 0) {
    const file = this.files[fileIndex]
    if (!file) return null
    
    // Return the local file path for VLC to stream
    // In a real implementation, you'd start an HTTP server
    return `file://${file.path}`
  }

  /**
   * Check if a piece is fully downloaded
   */
  async verifyPiece (pieceIndex) {
    const { pieces, pieceLength, lastPieceLength } = this.torrent
    const isLastPiece = pieceIndex === pieces.length - 1
    const length = isLastPiece ? lastPieceLength : pieceLength
    
    const data = await this.read(pieceIndex, 0, length)
    if (data.length !== length) return false
    
    const hash = crypto.createHash('sha1').update(data).digest('hex')
    
    return hash === pieces[pieceIndex]
  }
}

export default Storage
