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
from .peer import Peer
from .tracker_factory import TrackerFactory
from .downloader import FilesDownloadManager
from pathlib import Path
from .core.constants import PORT

logger = logging.getLogger(__name__)

PEER_MAX_FAILS = 5
PEER_MAX_DISCONNECTS = 10
PEER_MAX_CHOKES = 30


class Torrent:
    def __init__(self, torrent_file, save_dir: Path | str | None = None) -> None:
        """raise OSError"""
        if isinstance(torrent_file, io.IOBase):
            bencoded_data = torrent_file.read()
        else:
            with open(torrent_file, "rb") as f:
                bencoded_data = f.read()

        metainfo = bencode_wrapper.bdecode(bencoded_data)
        announce = metainfo.get("announce", None)
        announce_list = metainfo.get("announce-list", None)
        self.name = metainfo["info"]["name"]
        self.has_multiple_files = "files" in metainfo["info"]
        pieces = metainfo["info"]["pieces"]
        piece_length = metainfo["info"]["piece length"]

        self.trackers = []
        self.peers = []
        self.files = None
        self.downloader = None
        self.TARGET_PEER_COUNT = 30

        # ── Tracker interval state ─────────────────────────────────────
        # Persisted across re-announces so the loop never resets to default
        # on restart. Key = repr(tracker), value = interval in seconds.
        self._tracker_intervals: dict[str, int] = {}
        self._next_announce_time: float = 0.0

        files = metainfo["info"]["files"] if self.has_multiple_files else self.name

        size = 0
        if self.has_multiple_files:
            size = sum([f["length"] for f in files])
        else:
            size = metainfo["info"]["length"]

        bencoded_info = bencode_wrapper.bencode(metainfo["info"])
        info_hash = hashlib.sha1(bencoded_info).digest()

        piece_hashes = []
        if len(pieces) % 20 != 0:
            raise ValueError("corrupted pieces")
        num_pieces = len(pieces) // 20
        for i in range(num_pieces):
            piece = pieces[i * 20: (i + 1) * 20]
            piece_hashes.append(piece)

        trackers = []
        if announce:
            trackers.append(announce)
        if announce_list:
            for tier in announce_list:
                for tracker in tier:
                    if tracker not in trackers:
                        trackers.append(tracker)

        self.torrent_info = {
            "name": self.name,
            "size": size,
            "files": files,
            "piece_length": piece_length,
            "info_hash": info_hash,
            "piece_hashes": piece_hashes,
            "peers": [],
            "trackers": trackers,
            "peer_id": gen_secure_peer_id(),
            "downloaded": 0,
            "uploaded": 0,
        }

        self.files = FileTree(self.torrent_info)
        self.bitfield = BitArray(num_pieces)
        self.torrent_info["bitfield"] = self.bitfield
        self.torrent_info["broadcast_have"] = self.broadcast_have

        base_dir = Path(save_dir) if save_dir else Path.cwd() / "downloads"
        self.save_dir = base_dir / self.name
        self.save_dir.mkdir(parents=True, exist_ok=True)

    # ─────────────────────────────────────────────────────────────
    # Tracker interval helpers
    # ─────────────────────────────────────────────────────────────

    def _update_tracker_intervals(self):
        """Cache the announce interval from every active tracker response."""
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
        """Minimum announce interval across all active trackers (default 1800)."""
        return min(self._tracker_intervals.values()) if self._tracker_intervals else 1800

    # ─────────────────────────────────────────────────────────────
    # Init / connection
    # ─────────────────────────────────────────────────────────────

    async def broadcast_have(self, piece_index):
        if hasattr(self, 'peers'):
            tasks = [
                p.send_have(piece_index)
                for p in self.peers
                if getattr(p, 'has_handshaked', False) and getattr(p, 'active', False)
            ]
            if tasks:
                await asyncio.gather(*tasks)

    async def init(self, save_dir: Path | str | None = None):
        if save_dir:
            base = Path(save_dir)
            self.save_dir = base / self.name if base.name != self.name else base

        self.save_dir.mkdir(parents=True, exist_ok=True)
        self.torrent_info["save_dir"] = self.save_dir

        try:
            self.server = await asyncio.start_server(
                self.handle_incoming_connection, "0.0.0.0", PORT
            )
            logger.info(f"Torrent Initialization: TCP Server listening on 0.0.0.0:{PORT}")
        except Exception as e:
            logger.error(f"Failed to start TCP Server on {PORT}: {e}")

        await self._contact_trackers()
        peer_addrs = self._get_peers()

        self.peers = [Peer(addr, self.torrent_info) for addr in peer_addrs]
        await asyncio.gather(*[p.connect() for p in self.peers])
        await asyncio.gather(*[p.handshake() for p in self.peers])
        await asyncio.gather(*[p.interested() for p in self.peers])

        if not getattr(self, "_loops_started", False):
            asyncio.create_task(self._tracker_announce_loop())
            asyncio.create_task(self.maintain_peers())
            self._loops_started = True

        self.torrent_info["peers"] = peer_addrs

        active_peers = sum(1 for p in self.peers if p.has_handshaked)
        active_trackers = sum(1 for t in self.trackers if t.active)
        logger.info(
            f"Torrent Initialization: {active_trackers} trackers active, "
            f"{active_peers} peers active."
        )

    async def handle_incoming_connection(self, reader, writer):
        address = writer.get_extra_info('peername')
        logger.debug(f"Accepted incoming connection from {address}")
        peer = Peer(address, self.torrent_info)
        peer.reader = reader
        peer.writer = writer
        peer.active = True

        try:
            handshake_data = await asyncio.wait_for(peer.reader.read(68), timeout=5)
            if not handshake_data:
                await peer.disconnect("Empty handshake")
                return

            from .core.pwp_response_parse import PeerResponseParser as Parse
            from .core.pwp_response_handler import PeerResponseHandler as Handler

            artifacts = Parse(handshake_data).parse()
            await Handler(artifacts, peer).handle()

            if peer.has_handshaked:
                from .core.pwp_message_generator import gen_handshake_msg, gen_bitfield_msg
                reply = gen_handshake_msg(
                    self.torrent_info["info_hash"], self.torrent_info["peer_id"]
                )
                peer.writer.write(reply)
                await peer.writer.drain()

                if getattr(self, "bitfield", None) and self.bitfield.any(True):
                    b_msg = gen_bitfield_msg(self.bitfield)
                    peer.writer.write(b_msg)
                    await peer.writer.drain()

                self.peers.append(peer)
                asyncio.create_task(peer.listen_forever())

        except Exception as e:
            logger.error(f"Incoming connection failed: {e}")
            await peer.disconnect()

    def show_files(self):
        for f in self.files:
            print(f)

    def _get_peers(self):
        peers_aggregated = set()
        for tracker in self.trackers:
            peers_aggregated |= set(tracker.peers)
        logger.debug(f"Torrent: Aggregated {len(peers_aggregated)} unique peers.")
        return peers_aggregated

    async def _contact_trackers(self, event="started"):
        self.torrent_info["event"] = event

        if not self.trackers:
            for tracker_addr in self.torrent_info["trackers"]:
                tracker = TrackerFactory(tracker_addr, self.torrent_info)
                self.trackers.append(tracker)

        tasks = [asyncio.create_task(t.get_peers()) for t in self.trackers]
        done, pending = await asyncio.wait(tasks, timeout=20)

        # Persist intervals immediately after every scrape
        self._update_tracker_intervals()

        logger.info(
            f"Tracker scrape: {len(done)} done, {len(pending)} pending. "
            f"Intervals cached: {self._tracker_intervals}"
        )

    def get_torrent_file(self, format="json", verbose=False):
        torrent_info = copy.deepcopy(self.torrent_info)
        torrent_info["info_hash"] = torrent_info["info_hash"].hex()
        piece_hashes = torrent_info.pop("piece_hashes")
        peer_list = torrent_info.pop("peers")
        if verbose:
            torrent_info["piece_hashes"] = [h.hex() for h in piece_hashes]
            torrent_info["peers"] = tuple(peer_list)
        return json.dumps(torrent_info)

    # ─────────────────────────────────────────────────────────────
    # Download
    # ─────────────────────────────────────────────────────────────

    async def download(self, file):
        """Download a single File object (backwards-compat helper)."""
        active_peers = [p for p in self.peers if p.has_handshaked and p.active]
        self.downloader = FilesDownloadManager(self.torrent_info, active_peers)
        with PieceWriter(self.save_dir, file) as pw:
            async for piece in self.downloader.get_file(file):
                pw.write(piece)

    async def download_files(self, file_indices: list[int]):
        """
        Download multiple files by index using a single shared peer pool.
        Completed pieces are cached across files for efficiency.
        """
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
        """Prune stale/misbehaving peers (TTL + thresholds) and replenish pool."""
        while True:
            to_remove = [
                p for p in self.peers
                if (
                    getattr(p, 'failed_attempts', 0) > PEER_MAX_FAILS
                    or getattr(p, 'total_disconnects', 0) > PEER_MAX_DISCONNECTS
                    or getattr(p, 'choke_count', 0) > PEER_MAX_CHOKES
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
                existing = {p.address for p in self.peers}
                new_addrs = [a for a in self._get_peers() if a not in existing]
                need = self.TARGET_PEER_COUNT - len(active)
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
        logger.info(f"Torrent {self.name}: Entering SEED mode...")
        if not getattr(self, "_loops_started", False):
            asyncio.create_task(self._tracker_announce_loop())
            asyncio.create_task(self.maintain_peers())
            self._loops_started = True

        listen_tasks = [
            asyncio.create_task(p.listen_forever())
            for p in self.peers
            if p.active and p.has_handshaked
        ]
        if listen_tasks:
            results = await asyncio.gather(*listen_tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    logger.debug(f"Seed: peer task ended with: {r}")

        while True:
            await asyncio.sleep(60)

    async def _tracker_announce_loop(self):
        """
        Re-announce to trackers using the interval they advertised.
        The interval is cached in self._tracker_intervals so it survives
        across calls without reverting to the 1800s default.
        """
        while True:
            sleep_time = self._next_sleep_seconds()
            self._next_announce_time = time.time() + sleep_time

            logger.info(
                f"Torrent {self.name}: Next announce in {sleep_time}s "
                f"({time.strftime('%H:%M:%S', time.localtime(self._next_announce_time))})"
            )
            await asyncio.sleep(sleep_time)

            logger.info(f"Torrent {self.name}: Sending tracker heartbeat...")
            await self._contact_trackers(event="none")
            self._update_tracker_intervals()
