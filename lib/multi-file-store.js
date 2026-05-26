import fs from 'fs'
import path from 'path'

class MultiFileStore {
    /**
     * @param {number} pieceLength - Kích thước mỗi piece (bytes)
     * @param {Array<{path: string, offset: number, length: number}>} files
     *          Danh sách file đã sort theo offset và không chồng lấn.
     *          Mỗi file: path, offset (byte bắt đầu trong torrent), length (bytes)
     */
    constructor(pieceLength, files) {
        if (typeof pieceLength !== 'number' || pieceLength <= 0) {
            throw new Error('pieceLength must be a positive integer')
        }
        if (!Array.isArray(files) || files.length === 0) {
            throw new Error('files must be a non-empty array')
        }

        // Sort files by offset to ensure sequential access
        this.files = [...files].sort((a, b) => a.offset - b.offset)
        this.pieceLength = pieceLength

        // Validate no gaps/overlaps (optional but recommended)
        let expectedOffset = 0
        for (const file of this.files) {
            if (file.offset !== expectedOffset) {
                throw new Error(`File offset mismatch: expected ${expectedOffset}, got ${file.offset}`)
            }
            if (typeof file.length !== 'number' || file.length <= 0) {
                throw new Error(`Invalid file length for ${file.path}`)
            }
            expectedOffset += file.length
        }

        this.fds = []
        try {
            for (const file of this.files) {
                // Create directory if needed
                const dir = path.dirname(file.path)
                if (!fs.existsSync(dir)) {
                    fs.mkdirSync(dir, { recursive: true })
                }
                // Open file descriptor (create if not exists)
                const fd = fs.openSync(file.path, fs.existsSync(file.path) ? 'r+' : 'w+')
                this.fds.push(fd)
            }
        } catch (err) {
            // Clean up already opened fds
            this.close()
            throw new Error(`Failed to open files: ${err.message}`)
        }
    }

    /**
     * Đọc `length` bytes từ `torrentOffset` (có thể trải qua nhiều file)
     * @param {number} torrentOffset
     * @param {number} length
     * @returns {Buffer}
     */
    _readAt(torrentOffset, length) {
        if (torrentOffset < 0) throw new Error('torrentOffset must be non-negative')
        if (length <= 0) throw new Error('length must be positive');

        const buf = Buffer.allocUnsafe(length)
        let bufOffset = 0

        let remaining = length
        let pos = torrentOffset

        for (let i = 0; i < this.files.length && remaining > 0; i++) {
            const file = this.files[i]
            const fileEnd = file.offset + file.length

            if (pos >= fileEnd) continue          // chưa đến file này
            if (pos < file.offset) break          // đã qua (gap, không hợp lệ)

            const fileRelOffset = pos - file.offset
            const canRead = Math.min(fileEnd - pos, remaining)

            fs.readSync(this.fds[i], buf, bufOffset, canRead, fileRelOffset)
            bufOffset += canRead
            remaining -= canRead
            pos += canRead
        }

        if (remaining > 0) {
            throw new Error(`Read beyond torrent boundaries: requested ${length}, got ${length - remaining}`)
        }
        return buf
    }

    /**
     * Ghi `data` vào `torrentOffset` (có thể trải qua nhiều file)
     * @param {number} torrentOffset
     * @param {Buffer} data
     */
    _writeAt(torrentOffset, data) {
        if (torrentOffset < 0) throw new Error('torrentOffset must be non-negative')
        if (!Buffer.isBuffer(data)) throw new Error('data must be a Buffer')

        let remaining = data.length
        let dataOffset = 0
        let pos = torrentOffset

        for (let i = 0; i < this.files.length && remaining > 0; i++) {
            const file = this.files[i]
            const fileEnd = file.offset + file.length

            if (pos >= fileEnd) continue
            if (pos < file.offset) break

            const fileRelOffset = pos - file.offset
            const canWrite = Math.min(fileEnd - pos, remaining)

            fs.writeSync(this.fds[i], data, dataOffset, canWrite, fileRelOffset)
            dataOffset += canWrite
            remaining -= canWrite
            pos += canWrite
        }

        if (remaining > 0) {
            throw new Error(`Write beyond torrent boundaries: attempted ${data.length}, written ${data.length - remaining}`)
        }
    }

    /**
     * Ghi toàn bộ piece vào file
     * @param {number} pieceIndex
     * @param {Buffer} buf
     */
    write(pieceIndex, buf) {
        const offset = pieceIndex * this.pieceLength
        this._writeAt(offset, buf)
    }

    /**
     * Đọc một block từ piece
     * @param {number} pieceIndex
     * @param {number} begin
     * @param {number} length
     * @returns {Buffer}
     */
    read(pieceIndex, begin, length) {
        const offset = pieceIndex * this.pieceLength + begin
        return this._readAt(offset, length)
    }

    /**
     * Đọc bytes từ một file cụ thể (dùng cho HTTP streaming)
     * @param {Object} file - file entry (từ files array)
     * @param {number} fileOffset - offset trong file
     * @param {number} length
     * @returns {Buffer}
     */
    readFileRange(file, fileOffset, length) {
        return this._readAt(file.offset + fileOffset, length)
    }

    /**
     * Đóng tất cả file descriptors
     */
    close() {
        if (this.fds) {
            for (const fd of this.fds) {
                try {
                    fs.closeSync(fd)
                } catch (err) {
                    // ignore
                }
            }
            this.fds = []
        }
    }
}

export default MultiFileStore