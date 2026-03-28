import asyncio
import random
import hashlib

from .core.utils import Block
from .core.pwp_response_parse import PeerResponseParser as Parser
from .core.pwp_response_handler import PeerResponseHandler as Handler
from .core.constants import BLOCK_SIZE, BLOCKS_PER_CYCLE, MIN_BLOCKS_PER_CYCLE, MAX_BLOCKS_PER_CYCLE
from .core.pwp_message_generator import gen_request_msg
import logging

logger = logging.getLogger(__name__)


class Piece:
    def __init__(self, num: int, priority: int, piece_info: dict[str, int]):
        self.data = bytes()
        self.blocks = dict()
        self.num = num
        self.priority = priority

        self._is_last_piece = False
        self.total_blocks    = piece_info["total_blocks"]
        self.piece_size      = piece_info["piece_length"]
        self.piece_length    = piece_info["piece_length"]
        self.last_block_size = 0

        if self.num == piece_info["total_pieces"]:
            self._is_last_piece = True
            self.total_blocks, self.last_block_size = divmod(
                piece_info["last_piece"], BLOCK_SIZE
            )
            self.piece_size = piece_info["last_piece"]
            if self.last_block_size > 0:
                self.total_blocks += 1

        # BUG FIX: Lưu lại peer đã tải piece này để downloader có thể penalize
        # khi SHA-1 validation thất bại. Trước đây không có cơ chế này nên peer
        # xấu tiếp tục được sử dụng và gây SHA-1 failure lặp đi lặp lại.
        self._last_peer = None

    def __repr__(self):
        return f"Piece #{self.num}"

    async def fetch_blocks(self, block_offsets: list[int], peer) -> list[Block] | None:
        requests = bytes()

        for offset in block_offsets:
            block_num = offset // BLOCK_SIZE
            logger.debug(f"Piece #{self.num}: Requesting block #{block_num} from {peer}")
            request_message = gen_request_msg(self.num, offset)
            is_last_block = block_num == (self.total_blocks - 1)

            if self._is_last_piece and is_last_block and self.last_block_size > 0:
                request_message = gen_request_msg(self.num, offset, self.last_block_size)

            requests += request_message

        await peer.write_only(requests)
        response = await peer.stream_pieces(len(block_offsets))

        if not response:
            peer.update_piece_info(self.num, False)
            raise IOError(f"{peer} Sent Empty Blocks")

        try:
            artifacts = Parser(response).parse()
            blocks = await Handler(artifacts, peer=peer).handle()
            for block in blocks:  # type: ignore
                logger.debug(f"Piece #{self.num}: Received {block} from {peer}")
            return blocks  # type: ignore

        except TypeError as e:
            logger.warning(
                f"Piece #{self.num}: Fetching blocks from {peer} failed. Error: {e}"
            )
            self.adjust_blocks_per_cycle(-1)
            return None

    def is_piece_complete(self) -> bool:
        return all(b in self.blocks for b in range(self.total_blocks))

    def gen_offsets(self) -> set:
        return {
            b * BLOCK_SIZE
            for b in range(self.total_blocks)
            if b not in self.blocks
        }

    @staticmethod
    def is_valid(piece: "Piece", piece_hashmap) -> bool:
        piece_hash = hashlib.sha1(piece.data).digest()
        if piece_hash != piece_hashmap[piece.num]:
            logger.warning(f"Piece #{piece.num}: SHA-1 validation failed. Dropping.")
            return False
        return True

    def adjust_blocks_per_cycle(self, value: int = 1):
        global BLOCKS_PER_CYCLE
        BLOCKS_PER_CYCLE = max(
            MIN_BLOCKS_PER_CYCLE,
            min(MAX_BLOCKS_PER_CYCLE, BLOCKS_PER_CYCLE + value)
        )
        logger.debug(f"Piece #{self.num}: BLOCKS_PER_CYCLE → {BLOCKS_PER_CYCLE}")

    async def download(self, peers_man: asyncio.PriorityQueue) -> "Piece":
        priority, peer = await peers_man.get()

        while not self.is_piece_complete():
            if getattr(peer, 'choking_me', True):
                try:
                    await asyncio.wait_for(peer.unchoke_event.wait(), timeout=30.0)
                except asyncio.TimeoutError:
                    await peers_man.put((priority + 5, peer))
                    priority, peer = await peers_man.get()
                    continue
                if getattr(peer, 'choking_me', True):
                    continue

            if not getattr(peer, 'active', False):
                priority, peer = await peers_man.get()
                continue

            block_offsets = self.gen_offsets()
            if len(block_offsets) >= BLOCKS_PER_CYCLE:
                offsets = set(random.sample(sorted(block_offsets), BLOCKS_PER_CYCLE))
            else:
                offsets = block_offsets

            try:
                results = await asyncio.wait_for(
                    self.fetch_blocks(list(offsets), peer), timeout=15
                )
                if results:
                    self.adjust_blocks_per_cycle(1)
                    for block in results:
                        if block.data:
                            self.blocks[block.num] = block
                    # Ghi nhận peer đã cung cấp dữ liệu để downloader có thể penalize nếu cần
                    self._last_peer = peer
                else:
                    await peers_man.put((priority + 1, peer))
                    priority, peer = await peers_man.get()

            except (BrokenPipeError, IOError, asyncio.TimeoutError, Exception) as e:
                logger.debug(f"Piece #{self.num}: Error from {peer}: {e}")
                await peers_man.put((priority + 5, peer))
                priority, peer = await peers_man.get()
                continue

        for block_num in range(self.total_blocks):
            self.data += self.blocks[block_num].data

        await peers_man.put((priority - 1, peer))
        return self
