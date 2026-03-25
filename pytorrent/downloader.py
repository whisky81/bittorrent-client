import asyncio
from pathlib import Path

from .piece import Piece
from .core.constants import BLOCK_SIZE
from .core.file_utils import File, FileTree
import logging

logger = logging.getLogger(__name__)


class FilesDownloadManager:
    def __init__(self, torrent_info: dict, active_peers: list):
        piece_size = torrent_info["piece_length"]
        file_size = torrent_info["size"]
        self.directory = torrent_info["name"]
        Path(self.directory).mkdir(exist_ok=True)

        total_pieces, last_piece = divmod(file_size, piece_size)
        total_blocks, last_block = divmod(piece_size, BLOCK_SIZE) # piece_size ('piece length') % BLOCK_SIZE == 0 

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
            "piece_length": torrent_info['piece_length']
        }

        self.piece_info = piece_info
        self.piece_hashmap = torrent_info["piece_hashes"]
        self.file_tree = FileTree(torrent_info)
        self.completed_pieces: dict[int, bytes] = {}

        self.file_pieces = asyncio.PriorityQueue()
        peer_def = 10  
        self.peer_queue = asyncio.PriorityQueue()
        for peer in active_peers:
            self.peer_queue.put_nowait((peer_def, peer))

    def create_pieces_queue(self, file: File) -> None:
        piece_def = 3  
        for piece_num in range(file.start_piece, file.end_piece + 1):
            if piece_num not in self.completed_pieces:
                self.file_pieces.put_nowait((piece_def, piece_num))

    def file_downloaded(self) -> bool:
        return True if self.file_pieces.empty() else False

    async def get_file(self, file: File):
        self.create_pieces_queue(file)
        pending_tasks = set()

        # Yield cached pieces immediately
        for piece_num in range(file.start_piece, file.end_piece + 1):
            if piece_num in self.completed_pieces:
                piece = Piece(piece_num, 1, self.piece_info)
                piece_data = self.completed_pieces[piece_num]

                if file.start_piece == piece_num:
                    piece_data = piece_data[file.start_byte :]
                if file.end_piece == piece_num:
                    piece_data = piece_data[: file.end_byte]

                piece.data = piece_data
                file._set_bytes_written(file.get_bytes_written() + len(piece.data))
                yield piece

        while not self.file_downloaded():
            prio_piece, num = await self.file_pieces.get()
            piece = Piece(num, prio_piece, self.piece_info)
            task = asyncio.create_task(piece.download(self.peer_queue))
            pending_tasks.add(task)

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
                
                # Save to cache before slicing
                self.completed_pieces[piece.num] = piece.data
                
                piece_data = piece.data
                if file.start_piece == piece.num:
                    piece_data = piece_data[file.start_byte :]

                if file.end_piece == piece.num:
                    piece_data = piece_data[: file.end_byte]

                piece.data = piece_data
                file._set_bytes_written(file.get_bytes_written() + len(piece.data))
                yield piece

        logger.info(f"File {file.name} downloaded completely.")

