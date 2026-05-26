class BitfieldTracker {
    constructor(totalPieces, full = false) {
        this.totalPieces = totalPieces
        const totalBytes = Math.ceil(totalPieces / 8)
        this.bitfield = full ? Buffer.alloc(totalBytes, 0xff) : Buffer.alloc(totalBytes)

        const remainder = totalPieces % 8
        if (full && remainder !== 0) {
            this.bitfield[totalBytes - 1] = 0xFF << (8 - remainder) & 0xFF
        }
    }

    set(i) {
        if (i >= this.totalPieces) return
        this.bitfield[Math.floor(i / 8)] |= (1 << (7 - (i % 8)))
    }

    has(i) {
        if (i >= this.totalPieces) return false
        return !!(this.bitfield[Math.floor(i / 8)] & (1 << (7 - (i % 8))))
    }

    // trả về array index của các piece còn thiếu
    missing() {
        const out = []
        for (let i = 0; i < this.totalPieces; ++i) if (!this.has(i)) out.push(i)
        return out
    }

    get count() {
        let n = 0
        for (let i = 0; i < this.totalPieces; ++i) if (this.has(i)) ++n
        return n
    }
}

export default BitfieldTracker