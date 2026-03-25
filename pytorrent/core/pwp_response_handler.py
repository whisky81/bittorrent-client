from struct import unpack
from bitstring import BitArray
from .utils import Block
from .constants import HANDSHAKE_LEN, PROTOCOL_NAME
import logging

logger = logging.getLogger(__name__)

class PeerResponseHandler:
    def __init__(self, artifacts: dict, peer = None):
        self.artifacts = artifacts
        self.peer = peer

    async def handle(self):
        while self.artifacts:
            if "keep_alive" in self.artifacts:
                self.handle_keep_alive()
            if "choke" in self.artifacts:
                await self.handle_choke()
            if "unchoke" in self.artifacts:
                self.handle_unchoke()
            if "interested" in self.artifacts:
                await self.handle_interested()
            if "handshake" in self.artifacts:
                await self.handle_handshake()
            if "have" in self.artifacts:
                self.handle_bitfield()
            if "bitfield" in self.artifacts:
                self.handle_bitfield()
            if "requests" in self.artifacts:
                await self.handle_request()
            if "pieces" in self.artifacts:
                return self.handle_piece()

    def handle_keep_alive(self):
        logger.debug(f"PeerResponseHandler: Received Keep-Alive from {self.peer}")
        self.artifacts.pop("keep_alive")

    async def handle_choke(self):
        if not self.peer:
            return 
        self.peer.choking_me = True
        # await self.peer.disconnect("Choked client!")
        self.artifacts.pop("choke")

    def handle_unchoke(self):
        if not self.peer:
            return 
        self.peer.choking_me = False
        self.peer.am_interested = True
        logger.debug(f"PeerResponseHandler: Received Unchoke from {self.peer}")
        self.artifacts.pop("unchoke")

    async def handle_interested(self):
        if not self.peer:
            return
        from .pwp_message_generator import gen_no_payload_msg
        from .constants import UNCHOKE
        logger.debug(f"PeerResponseHandler: Received Interested from {self.peer}")
        self.peer.choking_me = False 
        unchoke_msg = gen_no_payload_msg(UNCHOKE)
        await self.peer.send_message(unchoke_msg)
        self.artifacts.pop("interested")

    async def handle_request(self):
        if not self.peer:
            return
        from .utils import PieceReader
        from .pwp_message_generator import gen_piece_msg
        
        for req in self.artifacts.get("requests", []):
            index, begin, length = req
            logger.debug(f"PeerResponseHandler: {self.peer} requested piece {index} offset {begin} len {length}")
            
            bitfield = self.peer.torrent_file.get("bitfield")
            if bitfield and bitfield[index]:
                directory = self.peer.torrent_file["name"]
                files = self.peer.files # if Peer has access, wait Torrent info doesn't have FileTree directly in dict.
                # Actually FileTree is in downloader, or Torrent.files.
                
                block_data = PieceReader.read(self.peer.torrent_file, index, begin, length)
                if block_data:
                    piece_msg = gen_piece_msg(index, begin, block_data)
                    await self.peer.send_message(piece_msg)
                    if "uploaded" in self.peer.torrent_file:
                        self.peer.torrent_file["uploaded"] += len(block_data)
        
        self.artifacts.pop("requests", None)

    async def handle_handshake(self):
        if not self.peer:
            return 
        message = self.artifacts["handshake"]
        if not message or len(message) < HANDSHAKE_LEN:
            await self.peer.disconnect("Empty/None/Wrong handshake message! ")
            return
        pstrlen, pstr, res, info_hash, peer_id = unpack(">B19sQ20s20s", message)

        if pstrlen != 19 or pstr != PROTOCOL_NAME:
            await self.peer.disconnect("Invalid pstrlen or pstr! ")
            return 

        handshake_response = {
            "pstrlen": pstrlen,
            "pstr": pstr,
            "reserved": res,
            "info_hash": info_hash,
            "peer_id": peer_id,
        }

        self.peer.has_handshaked = True
        self.peer.handshake_response = handshake_response
        logger.debug(f"PeerResponseHandler: Received Handshake from {self.peer}")
        self.artifacts.pop("handshake")

    def handle_bitfield(self):
        if not self.peer:
            return 
        if "bitfield" in self.artifacts:
            message = self.artifacts["bitfield"]
            pieces = BitArray(message)
            self.peer.has_bitfield = True 
        else:
            num_pieces = len(self.peer.torrent_info["piece_hashes"]) # type: ignore
            pieces = BitArray(num_pieces)

        if "have" in self.artifacts:
            for piece_num in self.artifacts["have"]:
                pieces[piece_num] = True

        self.peer.pieces = pieces
        try:
            if "have" in self.artifacts:
                self.artifacts.pop("have")
            if "bitfield" in self.artifacts:
                self.artifacts.pop("bitfield")
        except KeyError:
            ...
        finally:
            logger.debug(f"PeerResponseHandler: Received Bitfield from {self.peer}")

    def handle_piece(self):
        blocks = list()
        for block_info in self.artifacts["pieces"]:
            try:
                index, offset, data = block_info
                block = Block(index, offset, data)
                blocks.append(block)
            except TypeError:
                raise TypeError(f"Handler: Failed To Extract Piece sent by {self.peer}")

        self.artifacts.pop("pieces")
        return blocks
