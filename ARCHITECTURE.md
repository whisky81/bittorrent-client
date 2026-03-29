# pytorrent — Kiến trúc & Hướng dẫn đọc code

> BitTorrent client viết bằng Python, gồm engine asyncio và Web UI (Flask).

---

## Sơ đồ tổng quan

```
┌─────────────────────────────────────────────────────────────────┐
│  Web UI (Flask — main thread)                                   │
│  web/app.py          web/templates/index.html                   │
│    │                                                            │
│    │  asyncio.run_coroutine_threadsafe()                        │
│    ▼                                                            │
│  torrent_loop  (background asyncio event loop — daemon thread)  │
│    │                                                            │
│    ├── Torrent.init()                                           │
│    │     ├── _bind_tcp_server()   ← mở port TCP 6881-6889       │
│    │     ├── _setup_nat()         ← UPnP / PCP                  │
│    │     └── _contact_trackers()  ← lấy danh sách peer         │
│    │                                                            │
│    ├── Torrent.download_files()                                 │
│    │     └── FilesDownloadManager.get_file()                    │
│    │           └── Piece.download()  ←→ Peer.fetch_blocks()     │
│    │                                                            │
│    └── Torrent.seed()             ← giữ coroutine sống vô hạn  │
│          (TCP server, maintain_peers, announce_loop chạy nền)   │
└─────────────────────────────────────────────────────────────────┘
```

---

## Cây thư mục

```
pytorrent/                        ← package chính
│
├── __init__.py                   ← cấu hình logging toàn dự án
├── torrent.py                    ← class Torrent (orchestrator)
├── peer.py                       ← class Peer (1 kết nối TCP tới peer)
├── piece.py                      ← class Piece (download 1 piece)
├── downloader.py                 ← FilesDownloadManager (pipeline download)
├── download_manager.py           ← DownloadManager (JSON persistence)
├── tracker_factory.py            ← factory: udp/http → tracker object
│
└── core/                         ← thư viện nội bộ (không phụ thuộc nhau)
    ├── __init__.py               ← rỗng
    ├── constants.py              ← hằng số giao thức
    ├── bencode_wrapper.py        ← encode/decode bencode (wrap fastbencode)
    ├── file_utils.py             ← File, FileTree (metadata file trong torrent)
    ├── nat_traversal.py          ← UPnP + PCP (RFC 6887) port mapping
    ├── trackers.py               ← UDPTracker, HTTPTracker
    ├── utils.py                  ← Block, PieceWriter, PieceReader, gen_peer_id
    ├── pwp_message_generator.py  ← tạo PWP message bytes
    ├── pwp_response_parse.py     ← parse byte stream → artifacts dict
    └── pwp_response_handler.py   ← xử lý artifacts → actions

web/
├── app.py                        ← Flask app (REST API + SSE)
└── templates/index.html          ← Single-page UI
```

---

## Luồng download — bước-bước

### 1. Khởi tạo (`Torrent.init`)

```
torrent_file (.torrent)
    → bencode_wrapper.bdecode()
    → tách: info_hash, piece_hashes, tracker list, file list

_bind_tcp_server()              → tìm port trống 6881–6889
_setup_nat() ‖ _contact_trackers()   ← chạy song song (asyncio.gather)

_setup_nat():
    NATTraversal.setup()
        1. UPnP (miniupnpc) → addportmapping(external_port → internal_port)
        2. PCP  (RFC 6887)  → UDP MAP request tới gateway:5351
    → lưu external_ip, external_port vào torrent_info

_contact_trackers():
    TrackerFactory → UDPTracker | HTTPTracker
    tracker.get_peers() → danh sách (ip, port)
    → lưu vào tracker.peers

→ Tạo Peer objects, connect(), handshake(), interested()
→ Khởi động background tasks: announce_loop, maintain_peers, nat_renewal_loop
```

### 2. Download (`FilesDownloadManager.get_file`)

```
create_pieces_queue(file)
    _rarity_order() → sắp xếp piece theo "rarest-first"
    → PriorityQueue[piece_num]

Vòng lặp chính:
    while file_pieces not empty:
        dequeue piece_num
        asyncio.create_task(Piece.download(peer_queue))

    # End-game (≤4 pieces còn lại):
    _end_game_download() → gửi request tới MỌI peer cùng lúc

Thu thập kết quả (pending_tasks):
    Piece.is_valid() → SHA-1 check
    _finalize()      → lưu full piece vào completed_pieces cache
    _slice_piece_for_file() → trích đúng bytes cho file này  ← BUG FIX KEY
    PieceWriter.write()     → ghi ra disk
```

### 3. Piece download (`Piece.download`)

```
peer_queue.get() → chọn peer ưu tiên cao nhất

while not is_piece_complete():
    gen_offsets() → set các block offset chưa có
    fetch_blocks(offsets, peer):
        write_only(REQUEST messages)   ← gửi tất cả request cùng lúc
        stream_pieces(n)               ← đọc n PIECE responses
        Parser().parse()               ← raw bytes → artifacts
        Handler().handle()             ← artifacts → Block list
    lưu blocks vào self.blocks dict
    self._last_peer = peer             ← để downloader penalize nếu hash fail

assemble: self.data = blocks[0] + blocks[1] + ... + blocks[N]
return self
```

### 4. Seeding (`Torrent.seed`)

```
seed() chỉ giữ coroutine sống (while True: sleep(60)).
Seeding thực được xử lý bởi:

TCP server (handle_incoming_connection):
    peer kết nối vào → handshake → gửi bitfield → listen_forever()

listen_forever() (Peer):
    đọc messages → Parser → Handler
    Handler.handle_request():
        bitfield[piece_index] → True?  ← ta có piece này không?
        PieceReader.read()              ← đọc từ disk
        write_only(PIECE message)       ← gửi cho peer
        torrent_info["uploaded"] += len(data)
```

---

## Peer Wire Protocol (PWP) — Message flow

```
Bên tải về (leecher):        Bên chia (seeder):
    HANDSHAKE         →
                      ←      HANDSHAKE
    BITFIELD          →      (ta có gì)
                      ←      BITFIELD  (họ có gì)
    INTERESTED        →
                      ←      UNCHOKE
    REQUEST(piece,off,len) →
                      ←      PIECE(piece, off, data)
    HAVE(piece_num)   →      (broadcast sau khi xác nhận piece)
```

**Files liên quan:**
- `pwp_message_generator.py` → tạo bytes cho mỗi message type
- `pwp_response_parse.py` → parse byte stream, trả về `artifacts` dict
- `pwp_response_handler.py` → xử lý `artifacts`, thực hiện actions

---

## NAT Traversal — Tại sao cần và cách hoạt động

### Vấn đề

Khi client ở sau NAT (router nhà), peers bên ngoài không thể kết nối vào.
Chỉ có thể kết nối ra ngoài (outgoing). Điều này khiến `uploaded = 0` và
seeding không hiệu quả.

### Giải pháp: Port mapping tự động

```
Internet
   │
[Router/NAT]  ← cần thêm rule: "port 6881 TCP → 192.168.1.x:6881"
   │
[LAN: 192.168.1.x]
   │
[pytorrent: bind 0.0.0.0:6881]
```

**Chiến lược (theo thứ tự ưu tiên):**

1. **UPnP IGD** (`miniupnpc`): phổ biến nhất, router cũ và mới đều hỗ trợ
2. **PCP** (RFC 6887): kế nhiệm NAT-PMP, router 2012+, dùng 96-bit nonce

**PCP vs NAT-PMP:**

| Tính năng | NAT-PMP (RFC 6886) | PCP (RFC 6887) |
|-----------|---------------------|----------------|
| Version   | 0                   | 2              |
| Security  | Không có nonce      | 96-bit nonce   |
| Ext IP    | 2 bước riêng        | Trong MAP response |
| IPv6      | Không               | Có             |
| Port      | 5351/UDP            | 5351/UDP       |

**Lưu ý:** PCP được mở **một lần khi khởi động torrent** (trong `init()`),
không phải theo từng peer message. Đây là hoạt động network-level để router
biết forward incoming TCP connections về máy của bạn.

### Renewal

Mapping có lifetime (mặc định 2 giờ). `_nat_renewal_loop()` gia hạn mỗi
45 phút (`RENEW_INTERVAL = 2700s`) để tránh bị expire.

---

## Web UI — Kiến trúc

```
Browser                            Flask (main thread)
   │                                      │
   │── GET /api/stream ──────────────────►│ SSE stream (1 connection liên tục)
   │◄─ data: {torrents, notifications} ───│ (mỗi 1 giây)
   │                                      │
   │── POST /api/upload ────────────────►│ nhận .torrent file
   │◄─ {info_hash, files} ───────────────│
   │                                      │
   │── POST /api/start ─────────────────►│ asyncio.run_coroutine_threadsafe()
   │◄─ {success} ────────────────────────│     ↓
   │                                      │ torrent_loop (asyncio)
   │                                      │   Torrent.init()
   │                                      │   Torrent.download_files()
   │                                      │   Torrent.seed()
```

**Threading model:**
- Flask chạy trên main thread
- `torrent_loop` là asyncio event loop riêng trong daemon thread
- Giao tiếp qua `asyncio.run_coroutine_threadsafe()` + `threading.Lock`
- Notifications dùng `deque(maxlen=100)` + `_notifications_lock`

---

## Các bug đã được sửa (lịch sử)

| Bug | File | Mô tả |
|-----|------|-------|
| Slice offset sai khi start_piece==end_piece | `downloader.py` | Áp dụng start và end tuần tự thay vì cùng lúc lên piece gốc → ghi sai data → SHA-1 fail vĩnh viễn |
| `ValueError` crash asyncio event loop | `trackers.py` | `parse_udp_announce_res` raise trong `datagram_received` callback |
| `artifacts did not shrink` infinite loop | `pwp_response_handler.py` | `handshake` artifact không được pop khi disconnect |
| Stream desync: message_len hàng tỷ bytes | `pwp_response_parse.py` | Không có sanity check → parser bị treo |
| SHA-1 failure lặp từ cùng peer xấu | `downloader.py`, `piece.py` | Không penalize peer gây hash failure |
| `seed()` double-schedule `listen_forever` | `torrent.py` | `gather()` các task đã có trong background |
| `__init__.py` chứa duplicate tracker code | `core/__init__.py` | Import shadowing gây undefined behavior |
| `last_piece=0` khi size chia hết piece_size | `downloader.py` | `piece_info["last_piece"]=0` → last piece có 0 blocks |

---

## Hằng số quan trọng (`core/constants.py`)

| Hằng số | Giá trị | Ý nghĩa |
|---------|---------|---------|
| `BLOCK_SIZE` | 16KB (2^14) | Đơn vị nhỏ nhất download |
| `BLOCKS_PER_CYCLE` | 16 | Pipeline: 16 blocks × 16KB = 256KB in-flight |
| `MAX_BLOCKS_PER_CYCLE` | 64 | 64 × 16KB = 1MB in-flight tối đa |
| `HANDSHAKE_LEN` | 68 | Độ dài handshake message |
| `UDP_PROTOCOL_ID` | `0x41727101980` | Magic number UDP tracker |

---

## Cách thêm tính năng mới

### Thêm message type PWP mới

1. `constants.py`: thêm hằng số ID
2. `pwp_message_generator.py`: thêm hàm `gen_xxx_msg()`
3. `pwp_response_parse.py`: thêm `parse_xxx()`, đăng ký trong `self.messages`
4. `pwp_response_handler.py`: thêm `handle_xxx()`, gọi trong `handle()`

### Thêm tracker protocol mới

1. `trackers.py`: tạo class kế thừa `Tracker`, implement `get_peers()`
2. `tracker_factory.py`: thêm scheme vào `tracker_types` dict

### Thêm API endpoint mới

1. `web/app.py`: thêm `@app.route()` function
2. `web/templates/index.html`: gọi từ JavaScript

---

## Luồng dữ liệu piece (ví dụ multi-file)

```
Torrent: piece_length=512KB
File A: 800KB  (piece 0 full + piece 1 bytes 0–287KB)
File B: 700KB  (piece 1 bytes 288KB–511KB + piece 2 bytes 0–475KB)

Piece 1 (512KB) = [File_A_tail | File_B_head]
                   [0–287KB]    [288–511KB]

Khi ghi File A, piece 1:
    _slice_piece_for_file(piece.data, 1, fileA)
    → s=0 (fileA.start_piece=0≠1), e=288KB (fileA.end_piece=1)
    → piece.data[0:288KB] ✓

Khi ghi File B, piece 1 (từ cache):
    _slice_piece_for_file(completed_pieces[1], 1, fileB)
    → s=288KB (fileB.start_piece=1), e=len(piece.data) (fileB.end_piece=2≠1)
    → piece.data[288KB:] ✓

Khi ghi File B, piece 2:
    _slice_piece_for_file(piece.data, 2, fileB)
    → s=0 (fileB.start_piece=1≠2), e=476KB (fileB.end_piece=2)
    → piece.data[0:476KB] ✓
```
