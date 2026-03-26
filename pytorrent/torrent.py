import io
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
class Torrent:
    def __init__(self, torrent_file, save_dir: Path|str|None=None) -> None:
        """
        raise OSError
        """
        if isinstance(torrent_file, io.IOBase):
            bencoded_data = torrent_file.read()
        else:
            with open(torrent_file, "rb") as f:
                bencoded_data = f.read()

        metainfo = bencode_wrapper.bdecode(bencoded_data)
        announce = metainfo.get("announce", None)  # type: ignore
        announce_list = metainfo.get("announce-list", None)  # type: ignore
        self.name = metainfo["info"]["name"]  # type: ignore
        self.has_multiple_files = "files" in metainfo["info"]  # type: ignore
        pieces = metainfo["info"]["pieces"]  # type: ignore
        piece_length = metainfo["info"]["piece length"]  # type: ignore

        # TODO
        self.trackers = []
        self.peers = []
        self.files = None
        self.downloader = None
        self.TARGET_PEER_COUNT = 30

        files = metainfo["info"]["files"] if self.has_multiple_files else self.name  # type: ignore

        size = 0
        if self.has_multiple_files:
            size = sum([file["length"] for file in files])  # type: ignore
        else:
            size = metainfo["info"]["length"]  # type: ignore

        bencoded_info = bencode_wrapper.bencode(metainfo["info"])  # type: ignore
        info_hash = hashlib.sha1(bencoded_info).digest()

        piece_hashes = []
        if len(pieces) % 20 != 0:
            raise ValueError("corrupted pieces")
        num_pieces = len(pieces) // 20
        for i in range(num_pieces):
            piece = pieces[i * 20 : (i + 1) * 20]
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
        
        # Initialize save_dir
        if save_dir:
            self.save_dir = Path(save_dir)
        else:
            self.save_dir = Path.cwd() / "downloads"
        
        # Ensure base save_dir exists
        self.save_dir.mkdir(parents=True, exist_ok=True)

    async def broadcast_have(self, piece_index):
        if hasattr(self, 'peers'):
            have_tasks = []
            for peer in self.peers:
                if getattr(peer, 'has_handshaked', False) and getattr(peer, 'active', False):
                    have_tasks.append(peer.send_have(piece_index))
            if have_tasks:
                await asyncio.gather(*have_tasks)

    async def init(self, save_dir: Path|str|None=None):
        if save_dir:
            self.save_dir = Path(save_dir)
            self.save_dir.mkdir(parents=True, exist_ok=True)
        elif self.save_dir.name != self.torrent_info["name"]:
            # Default case: ensure we are in a subfolder named after the torrent
            self.save_dir = self.save_dir / self.torrent_info["name"]
            self.save_dir.mkdir(parents=True, exist_ok=True)
            
        # Store for PieceReader/PieceWriter usage via torrent_info
        self.torrent_info["save_dir"] = self.save_dir
        try:
            self.server = await asyncio.start_server(self.handle_incoming_connection, "0.0.0.0", PORT)
            logger.info(f"Torrent Initialization: TCP Server listening on 0.0.0.0:{PORT}")
        except Exception as e:
            logger.error(f"Failed to start TCP Server on {PORT}: {e}")

        await self._contact_trackers()
        peer_addrs = self._get_peers()

        self.peers = [Peer(address, self.torrent_info) for address in peer_addrs]
        connections = [peer.connect() for peer in self.peers]
        await asyncio.gather(*connections)
        handshakes = [peer.handshake() for peer in self.peers]
        await asyncio.gather(*handshakes)
        interested_msgs = [peer.interested() for peer in self.peers]
        await asyncio.gather(*interested_msgs)
        
        # Start maintenance loops (if not already running)
        if not getattr(self, "_loops_started", False):
            asyncio.create_task(self._tracker_announce_loop())
            asyncio.create_task(self.maintain_peers())
            self._loops_started = True

        self.torrent_info["peers"] = peer_addrs

        active_peers = 0
        active_trackers = 0
        for peer in self.peers:
            if peer.has_handshaked:
                active_peers += 1
        for tracker in self.trackers:
            if tracker.active:
                active_trackers += 1
        logger.info(f"Torrent Initialization: {active_trackers} trackers active, {active_peers} peers active.")

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
                reply = gen_handshake_msg(self.torrent_info["info_hash"], self.torrent_info["peer_id"])
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
        for file in self.files:  # type: ignore
            print(file)

    def _get_peers(self):
        peers_aggregated = set()
        for tracker in self.trackers:
            peer_list = set(tracker.peers)
            peers_aggregated |= peer_list
        logger.debug(f"Torrent: Aggregated {len(peers_aggregated)} unique peers across all trackers.")
        return peers_aggregated

    async def _contact_trackers(self, event="started"):
        self.torrent_info["event"] = event
        
        if not self.trackers:
            for tracker_addr in self.torrent_info["trackers"]:
                tracker = TrackerFactory(tracker_addr, self.torrent_info)
                self.trackers.append(tracker)
                
        tasks = []
        for tracker in self.trackers:
            tasks.append(
                asyncio.create_task(tracker.get_peers())  # type: ignore
            )
        done, pending = await asyncio.wait(tasks, timeout=20)
        logger.info(f"Tracker scrape finished. {len(done)} completed, {len(pending)} continuing in background.")

    def get_torrent_file(self, format="json", verbose=False):

        torrent_info = copy.deepcopy(self.torrent_info)
        torrent_info["info_hash"] = torrent_info["info_hash"].hex()

        piece_hashes = torrent_info.pop("piece_hashes")
        peer_list = torrent_info.pop("peers")
        if verbose:
            torrent_info["piece_hashes"] = [hash.hex() for hash in piece_hashes]
            torrent_info["peers"] = tuple(peer_list)
            pass

        return json.dumps(torrent_info)

    async def download(self, file):
        active_peers = [peer for peer in self.peers if peer.has_handshaked and peer.active]
        self.downloader = FilesDownloadManager(self.torrent_info, active_peers)
        with PieceWriter(self.save_dir, file) as piece_writer:
            async for piece in self.downloader.get_file(file):
                piece_writer.write(piece)

    async def maintain_peers(self):
        """Background task to ensure we have enough active peers and prune bad ones."""
        while True:
            # 1. Prune bad peers
            pruned_count = 0
            original_count = len(self.peers)
            
            # Thresholds for pruning
            self.peers = [
                p for p in self.peers if not (
                    getattr(p, 'failed_attempts', 0) > 5 or 
                    getattr(p, 'total_disconnects', 0) > 10 or
                    getattr(p, 'choke_count', 0) > 30
                )
            ]
            pruned_count = original_count - len(self.peers)
            if pruned_count > 0:
                logger.warning(f"Torrent: Pruned {pruned_count} bad peers from the pool.")

            # 2. Maintain active peer count
            active_peers = [p for p in self.peers if p.active and p.has_handshaked]
            if len(active_peers) < self.TARGET_PEER_COUNT:
                all_known_addrs = self._get_peers()
                existing_addrs = {p.address for p in self.peers}
                new_addrs = [addr for addr in all_known_addrs if addr not in existing_addrs]
                
                if new_addrs:
                    logger.debug(f"Torrent: Found {len(new_addrs)} new peer addresses. Trying to connect to some.")
                    # Try to connect to a batch of new peers
                    to_connect = new_addrs[:(self.TARGET_PEER_COUNT - len(active_peers))]
                    for addr in to_connect:
                        new_peer = Peer(addr, self.torrent_info)
                        self.peers.append(new_peer)
                        asyncio.create_task(self._init_new_peer(new_peer))
            
            await asyncio.sleep(60)

    async def _init_new_peer(self, peer):
        """Initialize a newly discovered peer."""
        await peer.connect()
        if peer.active:
            await peer.handshake()
            if peer.has_handshaked:
                await peer.interested()
                asyncio.create_task(peer.listen_forever())
                # If a download is active, add the peer to the downloader
                if self.downloader:
                    self.downloader.add_peer(peer)
                    logger.debug(f"Torrent: New peer {peer} added to active downloader.")

    async def seed(self):
        logger.info(f"Torrent {self.name}: Entering SEED mode...")
        # Start maintenance loops (if not already running)
        if not getattr(self, "_loops_started", False):
            asyncio.create_task(self._tracker_announce_loop())
            asyncio.create_task(self.maintain_peers())
            self._loops_started = True

        listen_tasks = []
        for peer in self.peers:
            if peer.active and peer.has_handshaked:
                listen_tasks.append(asyncio.create_task(peer.listen_forever()))

        if listen_tasks:
            # return_exceptions=True: one broken peer won't crash the entire gather
            results = await asyncio.gather(*listen_tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    logger.debug(f"Seed: peer task ended with exception: {r}")

        # Stay alive to accept new inbound connections via TCP server
        while True:
            await asyncio.sleep(60)

    async def _tracker_announce_loop(self):
        from .core.trackers import HTTPTracker
        while True:
            intervals = []
            for tracker in self.trackers:
                if getattr(tracker, "active", False) and getattr(tracker, "announce_response", None):
                    if isinstance(tracker, HTTPTracker):
                        interval = tracker.announce_response.get("interval", tracker.announce_response.get(b"interval", 1800))
                    else:
                        interval = tracker.announce_response.get("interval", 1800)
                    intervals.append(interval)

            sleep_time = min(intervals) if intervals else 1800
            
            logger.info(f"Torrent {self.name}: Sleeping {sleep_time}s before next tracker announce...")
            await asyncio.sleep(sleep_time)
            
            logger.info(f"Torrent {self.name}: Sending periodic tracker announce heartbeat...")
            await self._contact_trackers(event="none")
