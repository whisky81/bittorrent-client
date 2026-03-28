from struct import unpack
from struct import error as UnpackError
from .constants import (
    CHOKE, UNCHOKE, HAVE, BITFIELD, PIECE,
    HANDSHAKE_LEN, INTERESTED, REQUEST,
)
import logging

logger = logging.getLogger(__name__)

# BUG FIX: Giới hạn message_len hợp lý.
# Nếu message_len > MAX (2MB), dữ liệu bị desync — drop toàn bộ buffer.
# Không giới hạn → parser thử skip [4 + 2GB] bytes, Python slice không lỗi
# nhưng response buffer không bao giờ cạn → vòng while bất tận + log rác.
_MAX_MSG_LEN = 2 * 1024 * 1024   # 2 MB


class PeerResponseParser:
    def __init__(self, response):
        self.response = response
        self.messages = {
            CHOKE:     self.parse_choke,
            UNCHOKE:   self.parse_unchoke,
            INTERESTED:self.parse_interested,
            REQUEST:   self.parse_request,
            HAVE:      self.parse_have,
            BITFIELD:  self.parse_bitfield,
            PIECE:     self.parse_piece,
            19:        self.parse_handshake,
            None:      self.parse_keep_alive,
        }
        self.artifacts = dict()

    def parse(self):
        while self.response:
            try:
                if self.response[0] == 19:
                    self.parse_handshake()
                    return self.artifacts

                self.message_len = unpack(">I", self.response[:4])[0]

                # BUG FIX: Sanity check trên message_len.
                # Các message_id hợp lệ lớn nhất là PIECE (7), payload tối đa ~1MB.
                # Nếu message_len > 2MB thì stream đã bị desync (thường do partial read
                # ghép với garbage data). Drop toàn bộ buffer để tránh log nhiễu.
                if self.message_len > _MAX_MSG_LEN:
                    logger.warning(
                        f"PeerResponseParser: implausibly large message_len="
                        f"{self.message_len} — stream desynced, dropping buffer"
                    )
                    self.response = bytes()
                    break

                self.message_id = (
                    unpack(">B", self.response[4:5])[0]
                    if self.message_len != 0
                    else None
                )

                if self.message_len == 0 and not self.message_id:
                    self.parse_keep_alive()
                    continue

                if self.message_id not in self.messages:
                    logger.debug(
                        f"PeerResponseParser: unknown message_id={self.message_id}, "
                        f"len={self.message_len} — skipping"
                    )
                    self.response = self.response[4 + self.message_len:]
                    continue

                logger.debug(
                    f"PeerResponseParser: parsing message_id={self.message_id}, "
                    f"len={self.message_len}"
                )
                self.messages[self.message_id]()

            except Exception as e:
                logger.error(f"PeerResponseParser: exception — {e}")
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

    def parse_interested(self):
        self.response = self.response[5:]
        self.artifacts.update({"interested": True})

    def parse_request(self):
        if "requests" not in self.artifacts:
            self.artifacts["requests"] = list()
        try:
            index, begin, length = unpack(">III", self.response[5:17])
            self.artifacts["requests"].append((index, begin, length))
        except UnpackError:
            raise TypeError("Parser: Failed to extract request")
        finally:
            self.response = self.response[17:]

    def parse_have(self):
        message = self.response[:9]
        piece_index = unpack(">I", message[5:])[0]
        self.response = self.response[9:]
        if "have" not in self.artifacts:
            self.artifacts["have"] = {}
        self.artifacts["have"][piece_index] = True

    def parse_piece(self):
        if "pieces" not in self.artifacts:
            self.artifacts["pieces"] = list()
        block_len = self.message_len - 9
        total = block_len + 13
        try:
            index, offset = unpack(">II", self.response[5:13])
            data = self.response[13:total]
            self.artifacts["pieces"].append((index, offset, data))
        except UnpackError:
            raise TypeError("Parser: Failed to extract piece")
        finally:
            self.response = self.response[total:]

    def parse_bitfield(self):
        self.message_len -= 1
        total = self.message_len + self.message_id
        message = self.response[5:total]
        self.response = self.response[total:]
        self.artifacts.update({"bitfield": message})

    def parse_handshake(self):
        message = self.response[:HANDSHAKE_LEN]
        self.response = self.response[HANDSHAKE_LEN:]
        self.artifacts.update({"handshake": message})
