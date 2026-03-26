import asyncio
# from pathlib import Path
from collections import defaultdict

from .piece import Piece
from .core.constants import BLOCK_SIZE
from .core.file_utils import File, FileTree
import logging

logger = logging.getLogger(__name__)

# Trigger end-game when fewer than this many pieces remain
END_GAME_THRESHOLD = 4


class FilesDownloadManager:
    def __init__(self, torrent_info: dict, active_peers: list):
        self.torrent_info = torrent_info
        piece_size = torrent_info["piece_length"]
        file_size = torrent_info["size"]
        # self.directory = torrent_info["name"]
        # Path(self.directory).mkdir(exist_ok=True)

        total_pieces, last_piece = divmod(file_size, piece_size)
        total_blocks, last_block = divmod(piece_size, BLOCK_SIZE)

        if last_piece:
            total_pieces += 1
        if last_block:
            total_blocks += 1

        total_pieces -= 1

        piece_info = {
            "total_pieces": total_pieces,
            "total_blocks": total_blocks,
            "last_piece": last_piece,
            "last_block": last_block,
            "piece_length": torrent_info["piece_length"]
        }

        self.piece_info = piece_info
        self.piece_hashmap = torrent_info["piece_hashes"]
        self.file_tree = FileTree(torrent_info)
        self.completed_pieces: dict[int, bytes] = {}

        self.active_peers = list(active_peers)
        self.file_pieces: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self.peer_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        for peer in active_peers:
            self.add_peer(peer)

    def add_peer(self, peer, priority=10):
        """Add a new active peer to the download pool."""
        if peer not in self.active_peers:
            self.active_peers.append(peer)
        self.peer_queue.put_nowait((priority, peer))
        logger.debug(f"Downloader: Added peer {peer} to queue.")

    # ──────────────────────────────────────────────────────────────────────────
    # Rarest-First piece ordering
    # ──────────────────────────────────────────────────────────────────────────
    def _rarity_order(self, piece_nums: list[int]) -> list[tuple[int, int]]:
        """Return (count, piece_num) tuples sorted so rarest comes first."""
        count: dict[int, int] = defaultdict(int)
        for peer in self.active_peers:
            pieces_bf = getattr(peer, "pieces", None)
            if pieces_bf is None:
                continue
            for pn in piece_nums:
                try:
                    if pieces_bf[pn]:
                        count[pn] += 1
                except IndexError:
                    pass

        # Pieces no peer has yet → put at end (count=0 would normally be highest
        # priority, but if 0 peers have it we can't download it — push them back)
        result = []
        for pn in piece_nums:
            c = count.get(pn, 0)
            # Use negative count so sort gives rarest (lowest count) first;
            # but if count=0 (nobody has it) deprioritise to bottom.
            priority = c if c > 0 else 9999
            result.append((priority, pn))

        result.sort()
        return result

    def create_pieces_queue(self, file: File) -> None:
        piece_nums = [
            pn for pn in range(file.start_piece, file.end_piece + 1)
            if pn not in self.completed_pieces
        ]
        ordered = self._rarity_order(piece_nums)
        for prio, piece_num in ordered:
            self.file_pieces.put_nowait((prio, piece_num))

    def file_downloaded(self) -> bool:
        return self.file_pieces.empty()

    # ──────────────────────────────────────────────────────────────────────────
    # End-Game Mode helpers
    # ──────────────────────────────────────────────────────────────────────────
    async def _end_game_download(self, remaining_pieces: list[int]) -> list[Piece]:
        """
        In end-game mode, request every remaining piece from ALL peers
        simultaneously. Cancel duplicate tasks as soon as one succeeds.
        """
        logger.info(f"[End-Game] Activating for {len(remaining_pieces)} remaining pieces")

        # Drain the peer queue into a flat list to re-use all peers
        peers_snapshot: list = []
        while not self.peer_queue.empty():
            _, peer = self.peer_queue.get_nowait()
            if getattr(peer, "active", False):
                peers_snapshot.append(peer)

        if not peers_snapshot:
            # Fall back: put pieces back and return empty
            for pn in remaining_pieces:
                self.file_pieces.put_nowait((1, pn))
            return []

        completed: list[Piece] = []
        piece_tasks: dict[int, list[asyncio.Task]] = {}

        # Launch one task per (piece, peer) combination
        for pn in remaining_pieces:
            piece_tasks[pn] = []
            for peer in peers_snapshot:
                p = Piece(pn, 1, self.piece_info)
                # Rebuild a temporary peer_queue with only this peer
                tmp_q: asyncio.PriorityQueue = asyncio.PriorityQueue()
                tmp_q.put_nowait((0, peer))
                task = asyncio.create_task(p.download(tmp_q))
                piece_tasks[pn].append(task)

        all_tasks = [t for tasks in piece_tasks.values() for t in tasks]

        while piece_tasks:
            if not all_tasks:
                break
            done, _ = await asyncio.wait(all_tasks, return_when=asyncio.FIRST_COMPLETED)

            for task in done:
                all_tasks = [t for t in all_tasks if t != task]
                try:
                    piece: Piece = await task
                except Exception:
                    continue

                pn = piece.num
                if pn not in piece_tasks:
                    continue  # already completed by another peer

                if not Piece.is_valid(piece, self.piece_hashmap):
                    continue  # invalid — others may still succeed

                # Cancel sibling tasks for the same piece
                for sibling in piece_tasks.pop(pn, []):
                    if sibling != task and not sibling.done():
                        sibling.cancel()

                completed.append(piece)
                logger.info(f"[End-Game] Piece #{pn} completed")

        # Put back incomplete pieces
        for pn in piece_tasks:
            self.file_pieces.put_nowait((1, pn))

        # Return peers to queue
        for i, peer in enumerate(peers_snapshot):
            self.peer_queue.put_nowait((10 - i, peer))

        return completed

    # ──────────────────────────────────────────────────────────────────────────
    # Main download generator
    # ──────────────────────────────────────────────────────────────────────────
    async def get_file(self, file: File):
        self.create_pieces_queue(file)
        pending_tasks: set[asyncio.Task] = set()

        # Yield cached pieces immediately
        for piece_num in range(file.start_piece, file.end_piece + 1):
            if piece_num in self.completed_pieces:
                piece = Piece(piece_num, 1, self.piece_info)
                piece_data = self.completed_pieces[piece_num]

                if file.start_piece == piece_num:
                    piece_data = piece_data[file.start_byte:]
                if file.end_piece == piece_num:
                    piece_data = piece_data[:file.end_byte]

                piece.data = piece_data
                file._set_bytes_written(file.get_bytes_written() + len(piece.data))
                yield piece

        # Normal download loop
        while not self.file_downloaded():
            remaining = self.file_pieces.qsize()

            # ── End-Game Mode ─────────────────────────────────────────────
            if remaining <= END_GAME_THRESHOLD and not pending_tasks:
                rem_pieces: list[int] = []
                while not self.file_pieces.empty():
                    _, pn = self.file_pieces.get_nowait()
                    if pn not in self.completed_pieces:
                        rem_pieces.append(pn)

                if rem_pieces:
                    end_game_results = await self._end_game_download(rem_pieces)
                    for piece in end_game_results:
                        self._finalize_and_yield_setup(piece)
                        piece_data = piece.data
                        if file.start_piece == piece.num:
                            piece_data = piece_data[file.start_byte:]
                        if file.end_piece == piece.num:
                            piece_data = piece_data[:file.end_byte]
                        piece.data = piece_data
                        file._set_bytes_written(file.get_bytes_written() + len(piece.data))
                        yield piece
                break

            # ── Normal Mode ────────────────────────────────────────────────
            prio_piece, num = await self.file_pieces.get()
            if num in self.completed_pieces:
                continue

            piece = Piece(num, prio_piece, self.piece_info)
            task = asyncio.create_task(piece.download(self.peer_queue))
            pending_tasks.add(task)

        # Collect remaining pending tasks (normal mode)
        while pending_tasks:
            done, pending_tasks = await asyncio.wait(pending_tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                piece = await task

                if not Piece.is_valid(piece, self.piece_hashmap):
                    prio_piece = 1
                    num = piece.num
                    new_piece = Piece(num, prio_piece, self.piece_info)
                    new_task = asyncio.create_task(new_piece.download(self.peer_queue))
                    pending_tasks.add(new_task)
                    continue

                self._finalize_and_yield_setup(piece)
                piece_data = piece.data
                if file.start_piece == piece.num:
                    piece_data = piece_data[file.start_byte:]
                if file.end_piece == piece.num:
                    piece_data = piece_data[:file.end_byte]
                piece.data = piece_data
                file._set_bytes_written(file.get_bytes_written() + len(piece.data))
                yield piece

        logger.info(f"File {file.name} downloaded completely.")

    def _finalize_and_yield_setup(self, piece: Piece) -> None:
        """Store piece in cache, update global stats, broadcast HAVE."""
        self.completed_pieces[piece.num] = piece.data
        self.torrent_info["downloaded"] = self.torrent_info.get("downloaded", 0) + len(piece.data)

        if "bitfield" in self.torrent_info:
            self.torrent_info["bitfield"][piece.num] = True
        if "broadcast_have" in self.torrent_info:
            asyncio.create_task(self.torrent_info["broadcast_have"](piece.num))
