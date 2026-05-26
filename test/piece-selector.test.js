import test from 'node:test'
import assert from 'node:assert'
import PieceSelector from '../lib/piece-selector.js'
import BitfieldTracker from '../lib/bitfield-tracker.js'

function createPeerBitfield(totalPieces, pieces) {
    const bitfield = Buffer.alloc(Math.ceil(totalPieces / 8))
    for (const piece of pieces) {
        bitfield[Math.floor(piece / 8)] |= (1 << (7 - (piece % 8)))
    }
    return bitfield
}

// ==================== TESTS CHO CONSTRUCTOR ====================

test('constructor default mode is sequential', () => {
    const selector = new PieceSelector()
    assert.strictEqual(selector.mode, 'sequential')
    assert.ok(selector.peerAvailability instanceof Map)
    assert.ok(selector.inFlight instanceof Set)
    assert.strictEqual(selector.peerAvailability.size, 0)
    assert.strictEqual(selector.inFlight.size, 0)
})

test('constructor accepts sequential mode', () => {
    const selector = new PieceSelector('sequential')
    assert.strictEqual(selector.mode, 'sequential')
})

test('constructor accepts rarest-first mode', () => {
    const selector = new PieceSelector('rarest-first')
    assert.strictEqual(selector.mode, 'rarest-first')
})

// ==================== TESTS CHO addPeerBitfield() ====================

test('addPeerBitfield updates availability correctly', () => {
    const selector = new PieceSelector()
    const totalPieces = 10
    const peerPieces = [0, 2, 4, 6, 8]
    const peerBitfield = createPeerBitfield(totalPieces, peerPieces)
    
    selector.addPeerBitfield(peerBitfield, totalPieces)
    
    // Check availability cho từng piece
    for (let i = 0; i < totalPieces; i++) {
        const expected = peerPieces.includes(i) ? 1 : 0
        assert.strictEqual(selector.peerAvailability.get(i) ?? 0, expected)
    }
})

test('addPeerBitfield with multiple peers accumulates counts', () => {
    const selector = new PieceSelector()
    const totalPieces = 8
    
    // Peer 1: pieces 0,1,2
    const peer1Bitfield = createPeerBitfield(totalPieces, [0, 1, 2])
    selector.addPeerBitfield(peer1Bitfield, totalPieces)
    
    // Peer 2: pieces 1,2,3
    const peer2Bitfield = createPeerBitfield(totalPieces, [1, 2, 3])
    selector.addPeerBitfield(peer2Bitfield, totalPieces)
    
    // Expected: piece 0:1, piece1:2, piece2:2, piece3:1, others:0
    assert.strictEqual(selector.peerAvailability.get(0), 1)
    assert.strictEqual(selector.peerAvailability.get(1), 2)
    assert.strictEqual(selector.peerAvailability.get(2), 2)
    assert.strictEqual(selector.peerAvailability.get(3), 1)
    assert.strictEqual(selector.peerAvailability.get(4) ?? 0, 0)
})

test('addPeerBitfield works with totalPieces not multiple of 8', () => {
    const selector = new PieceSelector()
    const totalPieces = 10
    // Trailing bits should not affect availability
    const peerBitfield = createPeerBitfield(totalPieces, [8, 9])
    
    selector.addPeerBitfield(peerBitfield, totalPieces)
    
    assert.strictEqual(selector.peerAvailability.get(8), 1)
    assert.strictEqual(selector.peerAvailability.get(9), 1)
})

// ==================== TESTS CHO addHave() ====================

test('addHave increases availability for piece', () => {
    const selector = new PieceSelector()
    
    selector.addHave(5)
    assert.strictEqual(selector.peerAvailability.get(5), 1)
    
    selector.addHave(5)
    assert.strictEqual(selector.peerAvailability.get(5), 2)
    
    selector.addHave(10)
    assert.strictEqual(selector.peerAvailability.get(10), 1)
})

test('addHave works for pieces not previously tracked', () => {
    const selector = new PieceSelector()
    
    selector.addHave(99)
    assert.strictEqual(selector.peerAvailability.get(99), 1)
})

// ==================== TESTS CHO next() WITH PEER BITFIELD ====================

test('next() with sequential mode returns lowest missing piece', () => {
    const selector = new PieceSelector('sequential')
    const tracker = new BitfieldTracker(10)
    const peerBitfield = createPeerBitfield(10, [0, 1, 2, 3, 4, 5, 6, 7, 8, 9])
    
    // Mark some pieces as already downloaded
    tracker.set(0)
    tracker.set(1)
    
    const piece = selector.next(tracker, peerBitfield)
    assert.strictEqual(piece, 2) // Lowest missing piece that peer has
})

test('next() with sequential mode respects inFlight pieces', () => {
    const selector = new PieceSelector('sequential')
    const tracker = new BitfieldTracker(10)
    const peerBitfield = createPeerBitfield(10, [0, 1, 2, 3, 4])
    
    // Mark inFlight for piece 1
    selector.markInFlight(1)
    
    const piece = selector.next(tracker, peerBitfield)
    // Should skip piece 1 (inFlight) and return 0
    assert.strictEqual(piece, 0)
})

test('next() with sequential mode returns null when no candidates', () => {
    const selector = new PieceSelector('sequential')
    const tracker = new BitfieldTracker(10, true)
    const peerBitfield = createPeerBitfield(10, [])
    
    const piece = selector.next(tracker, peerBitfield)
    assert.strictEqual(piece, null)
})

test('next() with sequential mode only returns pieces peer has', () => {
    const selector = new PieceSelector('sequential')
    const tracker = new BitfieldTracker(10)
    // Peer only has pieces 5-9
    const peerBitfield = createPeerBitfield(10, [5, 6, 7, 8, 9])
    
    const piece = selector.next(tracker, peerBitfield)
    assert.strictEqual(piece, 5) // Lowest missing piece that peer has
    // assert.ok(false)
})

test('next() with sequential mode skips pieces we already have', () => {
    const selector = new PieceSelector('sequential')
    const tracker = new BitfieldTracker(10)
    const peerBitfield = createPeerBitfield(10, [0, 1, 2, 3, 4, 5, 6, 7, 8, 9])
    
    // We already have pieces 0-4
    for (let i = 0; i < 5; i++) {
        tracker.set(i)
    }
    
    const piece = selector.next(tracker, peerBitfield)
    assert.strictEqual(piece, 5)
})

// ==================== TESTS CHO next() WITHOUT PEER BITFIELD ====================

test('next() without peerBitfield considers all missing pieces', () => {
    const selector = new PieceSelector('sequential')
    const tracker = new BitfieldTracker(10)
    
    tracker.set(0)
    tracker.set(2)
    
    const piece = selector.next(tracker)
    assert.strictEqual(piece, 1) // Lowest missing piece
})

test('next() without peerBitfield respects inFlight pieces', () => {
    const selector = new PieceSelector('sequential')
    const tracker = new BitfieldTracker(10)
    
    selector.markInFlight(1)
    selector.markInFlight(3)
    
    const piece = selector.next(tracker)
    assert.strictEqual(piece, 0) // Lowest missing not inFlight
})

// ==================== TESTS CHO RAREST-FIRST MODE ====================

test('rarest-first returns piece with lowest availability', () => {
    const selector = new PieceSelector('rarest-first')
    const tracker = new BitfieldTracker(10)
    const peerBitfield = createPeerBitfield(10, [0, 1, 2, 3, 4, 5, 6, 7, 8, 9])
    
    // Set availability
    selector.peerAvailability.set(0, 5) // common
    selector.peerAvailability.set(1, 3) // medium
    selector.peerAvailability.set(2, 1) // rarest
    selector.peerAvailability.set(3, 2)
    
    const piece = selector.next(tracker, peerBitfield)
    // assert.strictEqual(piece, 2) // rarest piece
    assert.ok(new Set([4, 5, 6, 7, 8, 9]).has(piece))
})

test('rarest-first respects inFlight pieces', () => {
    const selector = new PieceSelector('rarest-first')
    const tracker = new BitfieldTracker(10)
    const peerBitfield = createPeerBitfield(10, [0, 1, 2, 3])
    
    selector.peerAvailability.set(0, 1)
    selector.peerAvailability.set(1, 1)
    selector.peerAvailability.set(2, 2)
    selector.peerAvailability.set(3, 2)
    
    // Mark rarest piece as inFlight
    selector.markInFlight(0)
    
    const piece = selector.next(tracker, peerBitfield)
    // Should return next rarest (piece 1)
    assert.strictEqual(piece, 1)
})

test('rarest-first treats missing availability as 0', () => {
    const selector = new PieceSelector('rarest-first')
    const tracker = new BitfieldTracker(10)
    const peerBitfield = createPeerBitfield(10, [0, 1, 2])
    
    // Only set availability for piece 0 and 2
    selector.peerAvailability.set(0, 5)
    selector.peerAvailability.set(2, 3)
    // Piece 1 has no availability (should be treated as 0)
    
    const piece = selector.next(tracker, peerBitfield)
    // Piece 1 should be chosen (availability 0 is rarest)
    assert.strictEqual(piece, 1)
})

test('rarest-first sorts correctly with mixed availability', () => {
    const selector = new PieceSelector('rarest-first')
    const tracker = new BitfieldTracker(20)
    const peerBitfield = createPeerBitfield(20, [5, 10, 15, 18])
    
    // Setup availability
    selector.peerAvailability.set(5, 10)
    selector.peerAvailability.set(10, 1)  // rarest
    selector.peerAvailability.set(15, 5)
    selector.peerAvailability.set(18, 3)
    
    const piece = selector.next(tracker, peerBitfield)
    assert.strictEqual(piece, 10)
})

test('rarest-first returns null when no candidates', () => {
    const selector = new PieceSelector('rarest-first')
    const tracker = new BitfieldTracker(10)
    const peerBitfield = createPeerBitfield(10, [])
    
    const piece = selector.next(tracker, peerBitfield)
    assert.strictEqual(piece, null)
})

// ==================== TESTS CHO inFlight MANAGEMENT ====================

test('markInFlight adds piece to inFlight set', () => {
    const selector = new PieceSelector()
    
    selector.markInFlight(5)
    assert.ok(selector.inFlight.has(5))
    
    selector.markInFlight(10)
    assert.ok(selector.inFlight.has(10))
    assert.ok(selector.inFlight.has(5))
})

test('markDone removes piece from inFlight set', () => {
    const selector = new PieceSelector()
    
    selector.markInFlight(5)
    assert.ok(selector.inFlight.has(5))
    
    selector.markDone(5)
    assert.ok(!selector.inFlight.has(5))
})

test('markFailed removes piece from inFlight set', () => {
    const selector = new PieceSelector()
    
    selector.markInFlight(5)
    assert.ok(selector.inFlight.has(5))
    
    selector.markFailed(5)
    assert.ok(!selector.inFlight.has(5))
})

test('markDone does nothing for piece not inFlight', () => {
    const selector = new PieceSelector()
    
    selector.markDone(99)
    assert.ok(!selector.inFlight.has(99))
})

// ==================== TESTS CHO SEQUENTIAL MODE EDGE CASES ====================

test('sequential mode with all pieces downloaded returns null', () => {
    const selector = new PieceSelector('sequential')
    const tracker = new BitfieldTracker(10)
    const peerBitfield = createPeerBitfield(10, [0, 1, 2, 3, 4, 5, 6, 7, 8, 9])
    
    // Download all pieces
    for (let i = 0; i < 10; i++) {
        tracker.set(i)
    }
    
    const piece = selector.next(tracker, peerBitfield)
    assert.strictEqual(piece, null)
})

test('sequential mode with all pieces inFlight returns null', () => {
    const selector = new PieceSelector('sequential')
    const tracker = new BitfieldTracker(5)
    const peerBitfield = createPeerBitfield(5, [0, 1, 2, 3, 4])
    
    // Mark all missing pieces as inFlight
    for (let i = 0; i < 5; i++) {
        selector.markInFlight(i)
    }
    
    const piece = selector.next(tracker, peerBitfield)
    assert.strictEqual(piece, null)
})

test('sequential mode with piece 0 missing and peer has it', () => {
    const selector = new PieceSelector('sequential')
    const tracker = new BitfieldTracker(10)
    const peerBitfield = createPeerBitfield(10, [0])
    
    const piece = selector.next(tracker, peerBitfield)
    assert.strictEqual(piece, 0)
})

// ==================== TESTS CHO RAREST-FIRST EDGE CASES ====================

test('rarest-first with multiple pieces same availability picks first', () => {
    const selector = new PieceSelector('rarest-first')
    const tracker = new BitfieldTracker(10)
    const peerBitfield = createPeerBitfield(10, [2, 5, 8])
    
    // All have same availability (1)
    selector.peerAvailability.set(2, 1)
    selector.peerAvailability.set(5, 1)
    selector.peerAvailability.set(8, 1)
    
    const piece = selector.next(tracker, peerBitfield)
    // Should pick the first in sorted order (2)
    assert.strictEqual(piece, 2)
})

test('rarest-first with all pieces having same availability picks lowest index', () => {
    const selector = new PieceSelector('rarest-first')
    const tracker = new BitfieldTracker(10)
    const peerBitfield = createPeerBitfield(10, [1, 3, 5, 7, 9])
    
    // Set all to same availability
    for (const piece of [1, 3, 5, 7, 9]) {
        selector.peerAvailability.set(piece, 3)
    }
    
    const piece = selector.next(tracker, peerBitfield)
    assert.strictEqual(piece, 1)
})

test('rarest-first with availability updated via addHave', () => {
    const selector = new PieceSelector('rarest-first')
    const tracker = new BitfieldTracker(10)
    const peerBitfield = createPeerBitfield(10, [1, 2, 3])
    
    // Initially, all have availability 0 (treated as 0)
    let piece = selector.next(tracker, peerBitfield)
    // Should pick first (1)
    assert.strictEqual(piece, 1)
    
    // Add more peers having piece 1
    selector.addHave(1)
    selector.addHave(1)
    
    piece = selector.next(tracker, peerBitfield)
    // Now piece 2 or 3 should be rarest (availability 0)
    assert.strictEqual(piece, 2)
})

// ==================== TESTS CHO MIXED SCENARIOS ====================

test('multiple peers with overlapping pieces', () => {
    const selector = new PieceSelector('rarest-first')
    const tracker = new BitfieldTracker(10)
    const peerBitfield = createPeerBitfield(10, [0, 1, 2, 3, 4])
    
    // Simulate multiple peers
    selector.addHave(0) // peer 2
    selector.addHave(0) // peer 3
    selector.addHave(1) // peer 2
    selector.addHave(2) // peer 2
    
    // Availability: piece0=2, piece1=1, piece2=1, piece3=0, piece4=0
    const piece = selector.next(tracker, peerBitfield)
    // Should pick piece3 or piece4 (availability 0), lowest is 3
    assert.strictEqual(piece, 3)
})

test('piece selection after download completion', () => {
    const selector = new PieceSelector('sequential')
    const tracker = new BitfieldTracker(10)
    const peerBitfield = createPeerBitfield(10, [0, 1, 2, 3, 4, 5, 6, 7, 8, 9])
    
    // Download piece 0
    selector.markInFlight(0)
    let piece = selector.next(tracker, peerBitfield)
    assert.strictEqual(piece, 1)
    
    // Complete piece 0
    tracker.set(0)
    selector.markDone(0)
    
    piece = selector.next(tracker, peerBitfield)
    assert.strictEqual(piece, 1)
    
    // Mark piece 1 as inFlight
    selector.markInFlight(1)
    piece = selector.next(tracker, peerBitfield)
    assert.strictEqual(piece, 2)
})

test('piece selection after failed download', () => {
    const selector = new PieceSelector('sequential')
    const tracker = new BitfieldTracker(10)
    const peerBitfield = createPeerBitfield(10, [0, 1, 2])
    
    // Start downloading piece 0
    selector.markInFlight(0)
    let piece = selector.next(tracker, peerBitfield)
    assert.strictEqual(piece, 1) // Skip inFlight piece 0
    
    // Piece 0 fails
    selector.markFailed(0)
    
    piece = selector.next(tracker, peerBitfield)
    assert.strictEqual(piece, 0) // Can try piece 0 again
})

// ==================== TESTS CHO REAL-WORLD SCENARIOS ====================

test('simulate VLC streaming with sequential mode', () => {
    const selector = new PieceSelector('sequential')
    const tracker = new BitfieldTracker(100)
    const peerBitfield = createPeerBitfield(100, Array.from({ length: 100 }, (_, i) => i))
    
    // Simulate playing piece by piece
    for (let expected = 0; expected < 50; expected++) {
        const piece = selector.next(tracker, peerBitfield)
        assert.strictEqual(piece, expected)
        
        // Download and mark as done
        selector.markInFlight(piece)
        tracker.set(piece)
        selector.markDone(piece)
    }
})

test('simulate rarest-first with multiple peers', () => {
    const selector = new PieceSelector('rarest-first')
    const tracker = new BitfieldTracker(20)
    const peerBitfield = createPeerBitfield(20, [5, 6, 7, 8, 9, 10, 11, 12, 13, 14])
    
    // Simulate peer availability from different peers
    // Pieces 5-9: common (many peers have)
    for (let i = 5; i <= 9; i++) {
        for (let j = 0; j < 5; j++) {
            selector.addHave(i)
        }
    }
    
    // Pieces 10-14: rare (few peers have)
    for (let i = 10; i <= 14; i++) {
        selector.addHave(i)
    }
    
    const piece = selector.next(tracker, peerBitfield)
    // Should pick from rare pieces (10-14)
    assert.ok(piece >= 10 && piece <= 14)
})

test('simulate swarm where peer disconnects', () => {
    const selector = new PieceSelector('rarest-first')
    const tracker = new BitfieldTracker(10)
    const peerBitfield = createPeerBitfield(10, [0, 1, 2])
    
    // Add availability from multiple peers
    selector.addHave(0)
    selector.addHave(0)
    selector.addHave(1)
    
    // Piece 0 is common (2 peers), piece 2 is rare (0 peers)
    const piece = selector.next(tracker, peerBitfield)
    assert.strictEqual(piece, 2) // Pick rarest first to preserve rare pieces
})

test('concurrent downloads with multiple peers', () => {
    const selector = new PieceSelector('sequential')
    const tracker = new BitfieldTracker(20)
    
    // Peer 1 has pieces 0-9
    const peer1Bitfield = createPeerBitfield(20, [0, 1, 2, 3, 4, 5, 6, 7, 8, 9])
    // Peer 2 has pieces 10-19
    const peer2Bitfield = createPeerBitfield(20, [10, 11, 12, 13, 14, 15, 16, 17, 18, 19])
    
    // Peer 1 picks piece 0
    const piece1 = selector.next(tracker, peer1Bitfield)
    assert.strictEqual(piece1, 0)
    selector.markInFlight(piece1)
    
    // Peer 2 picks piece 10 (lowest missing it has)
    const piece2 = selector.next(tracker, peer2Bitfield)
    assert.strictEqual(piece2, 10)
    selector.markInFlight(piece2)
    
    // Both pieces are now inFlight, not interfering with each other
    assert.ok(selector.inFlight.has(0))
    assert.ok(selector.inFlight.has(10))
})

// ==================== TESTS CHO edge cases with totalPieces = 0 ====================

test('handles totalPieces = 0', () => {
    const selector = new PieceSelector('sequential')
    const tracker = new BitfieldTracker(0)
    const peerBitfield = Buffer.alloc(0)
    
    const piece = selector.next(tracker, peerBitfield)
    assert.strictEqual(piece, null)
})

test('handles totalPieces = 1', () => {
    const selector = new PieceSelector('sequential')
    const tracker = new BitfieldTracker(1)
    const peerBitfield = createPeerBitfield(1, [0])
    
    let piece = selector.next(tracker, peerBitfield)
    assert.strictEqual(piece, 0)
    
    tracker.set(0)
    piece = selector.next(tracker, peerBitfield)
    assert.strictEqual(piece, null)
})

// ==================== TESTS CHO METHOD OVERLOADS ====================

test('next() with peerBitfield parameter picks only peer pieces', () => {
    const selector = new PieceSelector('sequential')
    const tracker = new BitfieldTracker(10)
    
    // Peer only has pieces 5-9
    const peerBitfield = createPeerBitfield(10, [5, 6, 7, 8, 9])
    
    const piece = selector.next(tracker, peerBitfield)
    assert.strictEqual(piece, 5)
})

test('next() without peerBitfield considers all pieces', () => {
    const selector = new PieceSelector('sequential')
    const tracker = new BitfieldTracker(10)
    
    const piece = selector.next(tracker)
    assert.strictEqual(piece, 0)
})

// ==================== TESTS CHO BIG NUMBERS ====================

test('works with large piece indices', () => {
    const selector = new PieceSelector('sequential')
    const totalPieces = 100000
    const tracker = new BitfieldTracker(totalPieces)
    
    // Create peer bitfield with random pieces
    const peerPieces = [0, 50000, 99999]
    const peerBitfield = createPeerBitfield(totalPieces, peerPieces)
    
    const piece = selector.next(tracker, peerBitfield)
    assert.strictEqual(piece, 0)
    
    selector.markInFlight(piece)
    assert.ok(selector.inFlight.has(0))
})

// ==================== PERFORMANCE TESTS ====================

test('performance with large candidate set', () => {
    const selector = new PieceSelector('rarest-first')
    const totalPieces = 100000
    const tracker = new BitfieldTracker(totalPieces)
    
    // Create peer with all pieces
    const peerBitfield = createPeerBitfield(totalPieces, 
        Array.from({ length: totalPieces }, (_, i) => i))
    
    // Add random availability
    for (let i = 0; i < totalPieces; i++) {
        selector.peerAvailability.set(i, Math.floor(Math.random() * 10))
    }
    
    const start = Date.now()
    const piece = selector.next(tracker, peerBitfield)
    const duration = Date.now() - start
    
    assert.ok(piece !== null)
    assert.ok(duration < 100, `Selection took ${duration}ms, should be < 100ms`)
})

// ==================== INTEGRATION TESTS ====================

test('full download flow with sequential mode', () => {
    const selector = new PieceSelector('sequential')
    const totalPieces = 25
    const tracker = new BitfieldTracker(totalPieces)
    const peerBitfield = createPeerBitfield(totalPieces, 
        Array.from({ length: totalPieces }, (_, i) => i))
    
    const downloaded = []
    const inFlightSet = new Set()
    
    while (downloaded.length < totalPieces) {
        const piece = selector.next(tracker, peerBitfield)
        if (piece === null) break
        
        // Simulate download
        inFlightSet.add(piece)
        selector.markInFlight(piece)
        
        // Verify piece not already downloaded
        assert.ok(!tracker.has(piece))
        
        // Complete download
        tracker.set(piece)
        downloaded.push(piece)
        inFlightSet.delete(piece)
        selector.markDone(piece)
    }
    
    assert.strictEqual(downloaded.length, totalPieces)
    assert.deepStrictEqual(downloaded, Array.from({ length: totalPieces }, (_, i) => i))
})

test('rarest-first prevents losing rare pieces', () => {
    const selector = new PieceSelector('rarest-first')
    const totalPieces = 10
    const tracker = new BitfieldTracker(totalPieces)
    const peerBitfield = createPeerBitfield(totalPieces, [0, 1, 2, 8, 9])
    
    // Piece 8 and 9 are rare (only 1 peer has them)
    selector.addHave(8)
    selector.addHave(9)
    
    // Common pieces have many peers
    for (let i = 0; i < 10; i++) {
        selector.addHave(0)
        selector.addHave(1)
        selector.addHave(2)
    }
    
    // Should pick rare pieces first
    const piece1 = selector.next(tracker, peerBitfield)
    assert.strictEqual(piece1, 8)
    selector.markInFlight(piece1)
    
    const piece2 = selector.next(tracker, peerBitfield)
    assert.strictEqual(piece2, 9)
})