from struct import unpack
from bitstring import BitArray
from pytorrent.core.utils import Block
from pytorrent.core.constants import HANDSHAKE_LEN, PROTOCOL_NAME

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
            if "handshake" in self.artifacts:
                await self.handle_handshake()
            if "have" in self.artifacts:
                self.handle_bitfield()
            if "bitfield" in self.artifacts:
                self.handle_bitfield()
            if "pieces" in self.artifacts:
                return self.handle_piece()

    def handle_keep_alive(self):
        print(f"Keep-Alive from {self.peer}")
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
        print(f"Unchoke from {self.peer}")
        self.artifacts.pop("unchoke")

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
        print(f"Handshake from {self.peer}")
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
            print(f"Bitfield from {self.peer}")

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
