import asyncio
from collections import defaultdict

from .piece import Piece
from .core.constants import BLOCK_SIZE
from .core.file_utils import File, FileTree
import logging

logger = logging.getLogger(__name__)

END_GAME_THRESHOLD = 4

# BUG FIX: Số lần SHA-1 failure từ một peer trước khi bị penalize nặng
_HASH_FAIL_PENALIZE_AFTER = 2
_HASH_FAIL_PRIORITY_BUMP  = 20   # Tăng priority (= giảm ưu tiên) nhiều để tránh peer xấu


class FilesDownloadManager:
    def __init__(self, torrent_info: dict, active_peers: list):
        self.torrent_info = torrent_info
        piece_size = torrent_info["piece_length"]
        file_size  = torrent_info["size"]

        total_pieces, last_piece = divmod(file_size, piece_size)
        total_blocks, last_block = divmod(piece_size, BLOCK_SIZE)

        if last_piece:  total_pieces += 1
        if last_block:  total_blocks += 1
        total_pieces -= 1

        self.piece_info = {
            "total_pieces": total_pieces,
            "total_blocks": total_blocks,
            "last_piece":   last_piece,
            "last_block":   last_block,
            "piece_length": torrent_info["piece_length"],
        }

        self.piece_hashmap    = torrent_info["piece_hashes"]
        self.file_tree        = FileTree(torrent_info)
        self.completed_pieces: dict[int, bytes] = {}
        self.active_peers     = list(active_peers)

        # BUG FIX: Đếm hash failures per peer để penalize peer xấu
        self._peer_hash_fails: dict[int, int] = {}   # id(peer) → fail count

        self.file_pieces: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self.peer_queue:  asyncio.PriorityQueue = asyncio.PriorityQueue()
        for peer in active_peers:
            self.add_peer(peer)

    def add_peer(self, peer, priority=10):
        if peer not in self.active_peers:
            self.active_peers.append(peer)
        self.peer_queue.put_nowait((priority, peer))
        logger.debug(f"Downloader: Added peer {peer}")

    # ── Rarest-First ──────────────────────────────────────────────────────────

    def _rarity_order(self, piece_nums: list[int]) -> list[tuple[int, int]]:
        count: dict[int, int] = defaultdict(int)
        for peer in self.active_peers:
            bf = getattr(peer, "pieces", None)
            if bf is None:
                continue
            for pn in piece_nums:
                try:
                    if bf[pn]:
                        count[pn] += 1
                except IndexError:
                    pass
        result = []
        for pn in piece_nums:
            c = count.get(pn, 0)
            prio = c if c > 0 else 9999
            result.append((prio, pn))
        result.sort()
        return result

    def create_pieces_queue(self, file: File) -> None:
        piece_nums = [
            pn for pn in range(file.start_piece, file.end_piece + 1)
            if pn not in self.completed_pieces
        ]
        for prio, piece_num in self._rarity_order(piece_nums):
            self.file_pieces.put_nowait((prio, piece_num))

    def file_downloaded(self) -> bool:
        return self.file_pieces.empty()

    # ── End-Game Mode ─────────────────────────────────────────────────────────

    async def _end_game_download(self, remaining_pieces: list[int]) -> list[Piece]:
        logger.info(f"[End-Game] Activating for {len(remaining_pieces)} pieces")
        peers_snapshot: list = []
        while not self.peer_queue.empty():
            _, peer = self.peer_queue.get_nowait()
            if getattr(peer, "active", False):
                peers_snapshot.append(peer)

        if not peers_snapshot:
            for pn in remaining_pieces:
                self.file_pieces.put_nowait((1, pn))
            return []

        completed:   list[Piece] = []
        piece_tasks: dict[int, list[asyncio.Task]] = {}

        for pn in remaining_pieces:
            piece_tasks[pn] = []
            for peer in peers_snapshot:
                p = Piece(pn, 1, self.piece_info)
                tmp_q: asyncio.PriorityQueue = asyncio.PriorityQueue()
                tmp_q.put_nowait((0, peer))
                piece_tasks[pn].append(asyncio.create_task(p.download(tmp_q)))

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
                    continue
                if not Piece.is_valid(piece, self.piece_hashmap):
                    continue
                for sibling in piece_tasks.pop(pn, []):
                    if sibling != task and not sibling.done():
                        sibling.cancel()
                completed.append(piece)
                logger.info(f"[End-Game] Piece #{pn} completed")

        for pn in piece_tasks:
            self.file_pieces.put_nowait((1, pn))
        for i, peer in enumerate(peers_snapshot):
            self.peer_queue.put_nowait((10 - i, peer))

        return completed

    # ── Main download generator ───────────────────────────────────────────────

    async def get_file(self, file: File):
        self.create_pieces_queue(file)
        pending_tasks: set[asyncio.Task] = set()

        # Yield cached pieces
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

        while not self.file_downloaded():
            remaining = self.file_pieces.qsize()

            # End-Game
            if remaining <= END_GAME_THRESHOLD and not pending_tasks:
                rem_pieces: list[int] = []
                while not self.file_pieces.empty():
                    _, pn = self.file_pieces.get_nowait()
                    if pn not in self.completed_pieces:
                        rem_pieces.append(pn)
                if rem_pieces:
                    for piece in await self._end_game_download(rem_pieces):
                        self._finalize(piece)
                        piece_data = piece.data
                        if file.start_piece == piece.num:
                            piece_data = piece_data[file.start_byte:]
                        if file.end_piece == piece.num:
                            piece_data = piece_data[:file.end_byte]
                        piece.data = piece_data
                        file._set_bytes_written(file.get_bytes_written() + len(piece.data))
                        yield piece
                break

            prio_piece, num = await self.file_pieces.get()
            if num in self.completed_pieces:
                continue

            piece = Piece(num, prio_piece, self.piece_info)
            pending_tasks.add(asyncio.create_task(piece.download(self.peer_queue)))

        while pending_tasks:
            done, pending_tasks = await asyncio.wait(
                pending_tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                piece = await task

                if not Piece.is_valid(piece, self.piece_hashmap):
                    # BUG FIX: Penalize peer gây hash failure.
                    # Log cho thấy cùng piece (#712, #719...) fail nhiều lần liên tiếp
                    # vì peer xấu được tái sử dụng. Bây giờ ta tăng priority của peer đó
                    # (số lớn hơn = ít được chọn hơn) sau N lần failure.
                    bad_peer = getattr(piece, '_last_peer', None)
                    if bad_peer is not None:
                        pid = id(bad_peer)
                        self._peer_hash_fails[pid] = self._peer_hash_fails.get(pid, 0) + 1
                        fail_count = self._peer_hash_fails[pid]
                        if fail_count >= _HASH_FAIL_PENALIZE_AFTER:
                            logger.warning(
                                f"Downloader: {bad_peer} caused {fail_count} hash failures "
                                f"— penalizing (priority bump +{_HASH_FAIL_PRIORITY_BUMP})"
                            )
                            # Đưa peer vào queue với priority rất thấp
                            self.peer_queue.put_nowait((_HASH_FAIL_PRIORITY_BUMP, bad_peer))

                    new_piece = Piece(piece.num, 1, self.piece_info)
                    pending_tasks.add(
                        asyncio.create_task(new_piece.download(self.peer_queue))
                    )
                    continue

                self._finalize(piece)
                piece_data = piece.data
                if file.start_piece == piece.num:
                    piece_data = piece_data[file.start_byte:]
                if file.end_piece == piece.num:
                    piece_data = piece_data[:file.end_byte]
                piece.data = piece_data
                file._set_bytes_written(file.get_bytes_written() + len(piece.data))
                yield piece

        logger.info(f"File {file.name} downloaded completely.")

    def _finalize(self, piece: Piece) -> None:
        self.completed_pieces[piece.num] = piece.data
        self.torrent_info["downloaded"] = (
            self.torrent_info.get("downloaded", 0) + len(piece.data)
        )
        if "bitfield" in self.torrent_info:
            self.torrent_info["bitfield"][piece.num] = True
        if "broadcast_have" in self.torrent_info:
            asyncio.create_task(self.torrent_info["broadcast_have"](piece.num))
