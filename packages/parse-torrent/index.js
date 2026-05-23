import bencode from 'bencode'
import crypto from 'crypto'
import path from 'path'

// ─── Helpers ────────────────────────────────────────────────────────────────

/**
 * Decode a bencode Buffer/Uint8Array to UTF-8 string.
 * @param {Buffer|Uint8Array} buf
 * @returns {string}
 */
const bufToString = (buf) => Buffer.from(buf).toString('utf-8')

/**
 * Assert a required field exists in the torrent dict.
 * Throws a descriptive error if the field is falsy.
 * @param {*}      value     - The field value to check
 * @param {string} fieldName - Human-readable field path (e.g. 'info.name')
 */
function ensure(value, fieldName) {
    if (!value) throw new Error(`Torrent is missing required field: ${fieldName}`)
}

/**
 * Split the concatenated SHA-1 hashes buffer into an array of hex strings.
 * Each piece hash is exactly 20 bytes (160-bit SHA-1).
 *
 * @param {Buffer} buf - Raw pieces buffer from torrent.info.pieces
 * @returns {string[]} Array of 40-char hex strings, one per piece
 */
function splitPieces(buf) {
    const pieces = []
    for (let i = 0; i < buf.length; i += 20) {
        pieces.push(Buffer.from(buf.slice(i, i + 20)).toString('hex'))
    }
    return pieces
}

/**
 * Resolve the OS-native relative path for a file inside the torrent.
 * Handles both multi-file (file.path array) and single-file (no path) layouts.
 *
 * Multi-file:  "torrentName/subdir/file.ext"
 * Single-file: "torrentName"  (file.path is absent → concat with [])
 *
 * @param {string}              torrentName - Top-level torrent name
 * @param {Buffer[]|string[]|undefined} filePath - Raw path segments from bencode
 * @returns {string} Relative OS path without leading separator
 */
function resolveFilePath(torrentName, filePath) {
    const parts = []
        .concat(torrentName, filePath || [])
        .map(p => ArrayBuffer.isView(p) ? bufToString(p) : p)

    // path.join needs spread args, not an array.
    // Prepend sep so join resolves correctly, then strip the leading slash.
    return path.join.apply(null, [path.sep].concat(parts)).slice(1)
}

// ─── Validation ─────────────────────────────────────────────────────────────

/**
 * Validate all required fields in the raw decoded torrent dict.
 * Throws if any required field is missing or has wrong type.
 *
 * @param {Object} torrent - Raw object from bencode.decode()
 */
function validateTorrent(torrent) {
    ensure(
        torrent.announce ||
        (Array.isArray(torrent['announce-list']) && torrent['announce-list'].length > 0),
        "torrent.announce or torrent['announce-list']"
    )
    ensure(torrent.info,                                            'info')
    ensure(torrent.info['name.utf-8'] || torrent.info.name,         'info.name')
    ensure(torrent.info['piece length'],                            "info['piece length']")
    ensure(torrent.info.pieces,                                     'info.pieces')

    if (torrent.info.files) {
        torrent.info.files.forEach(file => {
            ensure(typeof file.length === 'number',           'info.files.$.length')
            ensure(file['path.utf-8'] || file.path,           'info.files.$.path')
        })
    } else {
        ensure(typeof torrent.info.length === 'number',       'info.length')
    }
}

// ─── Field extractors ────────────────────────────────────────────────────────

/**
 * Compute the SHA-1 info hash from the bencoded info dictionary.
 * This is the canonical torrent identifier used by trackers and peers.
 *
 * @param {Buffer} infoBuffer - Bencoded info dict (bencode.encode(torrent.info))
 * @returns {{ infoHashBuffer: Buffer, infoHash: string }}
 */
function computeInfoHash(infoBuffer) {
    const infoHashBuffer = crypto
        .createHash('sha1')
        .update(Buffer.from(infoBuffer))
        .digest()
    return {
        infoHashBuffer,
        infoHash: infoHashBuffer.toString('hex')
    }
}

/**
 * Extract and deduplicate tracker announce URLs.
 *
 * BEP-12: if announce-list is present, it takes priority and
 * the single announce field is ignored.
 *
 * @param {Object} torrent - Raw decoded torrent dict
 * @returns {string[]} Deduplicated list of tracker URLs
 */
function extractAnnounce(torrent) {
    const urls = []

    if (Array.isArray(torrent['announce-list']) && torrent['announce-list'].length > 0) {
        // announce-list is a list of tiers, each tier is a list of URLs
        torrent['announce-list'].forEach(tier =>
            tier.forEach(url => urls.push(bufToString(url)))
        )
    } else if (torrent.announce) {
        urls.push(bufToString(torrent.announce))
    }

    return Array.from(new Set(urls))
}

/**
 * Build the normalized file list with byte offsets.
 * Works for both single-file and multi-file torrents.
 *
 * Single-file layout: torrent.info has no .files array;
 * we use torrent.info itself as the sole entry.
 *
 * @param {Object} torrent    - Raw decoded torrent dict
 * @param {string} torrentName - Decoded torrent name (used as root dir)
 * @returns {{ files: Array, totalLength: number }}
 *
 * Each file entry:
 *   path   {string} - OS-native relative path  e.g. "name/sub/file.ext"
 *   name   {string} - Filename only             e.g. "file.ext"
 *   length {number} - Byte size
 *   offset {number} - Byte offset from start of torrent data
 */
function extractFiles(torrent, torrentName) {
    const rawFiles = torrent.info.files || [torrent.info]
    let offset = 0

    const files = rawFiles.map(file => {
        const filePath = resolveFilePath(torrentName, file['path.utf-8'] || file.path)
        const entry = {
            path:   filePath,
            name:   path.basename(filePath),
            length: file.length,
            offset
        }
        offset += file.length
        return entry
    })

    return { files, totalLength: offset }
}

// ─── Main ────────────────────────────────────────────────────────────────────

/**
 * Parse a .torrent file buffer into a structured object.
 *
 * Synchronous — blocks the event loop while decoding and hashing.
 *
 * Supports:
 *   - Single-file and multi-file torrents
 *   - UTF-8 and legacy encoded names/paths
 *   - announce and announce-list (BEP-12)
 *   - Web seed url-list (BEP-19)
 *
 * Does NOT support:
 *   - Magnet URIs
 *   - Info-hash-only identifiers
 *   - Metadata fetched via ut_metadata
 *
 * @param  {Buffer|Uint8Array} data - Raw .torrent file bytes
 * @returns {{
 *   info:            Object,    - Raw info dict (needed for extension protocols)
 *   infoBuffer:      Buffer,    - Bencoded info dict
 *   infoHashBuffer:  Buffer,    - 20-byte SHA-1 info hash
 *   infoHash:        string,    - 40-char hex info hash
 *   name:            string,    - Torrent display name
 *   announce:        string[],  - Tracker URLs (deduped, BEP-12 priority)
 *   urlList:         string[],  - Web seed URLs (BEP-19)
 *   created:         Date|null,
 *   createdBy:       string|null,
 *   comment:         string|null,
 *   length:          number,    - Total byte size of all files
 *   pieceLength:     number,    - Bytes per piece (except last)
 *   lastPieceLength: number,    - Bytes in the final piece
 *   pieces:          string[],  - Hex SHA-1 hash per piece
 *   files:           Array      - Normalized file list (see extractFiles)
 * }}
 * @throws {Error} If data is not a Buffer/Uint8Array
 * @throws {Error} If any required torrent field is missing
 */
function parse(data) {
    if (!ArrayBuffer.isView(data)) {
        throw new Error('parse: expected a Buffer or Uint8Array')
    }

    const torrent = bencode.decode(data)
    validateTorrent(torrent)

    const name       = bufToString(torrent.info['name.utf-8'] || torrent.info.name)
    const infoBuffer = bencode.encode(torrent.info)
    const { infoHashBuffer, infoHash } = computeInfoHash(infoBuffer)
    const announce   = extractAnnounce(torrent)
    const { files, totalLength } = extractFiles(torrent, name)
    const pieceLength = torrent.info['piece length']

    return {
        // ── Identity ──────────────────────────────────────────────────────
        info:           torrent.info,
        infoBuffer,
        infoHashBuffer,
        infoHash,
        name,

        // ── Sources ───────────────────────────────────────────────────────
        announce,
        urlList: (torrent['url-list'] || []).map(url => bufToString(url)),

        // ── Metadata ──────────────────────────────────────────────────────
        created:   torrent['creation date'] ? new Date(torrent['creation date'] * 1000) : null,
        createdBy: torrent['created by']    ? bufToString(torrent['created by'])        : null,
        comment:   ArrayBuffer.isView(torrent.comment) ? bufToString(torrent.comment)   : null,

        // ── Layout ────────────────────────────────────────────────────────
        length:          totalLength,
        pieceLength,
        // Last piece is usually shorter than pieceLength.
        // If total size is an exact multiple of pieceLength, last piece is full-sized.
        lastPieceLength: (totalLength % pieceLength) || pieceLength,
        pieces:          splitPieces(torrent.info.pieces),
        files,
    }
}

export default parse