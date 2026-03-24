from bitstring import BitArray
import asyncio
from pytorrent.core.pwp_message_generator import gen_handshake_msg, gen_no_payload_msg
from pytorrent.core.pwp_response_parse import PeerResponseParser as Parse
from pytorrent.core.pwp_response_handler import PeerResponseHandler as Handler 
from pytorrent.core.constants import EMPTY_RESPONSE_THRESHOLD, INTERESTED
class Peer:
    def __init__(self, address, torrent_file, priority=10):
        self.address = address 
        self.torrent_file = torrent_file 
        self.active = False
        self.priority = priority
        
        self.total_disconnects = 0
        self.choking_me = True
        self.am_interested = False
        
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
            print(f"{self}\t\t\topened connection")
        except asyncio.TimeoutError:
            await self.disconnect("Timeout while connecting")
        except (ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError, OSError):
            await self.disconnect("Connection Refused/Reset/Aborted in CONNECT!")
            
    
    async def disconnect(self, message=""):
        self.active = False 
        self.total_disconnects += 1 
        if hasattr(self, 'writer'):
            await self.writer.drain()
            self.writer.close()
            await self.writer.wait_closed()
        print(f"{self}\t\t\t{message}\t\t\tclose connection") 
    
    
    async def send_message(self, message, timeout=3):
        if not self.active:
            if self.total_disconnects > 10:
                return 
            await self.connect()
            await self.handshake()
            await self.interested()
            
            if self.active:
                print(f"Tried sending message to inactive {self}. Successfully re-established connection!") 
            else:
                print(f"Tried sending message to inactive {self}. Failed to re-establish connection!") 
                
            # raise BrokenPipeError("Tried sending message to inactive peer")
        if not self.active:
            raise BrokenPipeError(f"Connection to {self} has been closed")
        
        response_buffer = bytes()
        self.writer.write(message)
        threshold = EMPTY_RESPONSE_THRESHOLD 
        try:
            while True:
                response = await asyncio.wait_for(self.reader.read(1024), timeout=timeout)
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
        
    async def handshake(self):
        if not self.active:
            return 
        info_hash = self.torrent_file['info_hash']
        handshake_message = gen_handshake_msg(info_hash)
        response = await self.send_message(handshake_message)
        artifacts = Parse(response).parse()
        # print(f"handshake response:\t\t\t{artifacts.get('handshake', b'')}")
        await Handler(artifacts, self).handle()
        # await Handler(artifacts, Peer=self).handle()
    
    async def interested(self):
        if not self.active or not self.has_handshaked:
            return 
        message = gen_no_payload_msg(INTERESTED)
        response = await self.send_message(message)
        artifacts = Parse(response).parse()
        await Handler(artifacts, self).handle()

    def update_piece_info(self, piece_num: int, has_piece: bool):
        self.pieces[piece_num] = has_piece