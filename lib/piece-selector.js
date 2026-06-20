class PieceSelector {
  constructor(mode = 'sequential') {
    this.mode = mode;
    this.peerAvailability = new Map();
    this.inFlight = new Set();
  }
  addPeerBitfield(bf, totalPieces) {
    for (let i = 0; i < totalPieces; ++i) {
      if (bf[Math.floor(i / 8)] & (1 << (7 - (i % 8)))) {
        this.peerAvailability.set(i, (this.peerAvailability.get(i) ?? 0) + 1);
      }
    }
  }
  addHave(i) {
    this.peerAvailability.set(i, (this.peerAvailability.get(i) ?? 0) + 1);
  }
  next(tracker, peerBitfield = null) {
    const candidates = tracker.missing().filter((i) => {
      return (
        !this.inFlight.has(i) &&
        (peerBitfield === null ? true : !!(peerBitfield[Math.floor(i / 8)] & (1 << (7 - (i % 8)))))
      );
    });
    return this._next(candidates);
  }
  _next(candidates) {
    if (candidates.length === 0) {
      return null;
    }
    if (this.mode === 'sequential') {
      return candidates[0];
    }
    candidates.sort(
      (a, b) => (this.peerAvailability.get(a) ?? 0) - (this.peerAvailability.get(b) ?? 0)
    );
    return candidates[0];
  }
  markInFlight(i) {
    this.inFlight.add(i);
  }
  markDone(i) {
    this.inFlight.delete(i);
  }
  markFailed(i) {
    this.inFlight.delete(i);
  }
}

export default PieceSelector;
