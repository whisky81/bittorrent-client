import test from 'node:test'
import assert from 'node:assert'
import crypto from 'node:crypto'
import os from 'node:os'

import WorkerPool from '../lib/worker-pool.js'
const task = 'D:/fbc/lib/verify-worker.js'

/**
 * Số lượng worker threads sử dụng trong tests
 * Tối đa 4 threads để tránh overload CI environment
 */
const WORKERS = Math.max(1, Math.min(os.availableParallelism() - 1, 4))

/**
 * Test suite cho WorkerPool
 * 
 * Các test cases:
 * 1. Verify cơ bản với nhiều pieces
 * 2. Xử lý invalid hash
 * 3. Empty buffer
 * 4. Large buffer (10MB)
 * 5. Concurrent multi-sessions
 * 6. Pool termination behavior
 * 7. Load balancing
 */

test('worker pool verifies pieces correctly', async () => {
  const TOTAL = 20
  let completed = 0
  const results = []

  await new Promise((resolve, reject) => {
    const pool = new WorkerPool(
      task,
      WORKERS,
      (index, valid, writeBuf, sessionId) => {
        results.push({ index, valid, len: writeBuf.length, sessionId })
        if (++completed === TOTAL) {
          pool.terminate().then(resolve).catch(reject)
        }
      },
      (index, error, writeBuf, sessionId) => {
        reject(new Error(`Worker error for index ${index}: ${error}`))
      }
    )

    for (let i = 0; i < TOTAL; i++) {
      const writeBuf = crypto.randomBytes(1024)
      const expectedHash = crypto
        .createHash('sha1')
        .update(writeBuf)
        .digest('hex')

      // Tạo ArrayBuffer riêng biệt
      const arrayBuf = new Uint8Array(writeBuf).buffer

      pool.push(i, arrayBuf, writeBuf, expectedHash, 123)
    }

    setTimeout(() => reject(new Error('test timeout')), 1000)
  })

  assert.strictEqual(results.length, TOTAL)
  results.forEach(r => {
    assert.strictEqual(r.valid, true)
    assert.strictEqual(r.len, 1024)
    assert.strictEqual(r.sessionId, 123)
  })
})

test('handles invalid hash correctly', async () => {
  let completed = 0
  
  await new Promise((resolve, reject) => {
    const pool = new WorkerPool(
      task,
      1,
      (index, valid, writeBuf, sessionId) => {
        assert.strictEqual(valid, false)
        if (++completed === 1) {
          pool.terminate().then(resolve).catch(reject)
        }
      }
    )

    const writeBuf = crypto.randomBytes(1024)
    const invalidHash = crypto.createHash('sha1').update(Buffer.from(crypto.randomBytes(1024))).digest('hex')
    const arrayBuf = new Uint8Array(writeBuf).buffer

    pool.push(0, arrayBuf, writeBuf, invalidHash, 123)
    
    setTimeout(() => reject(new Error('test timeout')), 1000)
  })
})

test('handles empty buffer', async () => {
  await new Promise((resolve, reject) => {
    const pool = new WorkerPool(
      task,
      1,
      (index, valid, writeBuf, sessionId) => {
        assert.strictEqual(valid, true)
        assert.strictEqual(writeBuf.length, 0)
        pool.terminate().then(resolve).catch(reject)
      }
    )

    const writeBuf = Buffer.alloc(0)
    const expectedHash = crypto.createHash('sha1').update('').digest('hex')
    const arrayBuf = new ArrayBuffer(0)

    pool.push(0, arrayBuf, writeBuf, expectedHash, 123)
    
    setTimeout(() => reject(new Error('test timeout')), 1000)
  })
})

test('handles large buffer', async () => {
  const LARGE_SIZE = 10 * 1024 * 1024 // 10MB
  
  await new Promise((resolve, reject) => {
    const pool = new WorkerPool(
      task,
      1,
      (index, valid, writeBuf, sessionId) => {
        assert.strictEqual(valid, true)
        assert.strictEqual(writeBuf.length, LARGE_SIZE)
        pool.terminate().then(resolve).catch(reject)
      }
    )

    const writeBuf = crypto.randomBytes(LARGE_SIZE)
    const expectedHash = crypto.createHash('sha1').update(writeBuf).digest('hex')
    const arrayBuf = new Uint8Array(writeBuf).buffer

    pool.push(0, arrayBuf, writeBuf, expectedHash, 123)
    
    setTimeout(() => reject(new Error('test timeout')), 1000)
  })
})

test('handles concurrent multiple sessions', async () => {
  const SESSIONS = 3
  const PIECES_PER_SESSION = 10
  let totalCompleted = 0
  const sessionResults = {}

  await new Promise((resolve, reject) => {
    const pool = new WorkerPool(
      task,
      WORKERS,
      (index, valid, writeBuf, sessionId) => {
        if (!sessionResults[sessionId]) {
          sessionResults[sessionId] = []
        }
        sessionResults[sessionId].push({ index, valid })
        
        if (++totalCompleted === SESSIONS * PIECES_PER_SESSION) {
          pool.terminate().then(resolve).catch(reject)
        }
      }
    )

    for (let s = 0; s < SESSIONS; s++) {
      for (let i = 0; i < PIECES_PER_SESSION; i++) {
        const writeBuf = crypto.randomBytes(1024)
        const expectedHash = crypto.createHash('sha1').update(writeBuf).digest('hex')
        const arrayBuf = new Uint8Array(writeBuf).buffer
        pool.push(i, arrayBuf, writeBuf, expectedHash, s)
      }
    }
    
    setTimeout(() => reject(new Error('test timeout')), 1000)
  })

  assert.strictEqual(totalCompleted, SESSIONS * PIECES_PER_SESSION)
  for (let s = 0; s < SESSIONS; s++) {
    assert.strictEqual(sessionResults[s].length, PIECES_PER_SESSION)
    sessionResults[s].forEach(r => assert.strictEqual(r.valid, true))
  }
})

test('handles worker pool termination during processing', async () => {
  const pool = new WorkerPool(task, 2, () => {})
  
  const writeBuf = crypto.randomBytes(1024)
  const expectedHash = crypto.createHash('sha1').update(writeBuf).digest('hex')
  const arrayBuf = new Uint8Array(writeBuf).buffer
  
  pool.push(0, arrayBuf, writeBuf, expectedHash, 123)
  
  await pool.terminate()
  
  assert.throws(() => {
    pool.push(1, arrayBuf, writeBuf, expectedHash, 123)
  }, /terminated/)
})


test('load balancing with uneven distribution', { timeout: 1000 }, async () => {
  const TOTAL = 50
  let completed = 0
  
  await new Promise((resolve, reject) => {
    const pool = new WorkerPool(task, 4, () => {
      if (++completed === TOTAL) {
        const stats = pool.getStats()
        // Check that load is relatively balanced
        const queueLengths = stats.map(s => s.queueLength)
        const maxQueue = Math.max(...queueLengths)
        const minQueue = Math.min(...queueLengths)
        // Difference should not be too large
        assert.ok(maxQueue - minQueue <= 2)
        pool.terminate().then(resolve).catch(reject)
      }
    })

    for (let i = 0; i < TOTAL; i++) {
      const writeBuf = crypto.randomBytes(512)
      const expectedHash = crypto.createHash('sha1').update(writeBuf).digest('hex')
      const arrayBuf = new Uint8Array(writeBuf).buffer
      pool.push(i, arrayBuf, writeBuf, expectedHash, 1)
    }
    
    setTimeout(() => reject(new Error('test timeout')), 5000)
  })
})

/**
 * Test utility functions
 */
test('getStats returns correct worker information', async () => {
  const pool = new WorkerPool(task, 3, () => {})
  
  const stats = pool.getStats()
  
  assert.strictEqual(stats.length, 3)
  stats.forEach((stat, idx) => {
    assert.strictEqual(stat.id, idx)
    assert.strictEqual(stat.busy, false)
    assert.strictEqual(stat.queueLength, 0)
  })
  
  await pool.terminate()
})


test('getPendingTasks returns correct count', { timeout: 1000 }, async () => {
  const pool = new WorkerPool(task, 2, () => {})
  
  assert.strictEqual(pool.getPendingTasks(), 0)
  
  // Push some tasks
  for (let i = 0; i < 5; i++) {
    const writeBuf = crypto.randomBytes(1024)
    const hash = crypto.createHash('sha1').update(writeBuf).digest('hex')
    const arrayBuf = new Uint8Array(writeBuf).buffer
    pool.push(i, arrayBuf, writeBuf, hash, 1)
  }
  
  assert.strictEqual(pool.getPendingTasks(), 5)
  
  await pool.terminate()
})
test('isIdle returns true when no tasks', async () => {
  const pool = new WorkerPool(task, 2, () => {})
  
  assert.strictEqual(pool.isIdle(), true)
  
  await pool.terminate()
})