import { parentPort } from 'worker_threads'
import crypto from 'node:crypto'

if (!parentPort) {
  throw new Error('verify-worker.js phải chạy như Worker thread, không phải main process')
}

/**
 * Worker thread để verify SHA-1 hash của piece
 * 
 * Nhận message từ main thread:
 * @typedef {Object} VerifyMessage
 * @property {number} index - Piece index
 * @property {ArrayBuffer} buffer - Buffer cần verify
 * @property {string} expectedHash - Expected SHA-1 hash
 * @property {number} sessionId - Session ID
 * 
 * Gửi message về main thread:
 * @typedef {Object} VerifyResult
 * @property {number} index - Piece index
 * @property {boolean} valid - Hash có khớp không
 * @property {number} sessionId - Session ID
 * @property {string} [error] - Error message nếu có lỗi
 * 
 * @example
 * ```js
 * // Worker sẽ tự động nhận message và trả kết quả
 * // Không cần code thêm ở main thread
 * ```
 */

/**
 * Xử lý message từ main thread
 * 
 * @param {VerifyMessage} message
 * @param {number} message.index
 * @param {ArrayBuffer} message.buffer
 * @param {string} message.expectedHash
 * @param {number} message.sessionId
 */
parentPort.on('message', ({ index, buffer, expectedHash, sessionId }) => {
  try {
    // Validate input
    if (!buffer) {
      throw new Error('Missing buffer')
    }
    
    if (!expectedHash || typeof expectedHash !== 'string') {
      throw new Error('Invalid expectedHash')
    }
    
    if (expectedHash.length !== 40) {
      throw new Error(`Invalid hash length: ${expectedHash.length}, expected 40`)
    }

    // Tính toán SHA-1 hash
    const hash = crypto
      .createHash('sha1')
      .update(Buffer.from(buffer))
      .digest('hex')

    const valid = hash === expectedHash
    
    // Gửi kết quả về main thread
    parentPort.postMessage({
      index,
      valid,
      sessionId
    })
  } catch (error) {
    // Gửi lỗi về main thread
    parentPort.postMessage({
      index,
      valid: false,
      sessionId,
      error: error.message
    })
  }
})

/**
 * Export empty object để ES module hoạt động đúng
 * Worker sẽ chạy code trên khi được khởi tạo
 */
export {}