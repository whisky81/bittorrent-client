from bitstring import BitArray
import asyncio
from .core.pwp_message_generator import gen_handshake_msg, gen_no_payload_msg
from .core.pwp_response_parse import PeerResponseParser as Parse
from .core.pwp_response_handler import PeerResponseHandler as Handler 
from .core.constants import EMPTY_RESPONSE_THRESHOLD, INTERESTED
import logging

logger = logging.getLogger(__name__)
class Peer:
    def __init__(self, address, torrent_file, priority=10):
        self.address = address 
        self.torrent_file = torrent_file 
        self.active = False
        self.priority = priority
        
        self.total_disconnects = 0
        self.choking_me = True
        self.unchoke_event = asyncio.Event()
        if not self.choking_me:
            self.unchoke_event.set()
            
        self.am_interested = False
        self.choke_count = 0
        self.failed_attempts = 0
        
        self.has_handshaked = False
        self.has_bitfield = False
        num_pieces = len(torrent_file['piece_hashes'])
        self.pieces = BitArray(num_pieces)
        self.handshake_response: dict[str, None] | None = None 
    
    def __repr__(self) -> str:
        return f"Peer({self.address})"
    
    def __lt__(self, other):
        return self.priority < other.priority
    
    async def connect(self):
        ip, port = self.address 
        try:
            connection = asyncio.open_connection(host=ip, port=port)
            self.reader, self.writer = await asyncio.wait_for(connection, timeout=3)
            self.active = True 
            logger.debug(f"Peer {self.address}: Connection opened successfully.")
        except asyncio.TimeoutError:
            self.failed_attempts = getattr(self, 'failed_attempts', 0) + 1
            await self.disconnect("Timeout while connecting")
        except (ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError, OSError):
            self.failed_attempts = getattr(self, 'failed_attempts', 0) + 1
            await self.disconnect("Connection Refused/Reset/Aborted in CONNECT!")
            
    
    async def disconnect(self, message=""):
        self.active = False 
        self.total_disconnects += 1 
        if hasattr(self, 'writer'):
            try:
                await self.writer.drain()
            except Exception:
                pass
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
        logger.debug(f"Peer {self.address}: Connection closed. Reason: {message}.")
    
    
    async def send_message(self, message, timeout=3):
        if not self.active:
            if self.total_disconnects > 10:
                return 
            await self.connect()
            await self.handshake()
            await self.interested()
            
            if self.active:
                logger.debug(f"Peer {self.address}: Re-established inactive connection.") 
            else:
                logger.error(f"Peer {self.address}: Failed to re-establish connection.") 
                
        if not self.active:
            raise BrokenPipeError(f"Connection to {self} has been closed")
        
        response_buffer = bytes()
        self.writer.write(message)
        threshold = EMPTY_RESPONSE_THRESHOLD 
        try:
            while True:
                response = await asyncio.wait_for(self.reader.read(4096), timeout=timeout)
                response_buffer += response
                if len(response) <= 0:
                    threshold -= 1 
                if threshold == 0:
                    await self.disconnect("Empty Response Threshold Exceeded!")
                    break 
                
        except asyncio.TimeoutError:
            pass 
        except (ConnectionAbortedError, ConnectionRefusedError, ConnectionResetError):
            await self.disconnect("Connection Refused/Reset/Aborted in SEND!") 
        finally:
            return response_buffer 

    async def write_only(self, message):
        """Write a message without consuming any incoming response bytes.
        Used for seeding paths (UNCHOKE, PIECE) so REQUEST frames are not eaten."""
        if not self.active:
            return
        try:
            self.writer.write(message)
            await self.writer.drain()
        except Exception as e:
            logger.debug(f"Peer {self.address}: write_only error: {e}")
            await self.disconnect(f"Write error: {e}")
        
    async def handshake(self):
        if not self.active:
            return 
        from .core.pwp_message_generator import gen_bitfield_msg
        
        info_hash = self.torrent_file["info_hash"]
        peer_id = self.torrent_file["peer_id"]
        handshake_message = gen_handshake_msg(info_hash, peer_id)
        response = await self.send_message(handshake_message)
        artifacts = Parse(response).parse()
        await Handler(artifacts, self).handle()
        
        if self.has_handshaked:
            bitfield = self.torrent_file.get("bitfield", None)
            if bitfield and bitfield.any(True):
                bitfield_msg = gen_bitfield_msg(bitfield)
                await self.write_only(bitfield_msg)
    
    async def send_have(self, piece_index):
        if self.active and self.has_handshaked:
            from .core.pwp_message_generator import gen_have_msg
            msg = gen_have_msg(piece_index)
            await self.write_only(msg)
    
    async def interested(self):
        if not self.active or not self.has_handshaked:
            return 
        message = gen_no_payload_msg(INTERESTED)
        response = await self.send_message(message)
        artifacts = Parse(response).parse()
        await Handler(artifacts, self).handle()

    async def stream_pieces(self, n: int, timeout_per_piece: float = 10.0) -> bytes:
        """
        Read exactly `n` complete PWP PIECE messages from the socket using
        length-prefix framing instead of a timeout-based loop.

        Each PWP message: [4-byte big-endian length][payload bytes]
        PIECE message payload: [1 byte msg id=7][4 byte index][4 byte begin][data]

        This is ~10x faster than the timeout-based send_message() because:
        - No idle waiting after the last byte arrives
        - No 4KB fragmentation — reads declared payload size at once
        - All N blocks accumulate before returning
        """
        from struct import unpack as _unpack
        if not self.active or not hasattr(self, 'reader'):
            return b''

        result = bytearray()
        received = 0
        try:
            while received < n:
                # Read 4-byte length prefix
                len_bytes = await asyncio.wait_for(
                    self.reader.readexactly(4), timeout=timeout_per_piece
                )
                msg_len = _unpack(">I", len_bytes)[0]
                if msg_len == 0:
                    continue  # keep-alive
                # Read exactly msg_len bytes payload
                payload = await asyncio.wait_for(
                    self.reader.readexactly(msg_len), timeout=timeout_per_piece
                )
                result += len_bytes + payload
                # Count only PIECE messages (id=7)
                if payload[0:1] == b'\x07':
                    received += 1
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            pass  # Return what we have so far
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError) as e:
            logger.debug(f"{self} stream_pieces connection error: {e}")
            await self.disconnect(f"stream error: {e}")
        return bytes(result)

    async def listen_forever(self):
        if not self.active:
            return 
            
        while self.active:
            try:
                response = await asyncio.wait_for(self.reader.read(4096), timeout=30)
                if not response:
                    await self.disconnect("Remote peer closed connection")
                    break
                    
                artifacts = Parse(response).parse()
                await Handler(artifacts, self).handle()
            except asyncio.TimeoutError:
                pass  # No data in 30s is normal — keep loop alive
            except (ConnectionResetError, ConnectionAbortedError, ConnectionRefusedError, BrokenPipeError, IOError, OSError) as e:
                logger.debug(f"{self} Connection lost in listen loop: {e}")
                await self.disconnect(f"Connection lost: {e}")
                break
            except Exception as e:
                logger.debug(f"{self} Error in listening loop: {e}")
                await self.disconnect(f"Error {e}")
                break

    def update_piece_info(self, piece_num: int, has_piece: bool):
        self.pieces[piece_num] = has_piece