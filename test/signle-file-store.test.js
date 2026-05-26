import test from 'node:test'
import assert from 'node:assert'
import fs from 'fs'
import path from 'path'
import os from 'os'
import SignleFileStore from '../lib/single-file-store.js'

// Helper: tạo đường dẫn file tạm thời
function getTempFilePath() {
    return path.join(os.tmpdir(), `test-store-${Date.now()}-${Math.random()}.dat`)
}

// Helper: tạo file có sẵn nội dung
function createFileWithContent(filePath, content) {
    fs.writeFileSync(filePath, content)
}

// ==================== TESTS CHO CONSTRUCTOR ====================

test('constructor creates new file when file does not exist', () => {
    const filePath = getTempFilePath()
    const pieceLength = 16384
    
    const store = new SignleFileStore(filePath, pieceLength)
    
    assert.ok(fs.existsSync(filePath))
    assert.strictEqual(store.pieceLength, pieceLength)
    assert.ok(store.fd)
    
    store.close()
    fs.unlinkSync(filePath)
})

test('constructor opens existing file when file exists', () => {
    const filePath = getTempFilePath()
    const pieceLength = 16384
    const initialContent = 'hello world'
    createFileWithContent(filePath, initialContent)
    
    const store = new SignleFileStore(filePath, pieceLength)
    
    assert.ok(fs.existsSync(filePath))
    assert.strictEqual(store.pieceLength, pieceLength)
    
    store.close()
    fs.unlinkSync(filePath)
})

test('constructor uses r+ mode for existing file', () => {
    const filePath = getTempFilePath()
    const pieceLength = 16384
    createFileWithContent(filePath, 'initial')
    
    const store = new SignleFileStore(filePath, pieceLength)
    
    // Ghi thêm để kiểm tra có thể ghi được
    const testBuf = Buffer.from('test')
    store.write(0, testBuf)
    
    const content = fs.readFileSync(filePath)
    assert.ok(content.includes(Buffer.from('test')))
    
    store.close()
    fs.unlinkSync(filePath)
})

test('constructor throws error for invalid pieceLength', () => {
    const filePath = getTempFilePath()
    
    // pieceLength <= 0
    assert.throws(() => new SignleFileStore(filePath, 0), /piece length must be positive/i)
    assert.throws(() => new SignleFileStore(filePath, -10), /piece length must be positive/i)
    
    // pieceLength không phải số
    assert.throws(() => new SignleFileStore(filePath, 'abc'), /piece length must be a number/i)
})

// test('constructor creates parent directories if needed', () => {
//     const nestedDir = path.join(os.tmpdir(), 'test-store-nested', 'subdir')
//     const filePath = path.join(nestedDir, 'test.dat')
//     const pieceLength = 16384
    
//     // Đảm bảo thư mục chưa tồn tại
//     if (fs.existsSync(nestedDir)) {
//         fs.rmSync(nestedDir, { recursive: true, force: true })
//     }
    
//     const store = new SignleFileStore(filePath, pieceLength)
//     assert.ok(fs.existsSync(filePath))
    
//     store.close()
//     fs.rmSync(nestedDir, { recursive: true, force: true })
// })

// ==================== TESTS CHO write() ====================

test('write() writes buffer at correct piece offset', () => {
    const filePath = getTempFilePath()
    const pieceLength = 16
    const store = new SignleFileStore(filePath, pieceLength)
    
    const pieceIndex = 2
    const data = Buffer.from('abcdefghijklmnop') // 16 bytes
    store.write(pieceIndex, data)
    
    // Verify bằng đọc raw
    const readData = store.readRaw(pieceIndex * pieceLength, data.length)
    assert.deepStrictEqual(readData, data)
    
    store.close()
    fs.unlinkSync(filePath)
})

test('write() overwrites existing data at same piece', () => {
    const filePath = getTempFilePath()
    const pieceLength = 16
    const store = new SignleFileStore(filePath, pieceLength)
    
    const pieceIndex = 0
    const data1 = Buffer.from('AAAAAAAAAAAAAAAA')
    const data2 = Buffer.from('BBBBBBBBBBBBBBBB')
    
    store.write(pieceIndex, data1)
    store.write(pieceIndex, data2)
    
    const readData = store.read(pieceIndex, 0, pieceLength)
    assert.deepStrictEqual(readData, data2)
    
    store.close()
    fs.unlinkSync(filePath)
})

test('write() can write different pieces independently', () => {
    const filePath = getTempFilePath()
    const pieceLength = 8
    const store = new SignleFileStore(filePath, pieceLength)
    
    const piece0Data = Buffer.from('piece0!!')
    const piece1Data = Buffer.from('piece1!!')
    const piece2Data = Buffer.from('piece2!!')
    
    store.write(0, piece0Data)
    store.write(2, piece2Data)
    store.write(1, piece1Data)
    
    assert.deepStrictEqual(store.read(0, 0, 8), piece0Data)
    assert.deepStrictEqual(store.read(1, 0, 8), piece1Data)
    assert.deepStrictEqual(store.read(2, 0, 8), piece2Data)
    
    store.close()
    fs.unlinkSync(filePath)
})

test('write() with buffer larger than pieceLength', () => {
    const filePath = getTempFilePath()
    const pieceLength = 8
    const store = new SignleFileStore(filePath, pieceLength)
    
    const largeData = Buffer.from('1234567890ABCDEF') // 16 bytes
    // Ghi vào piece 0, chỉ ghi 8 bytes đầu? Không, write sẽ ghi toàn bộ buf,
    // nhưng sẽ tràn sang piece tiếp theo? Implementation hiện tại ghi đúng offset
    // pieceIndex * pieceLength, nên nếu buf dài hơn pieceLength sẽ ghi đè sang piece tiếp theo.
    // Đây có thể là behavior mong muốn hoặc không. Ta test behavior hiện tại.
    store.write(0, largeData)
    
    // Đọc piece 0: 8 bytes đầu
    const piece0 = store.read(0, 0, 8)
    assert.deepStrictEqual(piece0, Buffer.from('12345678'))
    
    // Đọc piece 1: 8 bytes tiếp theo
    const piece1 = store.read(1, 0, 8)
    assert.deepStrictEqual(piece1, Buffer.from('90ABCDEF'))
    
    store.close()
    fs.unlinkSync(filePath)
})

test('write() with buffer smaller than pieceLength writes only that buffer', () => {
    const filePath = getTempFilePath()
    const pieceLength = 16
    const store = new SignleFileStore(filePath, pieceLength)
    
    const smallData = Buffer.from('small')
    store.write(0, smallData)
    
    const readData = store.read(0, 0, smallData.length)
    assert.deepStrictEqual(readData, smallData)
    
    // Đọc remainder (phần còn lại của piece) - chưa được ghi, nên có thể là garbage hoặc zeros
    // Nhưng do file mới tạo, bytes chưa ghi sẽ là 0 (hoặc undefined - tùy hệ điều hành)
    // Ta không assert phần chưa ghi vì không xác định.
    
    store.close()
    fs.unlinkSync(filePath)
})

test('write() with zero-length buffer', () => {
    const filePath = getTempFilePath()
    const pieceLength = 16
    const store = new SignleFileStore(filePath, pieceLength)
    
    const emptyBuf = Buffer.alloc(0)
    store.write(5, emptyBuf)
    
    // Không có gì thay đổi, đọc piece 5 vẫn là buffer rỗng hoặc garbage? Nhưng không có lỗi.
    // Chỉ cần không crash
    assert.ok(true)
    
    store.close()
    fs.unlinkSync(filePath)
})

// ==================== TESTS CHO read() ====================

test('read() reads correct block within piece', () => {
    const filePath = getTempFilePath()
    const pieceLength = 32
    const store = new SignleFileStore(filePath, pieceLength)
    
    // Ghi dữ liệu mẫu cho piece 0
    const fullPiece = Buffer.from('A'.repeat(16) + 'B'.repeat(16))
    store.write(0, fullPiece)
    
    // Đọc block từ byte 10 đến 20 (10 bytes)
    const block = store.read(0, 10, 10)
    const expected = fullPiece.slice(10, 20)
    assert.deepStrictEqual(block, expected)
    
    store.close()
    fs.unlinkSync(filePath)
})

test('read() reads from middle of piece', () => {
    const filePath = getTempFilePath()
    const pieceLength = 64
    const store = new SignleFileStore(filePath, pieceLength)
    
    const pieceData = Buffer.from('X'.repeat(64))
    store.write(2, pieceData)
    
    const block = store.read(2, 30, 20)
    assert.deepStrictEqual(block, pieceData.slice(30, 50))
    
    store.close()
    fs.unlinkSync(filePath)
})

test('read() reads entire piece when begin=0, length=pieceLength', () => {
    const filePath = getTempFilePath()
    const pieceLength = 128
    const store = new SignleFileStore(filePath, pieceLength)
    
    const pieceData = Buffer.alloc(pieceLength, 0xAB)
    store.write(3, pieceData)
    
    const entire = store.read(3, 0, pieceLength)
    assert.deepStrictEqual(entire, pieceData)
    
    store.close()
    fs.unlinkSync(filePath)
})

test('read() reads across piece boundary?', () => {
    const filePath = getTempFilePath()
    const pieceLength = 16
    const store = new SignleFileStore(filePath, pieceLength)
    
    // Ghi piece 0 và piece 1
    store.write(0, Buffer.from('AAAAAAAAAAAAAAAA')) // 16 bytes A
    store.write(1, Buffer.from('BBBBBBBBBBBBBBBB')) // 16 bytes B
    
    // Đọc block bắt đầu từ byte 10 của piece 0, dài 20 bytes -> sẽ đọc sang piece 1
    // Hành vi hiện tại: read chỉ đọc trong piece (vì offset = pieceIndex*pieceLength + begin)
    // Nên nếu begin+length > pieceLength, nó sẽ đọc ra ngoài piece nhưng vẫn trong file.
    // Test behavior.
    const block = store.read(0, 10, 20)
    // Expected: 6 bytes từ piece 0 (byte 10-15) + 14 bytes từ piece 1 (byte 0-13)
    const expected = Buffer.concat([
        Buffer.from('AAAAAA'), // byte 10-15 của piece 0 (6 bytes)
        Buffer.from('BBBBBBBBBBBBBB') // 14 bytes đầu của piece 1
    ])
    assert.deepStrictEqual(block, expected)
    
    store.close()
    fs.unlinkSync(filePath)
})

test('read() with invalid piece index throws error', () => {
    const filePath = getTempFilePath()
    const pieceLength = 16
    const store = new SignleFileStore(filePath, pieceLength)
    
    // Ghi một piece
    store.write(0, Buffer.from('test'))
    
    // Đọc piece âm
    assert.throws(() => store.read(-1, 0, 4), /piece index must be non-negative/i)
    
    // Đọc piece quá lớn (vẫn được vì file sparse? Nhưng offset sẽ rất lớn, tùy FS)
    // Có thể không throw lỗi nhưng đọc sẽ ra buffer rỗng hoặc lỗi EINVAL? Tùy.
    // Ta không assert cứng.
    
    store.close()
    fs.unlinkSync(filePath)
})

test('read() with begin out of bounds (negative) throws error', () => {
    const filePath = getTempFilePath()
    const pieceLength = 16
    const store = new SignleFileStore(filePath, pieceLength)
    
    assert.throws(() => store.read(0, -1, 4), /begin must be non-negative/i)
    
    store.close()
    fs.unlinkSync(filePath)
})

test('read() with length <= 0 throws error', () => {
    const filePath = getTempFilePath()
    const pieceLength = 16
    const store = new SignleFileStore(filePath, pieceLength)
    
    assert.throws(() => store.read(0, 0, 0), /length must be positive/i)
    assert.throws(() => store.read(0, 0, -5), /length must be positive/i)
    
    store.close()
    fs.unlinkSync(filePath)
})

test('read() with begin+length exceeding pieceLength works (crosses boundary)', () => {
    const filePath = getTempFilePath()
    const pieceLength = 8
    const store = new SignleFileStore(filePath, pieceLength)
    
    store.write(0, Buffer.from('12345678'))
    store.write(1, Buffer.from('ABCDEFGH'))
    
    const block = store.read(0, 6, 5) // bytes 6,7 của piece 0 và bytes 0,1,2 của piece 1
    const expected = Buffer.from('78ABC')
    assert.deepStrictEqual(block, expected)
    
    store.close()
    fs.unlinkSync(filePath)
})

// ==================== TESTS CHO readRaw() ====================

test('readRaw() reads absolute offset', () => {
    const filePath = getTempFilePath()
    const pieceLength = 32
    const store = new SignleFileStore(filePath, pieceLength)
    
    const data = Buffer.from('0123456789ABCDEF')
    store.write(0, data)
    
    const raw = store.readRaw(5, 4)
    assert.deepStrictEqual(raw, Buffer.from('5678'))
    
    store.close()
    fs.unlinkSync(filePath)
})

test('readRaw() can read across pieces', () => {
    const filePath = getTempFilePath()
    const pieceLength = 8
    const store = new SignleFileStore(filePath, pieceLength)
    
    store.write(0, Buffer.from('AAAAAAAA'))
    store.write(1, Buffer.from('BBBBBBBB'))
    
    const raw = store.readRaw(6, 8) // bytes 6,7 của piece 0 + bytes 0-5 của piece 1
    assert.deepStrictEqual(raw, Buffer.from('AABBBBBB'))
    
    store.close()
    fs.unlinkSync(filePath)
})

test('readRaw() with offset beyond end of file returns empty or partial?', () => {
    const filePath = getTempFilePath()
    const pieceLength = 16
    const store = new SignleFileStore(filePath, pieceLength)
    
    store.write(0, Buffer.from('short'))
    
    // Đọc offset 1000, length 10 - file chưa có dữ liệu, đọc sẽ ra buffer với nội dung không xác định (có thể zero)
    // Thực tế fs.readSync trên offset lớn hơn file size sẽ trả về buffer chứa garbage hoặc throw?
    // Trên Unix, nó vẫn đọc được nhưng dữ liệu không xác định (sparse file). Ta không assert cụ thể.
    const raw = store.readRaw(1000, 10)
    assert.ok(Buffer.isBuffer(raw))
    assert.strictEqual(raw.length, 10)
    
    store.close()
    fs.unlinkSync(filePath)
})

test('readRaw() with negative offset throws error', () => {
    const filePath = getTempFilePath()
    const pieceLength = 16
    const store = new SignleFileStore(filePath, pieceLength)
    
    assert.throws(() => store.readRaw(-1, 4), /offset must be non-negative/i)
    
    store.close()
    fs.unlinkSync(filePath)
})

test('readRaw() with zero length throws error', () => {
    const filePath = getTempFilePath()
    const pieceLength = 16
    const store = new SignleFileStore(filePath, pieceLength)
    
    assert.throws(() => store.readRaw(0, 0), /length must be positive/i)
    
    store.close()
    fs.unlinkSync(filePath)
})

// ==================== TESTS CHO close() ====================

test('close() releases file descriptor', () => {
    const filePath = getTempFilePath()
    const pieceLength = 16
    const store = new SignleFileStore(filePath, pieceLength)
    
    const fd = store.fd
    store.close()
    
    // Thử ghi lại qua fd cũ - không nên dùng nữa, nhưng ta kiểm tra xem có lỗi không
    assert.throws(() => {
        fs.writeSync(fd, Buffer.from('test'))
    }, /EBADF|file descriptor/)
    
    fs.unlinkSync(filePath)
})

test('close() can be called multiple times without error', () => {
    const filePath = getTempFilePath()
    const pieceLength = 16
    const store = new SignleFileStore(filePath, pieceLength)
    
    store.close()
    store.close() // lần 2 không lỗi
    store.close() // lần 3
    
    fs.unlinkSync(filePath)
})

// ==================== EDGE CASES ====================

test('write then read with high piece index', () => {
    const filePath = getTempFilePath()
    const pieceLength = 1024
    const store = new SignleFileStore(filePath, pieceLength)
    
    const largeIndex = 1000
    const data = Buffer.from('test data at high offset')
    store.write(largeIndex, data)
    
    const readData = store.read(largeIndex, 0, data.length)
    assert.deepStrictEqual(readData, data)
    
    store.close()
    fs.unlinkSync(filePath)
})

test('read from unwritten piece returns zero-filled? (implementation dependent)', () => {
    const filePath = getTempFilePath()
    const pieceLength = 16
    const store = new SignleFileStore(filePath, pieceLength)
    
    // Chưa ghi piece 5
    const data = store.read(5, 0, 16)
    // Có thể là buffer với nội dung bất kỳ, nhưng thường là 0
    assert.strictEqual(data.length, 16)
    // Không assert nội dung vì không xác định
    
    store.close()
    fs.unlinkSync(filePath)
})

test('concurrent writes and reads (sequential)', () => {
    const filePath = getTempFilePath()
    const pieceLength = 64
    const store = new SignleFileStore(filePath, pieceLength)
    
    for (let i = 0; i < 10; i++) {
        const buf = Buffer.from(`piece${i}`.padEnd(64, ' '))
        store.write(i, buf)
        const read = store.read(i, 0, 64)
        assert.deepStrictEqual(read, buf)
    }
    
    store.close()
    fs.unlinkSync(filePath)
})

test('large buffer write and read', () => {
    const filePath = getTempFilePath()
    const pieceLength = 1024 * 1024 // 1MB
    const store = new SignleFileStore(filePath, pieceLength)
    
    const largeBuf = Buffer.alloc(1024 * 1024, 0xFE)
    store.write(0, largeBuf)
    
    const readBuf = store.read(0, 0, largeBuf.length)
    assert.deepStrictEqual(readBuf, largeBuf)
    
    store.close()
    fs.unlinkSync(filePath)
})

test('write with buffer that is a slice of larger buffer', () => {
    const filePath = getTempFilePath()
    const pieceLength = 16
    const store = new SignleFileStore(filePath, pieceLength)
    
    const big = Buffer.alloc(100, 0xAA)
    const slice = big.subarray(10, 26) // 16 bytes
    store.write(0, slice)
    
    const read = store.read(0, 0, 16)
    assert.deepStrictEqual(read, slice)
    
    store.close()
    fs.unlinkSync(filePath)
})

test('write and read with pieceLength 1', () => {
    const filePath = getTempFilePath()
    const pieceLength = 1
    const store = new SignleFileStore(filePath, pieceLength)
    
    for (let i = 0; i < 10; i++) {
        store.write(i, Buffer.from([i + 65])) // 'A', 'B', ...
    }
    
    for (let i = 0; i < 10; i++) {
        const val = store.read(i, 0, 1)
        assert.strictEqual(val[0], i + 65)
    }
    
    store.close()
    fs.unlinkSync(filePath)
})

test('read with length larger than remaining file size', () => {
    const filePath = getTempFilePath()
    const pieceLength = 8
    const store = new SignleFileStore(filePath, pieceLength)
    
    store.write(0, Buffer.from('12345678'))
    // Đọc từ offset 6, length 10 -> sẽ đọc bytes 6,7 của piece 0 và 8 bytes của piece 1 (chưa ghi)
    const block = store.read(0, 6, 10)
    assert.strictEqual(block.length, 10)
    // Phần chưa ghi có thể là garbage, không assert
    
    store.close()
    fs.unlinkSync(filePath)
})

// ==================== CLEANUP TESTS ====================

test('file is properly closed and removed after test', async () => {
    const filePath = getTempFilePath()
    const pieceLength = 16
    let store = new SignleFileStore(filePath, pieceLength)
    store.write(0, Buffer.from('test'))
    store.close()
    
    // Try to delete file (should be possible)
    fs.unlinkSync(filePath)
    assert.ok(!fs.existsSync(filePath))
})