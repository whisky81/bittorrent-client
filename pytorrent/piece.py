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
        self.total_blocks = piece_info["total_blocks"]
        self.piece_size = piece_info["piece_length"]
        self.piece_length = piece_info["piece_length"]

        if self.num == piece_info["total_pieces"]:
            self._is_last_piece = True
            self.total_blocks, self.last_block_size = divmod(
                piece_info["last_piece"], BLOCK_SIZE
            )
            self.piece_size = piece_info["last_piece"]
            if self.last_block_size > 0:
                self.total_blocks += 1 

    def __repr__(self):
        return f"Piece #{self.num}"

    async def fetch_blocks(self, block_offsets: list[int], peer) -> list[Block] | None:
        requests = bytes()

        for offset in block_offsets:
            block_num = offset // BLOCK_SIZE
            logger.debug(f"Piece #{self.num}: Requesting block #{block_num} from {peer}")
            request_message = gen_request_msg(self.num, offset)
            is_last_block = True if block_num == (self.total_blocks - 1) else False

            if self._is_last_piece and is_last_block and self.last_block_size > 0:
                request_message = gen_request_msg(
                    self.num, offset, self.last_block_size
                )

            requests += request_message

        # Write all request messages at once (non-blocking, no response drain)
        await peer.write_only(requests)

        # Read exactly len(block_offsets) PIECE responses using length-prefix framing
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

        except TypeError as E:
            logger.warning(f"Piece #{self.num}: Fetching blocks from {peer} failed (Returned None). Error: {E}")
            self.adjust_blocks_per_cycle(-1)
            return None

    def is_piece_complete(self) -> bool:
        for block_num in range(self.total_blocks):
            if block_num not in self.blocks:
                return False
        return True

    def gen_offsets(self) -> set:
        blocks = set()
        for block_num in range(self.total_blocks):
            if block_num not in self.blocks:
                block_offset = block_num * BLOCK_SIZE
                blocks.add(block_offset)
        return blocks

    @staticmethod
    def is_valid(piece, piece_hashmap):
        piece_hash = hashlib.sha1(piece.data).digest()

        if piece_hash != piece_hashmap[piece.num]:
            logger.warning(f"Piece #{piece.num}: SHA-1 hash validation failed. Dropping piece.")
            return False

        return True

    def adjust_blocks_per_cycle(self, value: int = 1):
        global BLOCKS_PER_CYCLE
        BLOCKS_PER_CYCLE += value
        BLOCKS_PER_CYCLE = max(BLOCKS_PER_CYCLE, MIN_BLOCKS_PER_CYCLE)
        BLOCKS_PER_CYCLE = min(BLOCKS_PER_CYCLE, MAX_BLOCKS_PER_CYCLE)
        logger.debug(f"Piece #{self.num}: BLOCKS_PER_CYCLE adjusted to {BLOCKS_PER_CYCLE}")

    async def download(self, peers_man: asyncio.PriorityQueue) -> "Piece":
        priority, peer = await peers_man.get()

        while not self.is_piece_complete():
            # If peer is choking us, wait for unchoke event or rotate
            if getattr(peer, 'choking_me', True):
                # Suspend efficiently until peer unchokes us or we decide to rotate
                try:
                    # Wait up to 30s for an unchoke signal
                    await asyncio.wait_for(peer.unchoke_event.wait(), timeout=30.0)
                except asyncio.TimeoutError:
                    # If still choked after 30s, rotate to another peer
                    await peers_man.put((priority + 5, peer))
                    priority, peer = await peers_man.get()
                    continue
                
                # If we get here, the event was set (peer unchoked us)
                # Note: We don't sleep anymore, the wait() is exactly as long as needed
                if getattr(peer, 'choking_me', True):
                    # Edge case: event was set but choking_me is still True? 
                    # (Shouldn't happen with correct handler logic)
                    continue

            if not getattr(peer, 'active', False):
                # Handle dead peer: it's already disconnected, just get another one
                priority, peer = await peers_man.get()
                continue

            task_list = list()
            block_offsets = self.gen_offsets()

            if len(block_offsets) >= BLOCKS_PER_CYCLE:
                offsets = set(random.sample(sorted(block_offsets), BLOCKS_PER_CYCLE))
            else:
                offsets = self.gen_offsets()

            try:
                blocks_task = self.fetch_blocks(offsets, peer)  # type: ignore
                results = await asyncio.wait_for(blocks_task, timeout=15)
                
                if results:
                    self.adjust_blocks_per_cycle(1)
                    for block in results:
                        if block.data:
                            self.blocks.update({block.num: block})
                else:
                    # Peer sent nothing or was choked mid-stream
                    await peers_man.put((priority + 1, peer))
                    priority, peer = await peers_man.get()

            except (BrokenPipeError, IOError, asyncio.TimeoutError, Exception) as e:
                logger.debug(f"Piece #{self.num}: Error downloading from {peer}: {e}")
                current_priority, current_peer = priority, peer
                # Put back with high penalty if it's a connection error
                await peers_man.put((current_priority + 5, current_peer))
                priority, peer = await peers_man.get()
                continue

        for block_num in range(self.total_blocks):
            self.data += self.blocks[block_num].data

        await peers_man.put((priority - 1, peer))
        return self
