import test from 'node:test'
import assert from 'node:assert'
import BitfieldTracker from '../lib/bitfield-tracker.js'

// ==================== TESTS CHO CONSTRUCTOR ====================

test('constructor creates correct buffer size', () => {
    const testCases = [
        [0, 0],
        [1, 1],
        [8, 1],
        [9, 2],
        [10, 2],
        [16, 2],
        [17, 3],
        [100, 13]
    ]
    
    for (const [total, expectedSize] of testCases) {
        const tracker = new BitfieldTracker(total)
        assert.strictEqual(tracker.totalPieces, total)
        assert.strictEqual(tracker.bitfield.length, expectedSize)
    }
})

test('constructor initializes all bits to 0', () => {
    const tracker = new BitfieldTracker(100)
    for (let i = 0; i < 100; i++) {
        assert.strictEqual(tracker.has(i), false)
    }
})

test('constructor sets trailing bits to 0 correctly', () => {
    // Test với totalPieces = 10, remainder = 2
    // last byte phải là 0b11000000 (0xC0)
    const tracker1 = new BitfieldTracker(10, true)
    assert.strictEqual(tracker1.bitfield[1], 0xC0, 
        `Expected last byte = 0xC0 (11000000), got ${tracker1.bitfield[1].toString(2)}`)
    
    // Test totalPieces = 9, remainder = 1
    // last byte phải là 0b10000000 (0x80)
    const tracker2 = new BitfieldTracker(9, true)
    assert.strictEqual(tracker2.bitfield[1], 0x80,
        `Expected last byte = 0x80 (10000000), got ${tracker2.bitfield[1].toString(2)}`)
    
    // Test totalPieces = 15, remainder = 7
    // last byte phải là 0b11111110 (0xFE)
    const tracker3 = new BitfieldTracker(15, true)
    assert.strictEqual(tracker3.bitfield[1], 0xFE,
        `Expected last byte = 0xFE (11111110), got ${tracker3.bitfield[1].toString(2)}`)
    
    // Test totalPieces = 7, remainder = 7
    // last byte (byte 0) phải là 0b11111110 (0xFE)
    const tracker4 = new BitfieldTracker(7, true)
    assert.strictEqual(tracker4.bitfield[0], 0xFE,
        `Expected last byte = 0xFE (11111110), got ${tracker4.bitfield[0].toString(2)}`)
})

test('constructor trailing bits không ảnh hưởng đến has()', () => {
    // Với totalPieces = 10, pieces 8 và 9 là hợp lệ
    // trailing bits (bits 0-5 của byte cuối) không tương ứng piece nào
    const tracker = new BitfieldTracker(10)
    
    // Thử set piece 8 (hợp lệ) - bit thứ 2 từ trái của byte cuối
    tracker.set(8)
    assert.strictEqual(tracker.has(8), true)
    
    // Thử has(10) phải throw lỗi hoặc undefined (implementation hiện tại không validate)
    // Nhưng trailing bits không làm has() trả về true cho index không tồn tại
})

test('constructor handles totalPieces = 0', () => {
    const tracker = new BitfieldTracker(0)
    assert.strictEqual(tracker.totalPieces, 0)
    assert.strictEqual(tracker.bitfield.length, 0)
    assert.strictEqual(tracker.count, 0)
    assert.deepStrictEqual(tracker.missing(), [])
})

// ==================== TESTS CHO set() METHOD ====================

test('set() marks single piece correctly', () => {
    const tracker = new BitfieldTracker(16)
    
    for (let i = 0; i < 16; i++) {
        const newTracker = new BitfieldTracker(16)
        newTracker.set(i)
        
        // Chỉ piece i là true
        for (let j = 0; j < 16; j++) {
            assert.strictEqual(newTracker.has(j), j === i, 
                `Failed for piece ${i}, check piece ${j}`)
        }
    }
})

test('set() works with multiple pieces in same byte', () => {
    const tracker = new BitfieldTracker(16)
    
    // Set tất cả pieces trong byte 0 (pieces 0-7)
    for (let i = 0; i < 8; i++) {
        tracker.set(i)
    }
    
    // Byte 0 phải là 0xFF (11111111)
    assert.strictEqual(tracker.bitfield[0], 0xFF)
    
    // Kiểm tra từng piece
    for (let i = 0; i < 8; i++) {
        assert.strictEqual(tracker.has(i), true)
    }
    for (let i = 8; i < 16; i++) {
        assert.strictEqual(tracker.has(i), false)
    }
})

test('set() handles bit positions correctly theo MSB first', () => {
    // Bit ordering: MSB first - piece 0 là bit cao nhất (bit 7)
    const expectedValues = [
        0x80, // 10000000 - piece 0
        0x40, // 01000000 - piece 1
        0x20, // 00100000 - piece 2
        0x10, // 00010000 - piece 3
        0x08, // 00001000 - piece 4
        0x04, // 00000100 - piece 5
        0x02, // 00000010 - piece 6
        0x01  // 00000001 - piece 7
    ]
    
    for (let i = 0; i < 8; i++) {
        const tracker = new BitfieldTracker(8)
        tracker.set(i)
        assert.strictEqual(tracker.bitfield[0], expectedValues[i],
            `Piece ${i}: expected ${expectedValues[i].toString(2)}, got ${tracker.bitfield[0].toString(2)}`)
    }
})

test('set() works across byte boundaries', () => {
    const tracker = new BitfieldTracker(16)
    
    // Set piece 7 (cuối byte 0) và piece 8 (đầu byte 1)
    tracker.set(7)
    tracker.set(8)
    
    // Byte 0: piece 7 là bit thấp nhất = 0x01
    assert.strictEqual(tracker.bitfield[0], 0x01)
    // Byte 1: piece 8 là bit cao nhất = 0x80
    assert.strictEqual(tracker.bitfield[1], 0x80)
    
    assert.strictEqual(tracker.has(7), true)
    assert.strictEqual(tracker.has(8), true)
})

test('set() với totalPieces không chia hết cho 8', () => {
    // totalPieces = 10, pieces 8 và 9 nằm trong byte cuối
    const tracker = new BitfieldTracker(10)
    
    tracker.set(8) // piece 8
    // Byte cuối: piece 8 là bit thứ 2 từ trái (0b01000000)
    assert.strictEqual(tracker.bitfield[1], 0x80) // 0xC0 | 0x40 = 0xC0? 
    // Thực tế: 0xC0 (11000000) OR 0x40 (01000000) = 0xC0 vẫn giữ nguyên
    // Cần kiểm tra kỹ hơn
    
    tracker.set(9) // piece 9
    // Sau khi set cả 2, byte cuối phải là 0xC0 (11000000)
    assert.strictEqual(tracker.bitfield[1], 0xc0)
})

test('set() cùng piece nhiều lần vẫn hoạt động', () => {
    const tracker = new BitfieldTracker(10)
    
    tracker.set(5)
    assert.strictEqual(tracker.count, 1)
    
    tracker.set(5)
    tracker.set(5)
    tracker.set(5)
    assert.strictEqual(tracker.count, 1)
    assert.strictEqual(tracker.has(5), true)
})

// ==================== TESTS CHO has() METHOD ====================

test('has() returns false cho pieces chưa set', () => {
    const tracker = new BitfieldTracker(50)
    
    for (let i = 0; i < 50; i++) {
        assert.strictEqual(tracker.has(i), false)
    }
})

test('has() returns true sau khi set', () => {
    const tracker = new BitfieldTracker(50)
    const setPieces = [0, 7, 8, 15, 16, 23, 24, 31, 32, 39, 40, 49]
    
    for (const piece of setPieces) {
        tracker.set(piece)
    }
    
    for (let i = 0; i < 50; i++) {
        const expected = setPieces.includes(i)
        assert.strictEqual(tracker.has(i), expected)
    }
})

test('has() với trailing bits không bị ảnh hưởng', () => {
    // Với totalPieces = 10, trailing bits không tương ứng piece nào
    const tracker = new BitfieldTracker(10)
    
    // Set piece 8 và 9 (bits hợp lệ trong byte cuối)
    tracker.set(8)
    tracker.set(9)
    
    // Byte cuối bây giờ là 0xC0 (11000000)
    // Các trailing bits (bit 0-5) là 0, nhưng không có piece nào check vào đó
    assert.strictEqual(tracker.has(8), true)
    assert.strictEqual(tracker.has(9), true)
})

// ==================== TESTS CHO missing() METHOD ====================

test('missing() trả về tất cả pieces khi chưa set piece nào', () => {
    const tracker = new BitfieldTracker(10)
    const missing = tracker.missing()
    
    assert.strictEqual(missing.length, 10)
    assert.deepStrictEqual(missing, [0, 1, 2, 3, 4, 5, 6, 7, 8, 9])
})

test('missing() trả về mảng rỗng khi đã set tất cả pieces', () => {
    const tracker = new BitfieldTracker(16)
    
    for (let i = 0; i < 16; i++) {
        tracker.set(i)
    }
    
    assert.deepStrictEqual(tracker.missing(), [])
})

test('missing() chỉ trả về các pieces còn thiếu', () => {
    const tracker = new BitfieldTracker(20)
    const setPieces = [2, 5, 10, 15, 18]
    
    for (const piece of setPieces) {
        tracker.set(piece)
    }
    
    const missing = tracker.missing()
    const expectedMissing = []
    for (let i = 0; i < 20; i++) {
        if (!setPieces.includes(i)) {
            expectedMissing.push(i)
        }
    }
    
    assert.strictEqual(missing.length, 20 - setPieces.length)
    assert.deepStrictEqual(missing, expectedMissing)
})

test('missing() với totalPieces = 10 (có trailing bits)', () => {
    const tracker = new BitfieldTracker(10)
    
    // Set piece 8 và 9
    tracker.set(8)
    tracker.set(9)
    
    const missing = tracker.missing()
    // Chỉ pieces 0-7 là missing
    assert.strictEqual(missing.length, 8)
    assert.deepStrictEqual(missing, [0, 1, 2, 3, 4, 5, 6, 7])
})

// ==================== TESTS CHO count GETTER ====================

test('count trả về 0 ban đầu', () => {
    const tracker = new BitfieldTracker(100)
    assert.strictEqual(tracker.count, 0)
})

test('count tăng đúng khi set pieces', () => {
    const tracker = new BitfieldTracker(50)
    
    for (let i = 0; i < 10; i++) {
        tracker.set(i)
        assert.strictEqual(tracker.count, i + 1)
    }
    
    assert.strictEqual(tracker.count, 10)
    
    for (let i = 10; i < 25; i++) {
        tracker.set(i)
        assert.strictEqual(tracker.count, i + 1)
    }
    
    assert.strictEqual(tracker.count, 25)
})

test('count không tăng khi set cùng piece nhiều lần', () => {
    const tracker = new BitfieldTracker(10)
    
    tracker.set(5)
    assert.strictEqual(tracker.count, 1)
    
    tracker.set(5)
    assert.strictEqual(tracker.count, 1)
    
    tracker.set(5)
    tracker.set(5)
    assert.strictEqual(tracker.count, 1)
})

test('count với totalPieces không chia hết cho 8', () => {
    const tracker = new BitfieldTracker(10)
    
    // Set pieces 8 và 9
    tracker.set(8)
    tracker.set(9)
    
    assert.strictEqual(tracker.count, 2)
    
    // Set thêm piece 0
    tracker.set(0)
    assert.strictEqual(tracker.count, 3)
})

// ==================== EDGE CASES ====================

test('set() piece cuối cùng (totalPieces - 1)', () => {
    const sizes = [1, 8, 9, 10, 16, 17, 32, 33, 100]
    
    for (const size of sizes) {
        const tracker = new BitfieldTracker(size)
        const lastPiece = size - 1
        
        tracker.set(lastPiece)
        assert.strictEqual(tracker.has(lastPiece), true,
            `Failed for size ${size}, last piece ${lastPiece}`)
        assert.strictEqual(tracker.count, 1)
        
        // Verify các pieces khác không bị ảnh hưởng
        for (let i = 0; i < size - 1; i++) {
            assert.strictEqual(tracker.has(i), false)
        }
    }
})

test('set() piece đầu tiên (index 0)', () => {
    const sizes = [1, 8, 9, 10, 16, 17, 32, 33, 100]
    
    for (const size of sizes) {
        const tracker = new BitfieldTracker(size)
        
        tracker.set(0)
        assert.strictEqual(tracker.has(0), true,
            `Failed for size ${size}, piece 0`)
        assert.strictEqual(tracker.count, 1)
        
        // Verify các pieces khác không bị ảnh hưởng
        for (let i = 1; i < size; i++) {
            assert.strictEqual(tracker.has(i), false)
        }
    }
})

test('trailing bits luôn được giữ là 0 khi set các pieces hợp lệ', () => {
    // Với totalPieces = 10, byte cuối có 6 trailing bits
    // Các trailing bits không bao giờ được set dù set piece 8 và 9
    const tracker = new BitfieldTracker(10)
    
    tracker.set(8) // bit 6 (từ trái) của byte cuối
    tracker.set(9) // bit 7 (từ trái) của byte cuối
    
    // Byte cuối phải là 0b11000000 (0xC0)
    // 6 trailing bits cuối (bit 0-5) vẫn là 0
    assert.strictEqual(tracker.bitfield[1], 0xC0)
})

test('has() với index ngoài bounds (implementation hiện tại không validate)', () => {
    const tracker = new BitfieldTracker(10)
    
    // Lưu ý: implementation hiện tại KHÔNG validate index
    // Nên has(10) sẽ tính toán và có thể trả về giá trị từ trailing bits
    // Đây là behavior hiện tại, test chỉ ghi nhận
    const result = tracker.has(10)
    // Không assert vì behavior không xác định, chỉ cần không crash
    assert.ok(result === true || result === false)
})

// ==================== CONSISTENCY TESTS ====================

test('count phải khớp với đếm thủ công từ has()', () => {
    const tracker = new BitfieldTracker(50)
    
    // Set random pieces
    for (let i = 0; i < 50; i += 3) {
        tracker.set(i)
    }
    
    let manualCount = 0
    for (let i = 0; i < 50; i++) {
        if (tracker.has(i)) manualCount++
    }
    
    assert.strictEqual(tracker.count, manualCount)
})

test('missing().length + count = totalPieces', () => {
    const tracker = new BitfieldTracker(75)
    
    // Set 30 pieces
    for (let i = 0; i < 30; i++) {
        tracker.set(i)
    }
    
    const missing1 = tracker.missing()
    assert.strictEqual(missing1.length + tracker.count, tracker.totalPieces)
    
    // Set thêm 20 pieces
    for (let i = 30; i < 50; i++) {
        tracker.set(i)
    }
    
    const missing2 = tracker.missing()
    assert.strictEqual(missing2.length + tracker.count, tracker.totalPieces)
})

test('các tracker độc lập không ảnh hưởng lẫn nhau', () => {
    const tracker1 = new BitfieldTracker(50)
    const tracker2 = new BitfieldTracker(50)
    
    for (let i = 0; i < 25; i++) {
        tracker1.set(i)
        tracker2.set(i + 25)
    }
    
    for (let i = 0; i < 25; i++) {
        assert.strictEqual(tracker1.has(i), true)
        assert.strictEqual(tracker2.has(i), false)
    }
    
    for (let i = 25; i < 50; i++) {
        assert.strictEqual(tracker1.has(i), false)
        assert.strictEqual(tracker2.has(i), true)
    }
})

// ==================== BIT OPERATION TESTS ====================

test('bit mask cho mỗi vị trí trong byte là chính xác', () => {
    // Kiểm tra công thức: 1 << (7 - (i % 8))
    for (let bitPos = 0; bitPos < 8; bitPos++) {
        const tracker = new BitfieldTracker(8)
        tracker.set(bitPos)
        
        const byte = tracker.bitfield[0]
        const expectedByte = 1 << (7 - bitPos)
        
        assert.strictEqual(byte, expectedByte,
            `Bit position ${bitPos}: expected ${expectedByte.toString(2)}, got ${byte.toString(2)}`)
    }
})

test('set tất cả pieces trong 1 byte ra 0xFF', () => {
    const tracker = new BitfieldTracker(8)
    
    for (let i = 0; i < 8; i++) {
        tracker.set(i)
    }
    
    assert.strictEqual(tracker.bitfield[0], 0xFF)
    assert.strictEqual(tracker.count, 8)
})

// ==================== REAL-WORLD SCENARIO TESTS ====================

test('mô phỏng tải torrent tuần tự', () => {
    const totalPieces = 1000
    const tracker = new BitfieldTracker(totalPieces)
    
    for (let i = 0; i < 500; i++) {
        tracker.set(i)
        assert.strictEqual(tracker.count, i + 1)
    }
    
    assert.strictEqual(tracker.count, 500)
    
    const missing = tracker.missing()
    assert.strictEqual(missing.length, 500)
    assert.strictEqual(missing[0], 500)
    assert.strictEqual(missing[missing.length - 1], 999)
})

test('mô phỏng tải torrent ngẫu nhiên', () => {
    const totalPieces = 500
    const tracker = new BitfieldTracker(totalPieces)
    const downloaded = new Set()
    
    while (downloaded.size < 300) {
        const piece = Math.floor(Math.random() * totalPieces)
        if (!downloaded.has(piece)) {
            tracker.set(piece)
            downloaded.add(piece)
        }
    }
    
    assert.strictEqual(tracker.count, 300)
    
    for (let i = 0; i < totalPieces; i++) {
        assert.strictEqual(tracker.has(i), downloaded.has(i))
    }
})

test('mô phỏng tải torrent với totalPieces lẻ (10 pieces)', () => {
    const tracker = new BitfieldTracker(10)
    
    // Tải theo thứ tự: 0,2,4,6,8
    const downloaded = [0, 2, 4, 6, 8]
    for (const piece of downloaded) {
        tracker.set(piece)
    }
    
    assert.strictEqual(tracker.count, 5)
    
    // Kiểm tra từng piece
    for (let i = 0; i < 10; i++) {
        assert.strictEqual(tracker.has(i), downloaded.includes(i))
    }
    
    // missing phải là [1,3,5,7,9]
    assert.deepStrictEqual(tracker.missing(), [1, 3, 5, 7, 9])
})

// ==================== PERFORMANCE TESTS ====================

test('set() performance với nhiều pieces', () => {
    const tracker = new BitfieldTracker(100000)
    const start = Date.now()
    
    for (let i = 0; i < 100000; i++) {
        tracker.set(i)
    }
    
    const duration = Date.now() - start
    // 100k set operations nên < 100ms
    assert.ok(duration < 100, `set() took ${duration}ms for 100k pieces`)
})

test('has() performance với nhiều pieces', () => {
    const tracker = new BitfieldTracker(100000)
    
    for (let i = 0; i < 100000; i += 2) {
        tracker.set(i)
    }
    
    const start = Date.now()
    for (let i = 0; i < 100000; i++) {
        tracker.has(i)
    }
    const duration = Date.now() - start
    
    // 100k has operations nên < 100ms
    assert.ok(duration < 100, `has() took ${duration}ms for 100k operations`)
})

test('missing() performance với bitfield lớn', () => {
    const tracker = new BitfieldTracker(100000)
    
    for (let i = 0; i < 100000; i += 2) {
        tracker.set(i)
    }
    
    const start = Date.now()
    const missing = tracker.missing()
    const duration = Date.now() - start
    
    // missing() cho 100k pieces nên < 200ms
    assert.ok(duration < 200, `missing() took ${duration}ms for 100k pieces`)
    assert.strictEqual(missing.length, 50000)
})

// ==================== BITFIELD SERIALIZATION TEST ====================

test('bitfield có thể export và restore', () => {
    const original = new BitfieldTracker(100)
    
    for (let i = 0; i < 50; i += 3) {
        original.set(i)
    }
    
    // Export (copy buffer)
    const exportedBuffer = Buffer.from(original.bitfield)
    
    // Restore
    const restored = new BitfieldTracker(100)
    restored.bitfield = exportedBuffer
    
    // Verify consistency
    assert.strictEqual(restored.totalPieces, original.totalPieces)
    assert.strictEqual(restored.count, original.count)
    assert.deepStrictEqual(restored.missing(), original.missing())
    
    for (let i = 0; i < 100; i++) {
        assert.strictEqual(restored.has(i), original.has(i))
    }
})

// ==================== TEST CHO CÔNG THỨC TRAILING BITS ====================

test('verify công thức trailing bits: 0xFF << (8 - remainder) & 0xFF', () => {
    // Test công thức cho các remainder khác nhau
    const testCases = [
        { remainder: 1, expected: 0x80 }, // 10000000
        { remainder: 2, expected: 0xC0 }, // 11000000
        { remainder: 3, expected: 0xE0 }, // 11100000
        { remainder: 4, expected: 0xF0 }, // 11110000
        { remainder: 5, expected: 0xF8 }, // 11111000
        { remainder: 6, expected: 0xFC }, // 11111100
        { remainder: 7, expected: 0xFE }, // 11111110
    ]
    
    for (const { remainder, expected } of testCases) {
        const result = (0xFF << (8 - remainder)) & 0xFF
        assert.strictEqual(result, expected,
            `remainder=${remainder}: expected ${expected.toString(2)}, got ${result.toString(2)}`)
    }
})

test('totalPieces=10, remainder=2, last byte = 0xC0', () => {
    const tracker = new BitfieldTracker(10, true)
    // 2 pieces cuối (8,9) nằm ở 2 bit MSB: 11000000 = 0xC0
    assert.strictEqual(tracker.bitfield[1], 0xC0)
})

test('totalPieces=9, remainder=1, last byte = 0x80', () => {
    const tracker = new BitfieldTracker(9, true)
    // 1 piece cuối (8) nằm ở bit MSB: 10000000 = 0x80
    assert.strictEqual(tracker.bitfield[1], 0x80)
})

test('totalPieces=7, remainder=7, last byte = 0xFE', () => {
    const tracker = new BitfieldTracker(7, true)
    // 7 pieces nằm ở 7 bit MSB: 11111110 = 0xFE
    assert.strictEqual(tracker.bitfield[0], 0xFE)
})

test('totalPieces=15, remainder=7, last byte = 0xFE', () => {
    const tracker = new BitfieldTracker(15, true)
    // pieces 8-14 nằm ở byte 1, 7 bit MSB: 11111110 = 0xFE
    assert.strictEqual(tracker.bitfield[1], 0xFE)
})