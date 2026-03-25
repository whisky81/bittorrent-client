from struct import unpack
from struct import error as UnpackError
from .constants import (
    CHOKE,
    UNCHOKE,
    HAVE,
    BITFIELD,
    PIECE,
    HANDSHAKE_LEN,
)
import logging

logger = logging.getLogger(__name__)


class PeerResponseParser:
    def __init__(self, response):
        self.response = response
        self.messages = {
            CHOKE: self.parse_choke,
            UNCHOKE: self.parse_unchoke,
            HAVE: self.parse_have,
            BITFIELD: self.parse_bitfield,
            PIECE: self.parse_piece,
            19: self.parse_handshake,
            None: self.parse_keep_alive,
        }
        self.artifacts = dict()

    def parse(self):
        while self.response:
            try:
                if self.response[0] == 19:
                    self.parse_handshake()
                    return self.artifacts 

                self.message_len = unpack(">I", self.response[:4])[0]
                self.message_id = (
                    unpack(">B", self.response[4:5])[0]
                    if self.message_len != 0
                    else None
                )

                if self.message_len == 0 and not self.message_id:
                    self.parse_keep_alive()

                if self.message_id not in self.messages:
                    logger.warning(f"PeerResponseParser: Unhandled message_id={self.message_id}, length={self.message_len}")
                    self.response = bytes()

                logger.debug(f"PeerResponseParser: Parsing message_id={self.message_id}, length={self.message_len}")
                self.messages[self.message_id]()

            except Exception as E:
                logger.error(f"PeerResponseParser: Exception while parsing response - {E}")
                self.response = bytes()

        return self.artifacts

    def parse_keep_alive(self):
        self.response = self.response[4:]
        self.artifacts.update({"keep_alive": True})

    def parse_choke(self):
        self.response = self.response[5:]
        self.artifacts.update({"choke": True})

    def parse_unchoke(self):
        self.response = self.response[5:]
        self.artifacts.update({"unchoke": True})

    def parse_have(self):
        message = self.response[:9]
        piece_index = unpack(">I", message[5:])[0]
        self.response = self.response[9:]
        self.artifacts.update({"have": {piece_index: True}})

    def parse_piece(self):

        if "pieces" not in self.artifacts:
            self.artifacts["pieces"] = list()
        block_len = self.message_len - 9
        total = block_len + 13
        try:
            index, offset = unpack(">II", self.response[5:13])
            data = self.response[13:total]
            block_info = (index, offset, data)
            self.artifacts["pieces"].append(block_info)
        except UnpackError:
            raise TypeError("Parser: Failed to extract piece")
        finally:
            self.response = self.response[total:]

    def parse_bitfield(self):
        # Since bitfield len is 1+X. X is length of bitfield
        self.message_len -= 1
        total = self.message_len + self.message_id
        message = self.response[5:total]
        self.response = self.response[total:]
        self.artifacts.update({"bitfield": message})

    def parse_handshake(self):
        message = self.response[:HANDSHAKE_LEN]
        self.response = self.response[HANDSHAKE_LEN:]
        self.artifacts.update({"handshake": message})
