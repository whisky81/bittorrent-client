// 2 mode:
//  sequential   → piece index thấp nhất còn thiếu (tốt cho VLC streaming)
//  rarest-first → piece ít peer nhất có (tốt cho swarm health)
//
// inFlight set: track pieces đang được download bởi bất kỳ peer nào
// → tránh 2 peer cùng download 1 piece trùng nhau
class PieceSelector {
    constructor(mode = 'sequential') {
        this.mode = mode
        // peerAvailability[i] = tổng số peer trong swarm có piece i
        // dùng cho rarest-first: sort ascending → pick piece hiếm nhất
        this.peerAvailability = new Map()

        // pieces đang được download bởi peer nào đó — loại khỏi candidates
        this.inFlight = new Set()
    }
    // peer kết nối mới → update availability từ bitfield của nó
    addPeerBitfield(bf, totalPieces) {
        for (let i = 0; i < totalPieces; ++i) {
            if (bf[Math.floor(i / 8)] & (1 << (7 - (i % 8)))) {
                this.peerAvailability.set(i, (this.peerAvailability.get(i) ?? 0) + 1)
            }
        }
    }
    // peer broadcast wire.on('have',(i)=>{}) → tăng availability của piece i
    addHave(i) {
        this.peerAvailability.set(i, (this.peerAvailability.get(i) ?? 0) + 1)
    }
    // chọn piece tiếp theo để request từ peer này
    // tracker: BitfieldTracker — những gì chúng ta đã có
    // peerBitfield: Buffer — những gì PEER NÀY có
    next(tracker, peerBitfield = null) {
        console.log(peerBitfield.constructor.name)
        console.log(peerBitfield)
        // console.log(tracker.missing())
        const candidates = tracker.missing().filter(i => {
            // console.log(i, !this.inFlight.has(i), (peerBitfield === null ? true : !!(peerBitfield[Math.floor(i / 8)] & (1 << (7 - (i % 8))))))
            return !this.inFlight.has(i) && (peerBitfield === null ? true : !!(peerBitfield[Math.floor(i / 8)] & (1 << (7 - (i % 8)))))

        })
        return this._next(candidates)
    }
    _next(candidates) {
        if (candidates.length === 0) {
            return null 
        }
        if (this.mode === 'sequential') {
            // candidates đã sorted vì missing() iterate 0→N
            return candidates[0]
        }
        // rarest-first: sort by peerAvailability ascending
        // piece nào ít peer có nhất → ưu tiên download trước
        // (nếu peer đó disconnect thì swarm mất piece đó vĩnh viễn)
        candidates.sort((a, b) =>
            (this.peerAvailability.get(a) ?? 0) - (this.peerAvailability.get(b) ?? 0)
        )
        return candidates[0]
    }
    markInFlight(i) { this.inFlight.add(i) }
    markDone(i) { this.inFlight.delete(i) }
    markFailed(i) { this.inFlight.delete(i) }
}

export default PieceSelector