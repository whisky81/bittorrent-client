import io
import hashlib
import asyncio
import copy
import json

from .core.file_utils import FileTree
from .core import bencode_wrapper
from .core.utils import PieceWriter, gen_secure_peer_id
import logging

logger = logging.getLogger(__name__)

from .peer import Peer
from .tracker_factory import TrackerFactory
from .downloader import FilesDownloadManager

class Torrent:
    def __init__(self, torrent_file) -> None:
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
        }

        self.files = FileTree(self.torrent_info)

    async def init(self):
        await self._contact_trackers()
        peer_addrs = self._get_peers()

        self.peers = [Peer(address, self.torrent_info) for address in peer_addrs]
        connections = [peer.connect() for peer in self.peers]
        await asyncio.gather(*connections)
        handshakes = [peer.handshake() for peer in self.peers]
        await asyncio.gather(*handshakes)
        interested_msgs = [peer.interested() for peer in self.peers]
        await asyncio.gather(*interested_msgs)

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

    async def _contact_trackers(self):
        tasks = []
        for tracker_addr in self.torrent_info["trackers"]:
            tracker = TrackerFactory(tracker_addr, self.torrent_info)
            self.trackers.append(tracker)
            tasks.append(
                asyncio.create_task(tracker.get_peers())  # type: ignore
            )
        await asyncio.gather(*tasks)

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

    async def download(self, file, strategy=0):
        active_peers = [peer for peer in self.peers if peer.has_handshaked]
        fd_man = FilesDownloadManager(self.torrent_info, active_peers)
        directory = self.torrent_info["name"]
        with PieceWriter(directory, file) as piece_writer:
            async for piece in fd_man.get_file(file):
                piece_writer.write(piece)
