import io
import time
import hashlib
import asyncio
import copy
import json
from bitstring import BitArray
import logging

from .core.file_utils import FileTree
from .core import bencode_wrapper
from .core.utils import PieceWriter, gen_secure_peer_id
from .core.nat_traversal import NATTraversal
from .peer import Peer
from .tracker_factory import TrackerFactory
from .downloader import FilesDownloadManager
from pathlib import Path
from .core.constants import PORT

logger = logging.getLogger(__name__)

PEER_MAX_FAILS = 5
PEER_MAX_DISCONNECTS = 10
PEER_MAX_CHOKES = 30

_BITTORRENT_PORT_RANGE = range(6881, 6890)


class Torrent:
    def __init__(self, torrent_file, save_dir: Path | str | None = None) -> None:
        if isinstance(torrent_file, io.IOBase):
            bencoded_data = torrent_file.read()
        else:
            with open(torrent_file, "rb") as f:
                bencoded_data = f.read()

        metainfo        = bencode_wrapper.bdecode(bencoded_data)
        announce        = metainfo.get("announce", None)       # type: ignore
        announce_list   = metainfo.get("announce-list", None)  # type: ignore
        self.name               = metainfo["info"]["name"]           # type: ignore
        self.has_multiple_files = "files" in metainfo["info"]        # type: ignore
        pieces                  = metainfo["info"]["pieces"]         # type: ignore
        piece_length            = metainfo["info"]["piece length"]   # type: ignore

        self.trackers   = []
        self.peers      = []
        self.files      = None
        self.downloader = None
        self.TARGET_PEER_COUNT = 30
        self.port  = PORT
        self._nat: NATTraversal | None = None

        self._tracker_intervals: dict[str, int] = {}
        self._next_announce_time: float = 0.0

        files = metainfo["info"]["files"] if self.has_multiple_files else self.name  # type: ignore

        size = 0
        if self.has_multiple_files:
            size = sum(f["length"] for f in files)  # type: ignore
        else:
            size = metainfo["info"]["length"]  # type: ignore

        bencoded_info = bencode_wrapper.bencode(metainfo["info"])  # type: ignore
        info_hash     = hashlib.sha1(bencoded_info).digest()

        piece_hashes = []
        if len(pieces) % 20 != 0:
            raise ValueError("corrupted pieces")
        num_pieces = len(pieces) // 20
        for i in range(num_pieces):
            piece_hashes.append(pieces[i * 20 : (i + 1) * 20])

        trackers = []
        if announce:
            trackers.append(announce)
        if announce_list:
            for tier in announce_list:
                for tracker in tier:
                    if tracker not in trackers:
                        trackers.append(tracker)

        self.torrent_info = {
            "name":          self.name,
            "size":          size,
            "files":         files,
            "piece_length":  piece_length,
            "info_hash":     info_hash,
            "piece_hashes":  piece_hashes,
            "peers":         [],
            "trackers":      trackers,
            "peer_id":       gen_secure_peer_id(),
            "downloaded":    0,
            "uploaded":      0,
            "port":          PORT,   # local port, updated after bind
            "external_ip":   None,   # filled by NAT traversal
            "external_port": PORT,   # external port, updated by NAT traversal
        }

        self.files = FileTree(self.torrent_info)
        self.bitfield = BitArray(num_pieces)
        self.torrent_info["bitfield"]        = self.bitfield
        self.torrent_info["broadcast_have"]  = self.broadcast_have

        base_dir = Path(save_dir) if save_dir else Path.cwd() / "downloads"
        self.save_dir = base_dir / self.name
        self.save_dir.mkdir(parents=True, exist_ok=True)

    # ─────────────────────────────────────────────────────────────
    # TCP server binding (dynamic port)
    # ─────────────────────────────────────────────────────────────

    async def _bind_tcp_server(self) -> int | None:
        for try_port in _BITTORRENT_PORT_RANGE:
            try:
                self.server = await asyncio.start_server(
                    self.handle_incoming_connection, "0.0.0.0", try_port
                )
                logger.info(f"Torrent: TCP Server on 0.0.0.0:{try_port}")
                return try_port
            except OSError:
                continue

        try:
            self.server = await asyncio.start_server(
                self.handle_incoming_connection, "0.0.0.0", 0
            )
            bound_port = self.server.sockets[0].getsockname()[1]
            logger.info(f"Torrent: TCP Server on OS-assigned port {bound_port}")
            return bound_port
        except Exception as e:
            logger.error(f"Torrent: Failed to start TCP Server: {e}")
            return None

    # ─────────────────────────────────────────────────────────────
    # NAT traversal
    # ─────────────────────────────────────────────────────────────

    async def _setup_nat(self, internal_port: int):
        """
        Thử UPnP → NAT-PMP để mở cổng trên router.
        Kết quả được lưu vào torrent_info để announce đúng địa chỉ.
        """
        self._nat = NATTraversal(internal_port)
        ext_ip, ext_port = await self._nat.setup()

        self.torrent_info["external_ip"]   = ext_ip
        self.torrent_info["external_port"] = ext_port

        if ext_ip:
            logger.info(f"NAT traversal OK: external {ext_ip}:{ext_port}")
        else:
            logger.warning(
                f"NAT traversal failed — client sẽ không nhận được kết nối vào "
                f"nếu port {internal_port} chưa được forward trên router."
            )

    async def _nat_renewal_loop(self):
        if self._nat:
            await self._nat.renewal_loop()

    def cleanup_nat(self):
        """Xóa port mapping khi tắt. Gọi từ signal handler hoặc finally."""
        if self._nat:
            self._nat.cleanup()

    def get_nat_status(self) -> dict:
        if self._nat:
            return self._nat.status
        return {"method": None, "active": False,
                "internal_port": self.port,
                "external_port": self.torrent_info.get("external_port", self.port),
                "external_ip": None}

    # ─────────────────────────────────────────────────────────────
    # Tracker interval helpers
    # ─────────────────────────────────────────────────────────────

    def _update_tracker_intervals(self):
        from .core.trackers import HTTPTracker
        for tracker in self.trackers:
            if not getattr(tracker, "active", False):
                continue
            resp = getattr(tracker, "announce_response", {}) or {}
            if isinstance(tracker, HTTPTracker):
                interval = resp.get("interval", resp.get(b"interval", 1800))
            else:
                interval = resp.get("interval", 1800)
            self._tracker_intervals[repr(tracker)] = int(interval)

    def _next_sleep_seconds(self) -> int:
        return min(self._tracker_intervals.values()) if self._tracker_intervals else 1800

    # ─────────────────────────────────────────────────────────────
    # Init / connection
    # ─────────────────────────────────────────────────────────────

    async def broadcast_have(self, piece_index):
        if hasattr(self, "peers"):
            tasks = [
                p.send_have(piece_index)
                for p in self.peers
                if getattr(p, "has_handshaked", False) and getattr(p, "active", False)
            ]
            if tasks:
                await asyncio.gather(*tasks)

    async def init(self, save_dir: Path | str | None = None):
        if save_dir:
            base = Path(save_dir)
            self.save_dir = base / self.name if base.name != self.name else base

        self.save_dir.mkdir(parents=True, exist_ok=True)
        self.torrent_info["save_dir"] = self.save_dir

        # 1. Bind TCP server (dynamic port in BitTorrent range)
        bound_port = await self._bind_tcp_server()
        if bound_port:
            self.port = bound_port
            self.torrent_info["port"]          = bound_port
            self.torrent_info["external_port"] = bound_port  # default before NAT

        # 2. NAT traversal + tracker contact run concurrently
        nat_task     = asyncio.create_task(self._setup_nat(bound_port or PORT))
        tracker_task = asyncio.create_task(self._contact_trackers())
        await asyncio.gather(nat_task, tracker_task)

        peer_addrs = self._get_peers()

        self.peers = [Peer(addr, self.torrent_info) for addr in peer_addrs]
        await asyncio.gather(*[p.connect()    for p in self.peers])
        await asyncio.gather(*[p.handshake()  for p in self.peers])
        await asyncio.gather(*[p.interested() for p in self.peers])

        if not getattr(self, "_loops_started", False):
            asyncio.create_task(self._tracker_announce_loop())
            asyncio.create_task(self.maintain_peers())
            asyncio.create_task(self._nat_renewal_loop())
            self._loops_started = True

        self.torrent_info["peers"] = peer_addrs

        nat_st          = self.get_nat_status()
        active_peers    = sum(1 for p in self.peers if p.has_handshaked)
        active_trackers = sum(1 for t in self.trackers if t.active)
        logger.info(
            f"Torrent init complete: local_port={self.port}, "
            f"NAT={nat_st.get('method', 'none')} "
            f"ext={nat_st.get('external_ip')}:{nat_st.get('external_port')}, "
            f"{active_trackers} trackers, {active_peers} peers."
        )

    async def handle_incoming_connection(self, reader, writer):
        address = writer.get_extra_info("peername")
        peer    = Peer(address, self.torrent_info)
        peer.reader = reader
        peer.writer = writer
        peer.active = True

        try:
            handshake_data = await asyncio.wait_for(peer.reader.read(68), timeout=5)
            if not handshake_data:
                await peer.disconnect("Empty handshake")
                return

            from .core.pwp_response_parse   import PeerResponseParser as Parse
            from .core.pwp_response_handler import PeerResponseHandler as Handler

            artifacts = Parse(handshake_data).parse()
            await Handler(artifacts, peer).handle()

            if peer.has_handshaked:
                from .core.pwp_message_generator import gen_handshake_msg, gen_bitfield_msg
                peer.writer.write(gen_handshake_msg(
                    self.torrent_info["info_hash"], self.torrent_info["peer_id"]
                ))
                await peer.writer.drain()

                if getattr(self, "bitfield", None) and self.bitfield.any(True):
                    peer.writer.write(gen_bitfield_msg(self.bitfield))
                    await peer.writer.drain()

                self.peers.append(peer)
                asyncio.create_task(peer.listen_forever())

        except Exception as e:
            logger.error(f"Incoming connection failed: {e}")
            await peer.disconnect()

    def show_files(self):
        for f in self.files:  # type: ignore
            print(f)

    def _get_peers(self):
        peers_aggregated = set()
        for tracker in self.trackers:
            peers_aggregated |= set(tracker.peers)
        return peers_aggregated

    async def _contact_trackers(self, event="started"):
        self.torrent_info["event"] = event

        if not self.trackers:
            for tracker_addr in self.torrent_info["trackers"]:
                tracker = TrackerFactory(tracker_addr, self.torrent_info)
                if tracker:
                    self.trackers.append(tracker)

        tasks = [asyncio.create_task(t.get_peers()) for t in self.trackers]
        await asyncio.wait(tasks, timeout=20)
        self._update_tracker_intervals()

    def get_torrent_file(self, format="json", verbose=False):
        torrent_info = copy.deepcopy(self.torrent_info)
        torrent_info["info_hash"] = torrent_info["info_hash"].hex()
        piece_hashes = torrent_info.pop("piece_hashes")
        peer_list    = torrent_info.pop("peers")
        if verbose:
            torrent_info["piece_hashes"] = [h.hex() for h in piece_hashes]
            torrent_info["peers"]        = tuple(peer_list)
        return json.dumps(torrent_info)

    # ─────────────────────────────────────────────────────────────
    # Download
    # ─────────────────────────────────────────────────────────────

    async def download(self, file):
        active_peers = [p for p in self.peers if p.has_handshaked and p.active]
        self.downloader = FilesDownloadManager(self.torrent_info, active_peers)
        with PieceWriter(self.save_dir, file) as pw:
            async for piece in self.downloader.get_file(file):
                pw.write(piece)

    async def download_files(self, file_indices: list[int]):
        if not self.files:
            return
        valid = [i for i in file_indices if 0 <= i < len(self.files)]
        if not valid:
            return

        active_peers = [p for p in self.peers if p.has_handshaked and p.active]
        self.downloader = FilesDownloadManager(self.torrent_info, active_peers)

        for idx in valid:
            file = self.files[idx]
            logger.info(f"Torrent: Downloading [{idx}] '{file.name}'")
            with PieceWriter(self.save_dir, file) as pw:
                async for piece in self.downloader.get_file(file):
                    pw.write(piece)
            logger.info(f"Torrent: Completed [{idx}] '{file.name}'")

    # ─────────────────────────────────────────────────────────────
    # Background loops
    # ─────────────────────────────────────────────────────────────

    async def maintain_peers(self):
        while True:
            to_remove = [
                p for p in self.peers
                if (
                    getattr(p, "failed_attempts",   0) > PEER_MAX_FAILS
                    or getattr(p, "total_disconnects", 0) > PEER_MAX_DISCONNECTS
                    or getattr(p, "choke_count",       0) > PEER_MAX_CHOKES
                    or p.is_timed_out()
                )
            ]
            for p in to_remove:
                if p.active:
                    asyncio.create_task(p.disconnect("TTL/threshold exceeded"))

            self.peers = [p for p in self.peers if p not in to_remove]
            if to_remove:
                logger.warning(f"Torrent: Pruned {len(to_remove)} peers.")

            active = [p for p in self.peers if p.active and p.has_handshaked]
            if len(active) < self.TARGET_PEER_COUNT:
                existing  = {p.address for p in self.peers}
                new_addrs = [a for a in self._get_peers() if a not in existing]
                need      = self.TARGET_PEER_COUNT - len(active)
                for addr in new_addrs[:need]:
                    np = Peer(addr, self.torrent_info)
                    self.peers.append(np)
                    asyncio.create_task(self._init_new_peer(np))

            await asyncio.sleep(60)

    async def _init_new_peer(self, peer):
        await peer.connect()
        if peer.active:
            await peer.handshake()
            if peer.has_handshaked:
                await peer.interested()
                asyncio.create_task(peer.listen_forever())
                if self.downloader:
                    self.downloader.add_peer(peer)

    async def seed(self):
        """
        Seeding mode — vô hạn.

        Seeding thực sự được thực hiện bởi:
          • TCP server → handle_incoming_connection (peers kết nối vào)
          • maintain_peers → _init_new_peer → listen_forever (peers outgoing)
          • _tracker_announce_loop (keepalive với tracker)
          • _nat_renewal_loop (gia hạn UPnP/NAT-PMP mỗi 45 phút)
        Tất cả đã được start trong init(). seed() chỉ giữ coroutine sống.

        Bug cũ: gather listen_forever của peer ban đầu ở đây gây double-schedule
        vì init() không start listen_forever — nay đã loại bỏ.
        """
        logger.info(f"Torrent '{self.name}': seed mode active (local port {self.port})")
        nat = self.get_nat_status()
        if nat.get("active"):
            logger.info(
                f"  External reachability: {nat['external_ip']}:{nat['external_port']} "
                f"via {nat['method'].upper()}"
            )
        else:
            logger.warning(
                f"  No NAT mapping active. Seeder not reachable from outside "
                f"unless port {self.port} is manually forwarded."
            )

        if not getattr(self, "_loops_started", False):
            asyncio.create_task(self._tracker_announce_loop())
            asyncio.create_task(self.maintain_peers())
            asyncio.create_task(self._nat_renewal_loop())
            self._loops_started = True

        while True:
            await asyncio.sleep(60)

    async def _tracker_announce_loop(self):
        while True:
            sleep_time = self._next_sleep_seconds()
            self._next_announce_time = time.time() + sleep_time
            await asyncio.sleep(sleep_time)
            await self._contact_trackers(event="none")
            self._update_tracker_intervals()
