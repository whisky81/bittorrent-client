// ─────────────────────────────────────────────
// single-file torrent
// ─────────────────────────────────────────────
import fs from 'fs'
 
class SignleFileStore {
  constructor(filePath, pieceLength, lastPieceLength = null, totalPieces = null) {
    if (typeof pieceLength !== "number" || isNaN(pieceLength)) throw new Error('piece length must be a number')
    if (pieceLength <= 0) throw new Error('piece length must be positive')
    this.pieceLength = pieceLength
    this.fd = fs.openSync(filePath, fs.existsSync(filePath) ? 'r+' : 'w+')
    this.lastPieceLength = lastPieceLength
    this.totalPieces = totalPieces
  }
 
  write(pieceIndex, buf) {
    fs.writeSync(this.fd, buf, 0, buf.length, pieceIndex * this.pieceLength)
  }
 
  // đọc block [begin, begin+length) trong piece pieceIndex
  read(pieceIndex, begin, length) {
    if (pieceIndex < 0) throw new Error('piece index must be non-negative')
    if (begin < 0) throw new Error('begin must be non-negative')
    if (length <= 0) throw new Error('length must be positive')
    
    const curr = pieceIndex === this.totalPieces - 1 ? this.lastPieceLength : this.pieceLength

    const safeLength = Math.min(length, curr - begin)

    const buf = Buffer.allocUnsafe(safeLength)
    fs.readSync(this.fd, buf, 0, safeLength, pieceIndex * this.pieceLength + begin)
    return buf
  }
 
  // đọc byte range tuyệt đối trong file
  readRaw(offset, length) {
    if (offset < 0) throw new Error('offset must be non-negative')
    if (length <= 0) throw new Error('length must be positive')
    const buf = Buffer.allocUnsafe(length)
    fs.readSync(this.fd, buf, 0, length, offset)
    return buf
  }
 
  close() { 
    if (this.fd !== null)
    fs.closeSync(this.fd)
      this.fd = null 
   }
}

export default SignleFileStore