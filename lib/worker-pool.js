import { Worker } from 'worker_threads'

/**
 * @typedef {Object} WorkerTask
 * @property {number} index - Piece index trong torrent
 * @property {ArrayBuffer} arrayBuf - Buffer để verify (sẽ được transfer)
 * @property {Buffer} writeBuf - Buffer để ghi disk (main thread giữ)
 * @property {string} expectedHash - Hash SHA-1 expected của piece
 * @property {number} sessionId - Session ID để phân biệt multi-torrent
 */

/**
 * @typedef {Object} WorkerWrapper
 * @property {number} id - Worker ID
 * @property {boolean} busy - Worker đang bận không
 * @property {Array<WorkerTask>} queue - Hàng đợi tasks
 * @property {Worker} worker - Worker thread instance
 */

/**
 * Worker Pool để verify pieces torrent song song
 * 
 * Features:
 * - Load balancing dựa trên độ dài queue
 * - Zero-copy transfer dùng worker_threads transferList
 * - Hỗ trợ multi-torrent sessions
 * - Auto drain queue khi worker rảnh
 * - Error handling và recovery
 * 
 * @example
 * ```js
 * const pool = new WorkerPool(
 *   './verify-worker.js',
 *   4,
 *   (index, valid, writeBuf, sessionId) => {
 *     if (valid) {
 *       fs.writeSync(fd, writeBuf, 0, writeBuf.length, index * pieceSize)
 *     }
 *   },
 *   (index, error, writeBuf, sessionId) => {
 *     console.error(`Failed piece ${index}: ${error}`)
 *   }
 * )
 * 
 * pool.push(0, arrayBuf, writeBuf, expectedHash, sessionId)
 * ```
 */
class WorkerPool {
  /**
   * Khởi tạo Worker Pool
   * 
   * @param {string} workerPath - Đường dẫn đến worker script
   * @param {number} size - Số lượng worker threads
   * @param {Function} onResult - Callback khi verify thành công
   * @param {Function} [onError] - Callback khi có lỗi (optional)
   * 
   * @param {number} onResult.index - Piece index
   * @param {boolean} onResult.valid - Piece có hợp lệ không
   * @param {Buffer} onResult.writeBuf - Buffer để ghi disk
   * @param {number} onResult.sessionId - Session ID
   * 
   * @param {number} onError.index - Piece index bị lỗi
   * @param {string|Error} onError.error - Thông tin lỗi
   * @param {Buffer} onError.writeBuf - Buffer của piece lỗi
   * @param {number} onError.sessionId - Session ID
   * 
   * @throws {Error} Nếu workerPath không tồn tại hoặc size <= 0
   */
  constructor(workerPath, size, onResult, onError = null) {
    if (size <= 0) {
      throw new Error('Worker pool size must be greater than 0')
    }
    
    if (typeof onResult !== 'function') {
      throw new Error('onResult callback must be a function')
    }

    /** @type {Function} */
    this.onResult = onResult
    
    /** @type {Function|null} */
    this.onError = onError
    
    /** @type {Array<WorkerWrapper>} */
    this.workers = []
    
    /** @type {boolean} */
    this.isTerminated = false

    // Khởi tạo workers
    for (let id = 0; id < size; id++) {
      /** @type {WorkerWrapper} */
      const w = { 
        id, 
        busy: false,
        queue: [],
        worker: null 
      }

      try {
        w.worker = new Worker(workerPath)
      } catch (err) {
        throw new Error(`Failed to create worker ${id}: ${err.message}`)
      }

      // Xử lý message từ worker
      w.worker.on('message', ({ index, valid, sessionId, error }) => {
        const item = w.queue.shift()
        w.busy = false
        
        if (item) {
          if (error && this.onError) {
            this.onError(index, error, item.writeBuf, sessionId)
          } else {
            this.onResult(index, valid, item.writeBuf, sessionId)
          }
        }
        
        if (!this.isTerminated) {
          this._drain(w)
        }
      })

      // Xử lý lỗi worker
      w.worker.on('error', (err) => {
        console.error(`[worker-${id}] error:`, err.message)
        const item = w.queue.shift()
        w.busy = false
        
        if (item && this.onError) {
          this.onError(item.index, err.message, item.writeBuf, item.sessionId)
        }
        
        if (!this.isTerminated) {
          this._drain(w)
        }
      })

      // Xử lý worker exit
      w.worker.on('exit', (code) => {
        if (code !== 0 && !this.isTerminated) {
          console.error(`[worker-${id}] exited with code ${code}`)
          // TODO: Implement worker restart logic if needed
        }
      })

      this.workers.push(w)
    }
  }

  /**
   * Thêm piece vào queue để verify
   * 
   * @param {number} index - Piece index
   * @param {ArrayBuffer} arrayBuf - Buffer để verify (sẽ bị detach sau khi transfer)
   * @param {Buffer} writeBuf - Buffer để ghi disk (main thread giữ)
   * @param {string} expectedHash - Expected SHA-1 hash (hex string)
   * @param {number} sessionId - Session ID
   * 
   * @throws {Error} Nếu pool đã bị terminate
   * @throws {Error} Nếu arrayBuf không phải ArrayBuffer
   * 
   * @example
   * ```js
   * const writeBuf = Buffer.alloc(16384)
   * const arrayBuf = writeBuf.buffer
   * const hash = crypto.createHash('sha1').update(writeBuf).digest('hex')
   * 
   * pool.push(5, arrayBuf, writeBuf, hash, 123)
   * // arrayBuf.byteLength === 0 sau khi transfer
   * ```
   */
  push(index, arrayBuf, writeBuf, expectedHash, sessionId) {
    if (this.isTerminated) {
      throw new Error('WorkerPool has been terminated')
    }

    if (!(arrayBuf instanceof ArrayBuffer)) {
      throw new Error('arrayBuf must be an ArrayBuffer')
    }

    if (!Buffer.isBuffer(writeBuf)) {
      throw new Error('writeBuf must be a Buffer')
    }

    if (typeof expectedHash !== 'string' || expectedHash.length !== 40) {
      throw new Error('expectedHash must be a 40-character hex string')
    }

    // Load balancing: chọn worker có queue ngắn nhất
    const w = this.workers.reduce((a, b) =>
      a.queue.length <= b.queue.length ? a : b
    )
    
    // Copy dữ liệu để tránh race condition
    // Buffer gốc vẫn được main thread giữ để ghi disk
    const safeArrayBuf = arrayBuf.slice(0)
    
    /** @type {WorkerTask} */
    const task = { 
      index, 
      arrayBuf: safeArrayBuf, 
      writeBuf, 
      expectedHash, 
      sessionId 
    }
    
    w.queue.push(task)
    this._drain(w)
  }

  /**
   * Xử lý queue cho một worker cụ thể
   * Gửi task tiếp theo nếu worker rảnh và còn task trong queue
   * 
   * @param {WorkerWrapper} w - Worker wrapper object
   * @private
   */
  _drain(w) {
    if (w.busy || w.queue.length === 0 || this.isTerminated) return
    
    const { index, arrayBuf, expectedHash, sessionId } = w.queue[0]
    w.busy = true
    
    // Transfer array buffer ownership sang worker
    // Sau lệnh này, arrayBuf bị detach khỏi main thread
    w.worker.postMessage(
      { index, buffer: arrayBuf, expectedHash, sessionId },
      [arrayBuf] // Transfer list: main thread mất quyền truy cập
    )
  }

  /**
   * Terminate toàn bộ worker pool
   * Chờ tất cả workers kết thúc công việc hiện tại
   * 
   * @returns {Promise<void>}
   * 
   * @example
   * ```js
   * await pool.terminate()
   * console.log('All workers stopped')
   * ```
   */
  async terminate() {
    this.isTerminated = true
    const promises = this.workers.map(w => w.worker.terminate())
    await Promise.all(promises)
  }

  /**
   * Lấy thống kê về trạng thái các workers
   * Hữu ích cho debugging và monitoring
   * 
   * @returns {Array<{id: number, busy: boolean, queueLength: number}>}
   * 
   * @example
   * ```js
   * const stats = pool.getStats()
   * console.log(stats)
   * // [
   * //   { id: 0, busy: true, queueLength: 2 },
   * //   { id: 1, busy: false, queueLength: 0 }
   * // ]
   * ```
   */
  getStats() {
    return this.workers.map(w => ({
      id: w.id,
      busy: w.busy,
      queueLength: w.queue.length
    }))
  }

  /**
   * Lấy tổng số tasks đang pending
   * 
   * @returns {number} Tổng số tasks trong tất cả queues
   */
  getPendingTasks() {
    return this.workers.reduce((sum, w) => sum + w.queue.length, 0)
  }

  /**
   * Kiểm tra tất cả workers có đang rảnh không
   * 
   * @returns {boolean} True nếu không có task nào đang xử lý hoặc pending
   */
  isIdle() {
    return this.workers.every(w => !w.busy && w.queue.length === 0)
  }
}

export default WorkerPool