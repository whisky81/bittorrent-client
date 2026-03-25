from .constants import BLOCK_SIZE, PEER_ID_PREFIX
import logging
from pathlib import Path

logger = logging.getLogger(__name__)
import secrets

class Block:
	def __init__(self, piece_num=-1, offset=-1, data=bytes()):
		self.piece_num = piece_num 
		self.offset = offset
		self.data = data
		self.num = offset // BLOCK_SIZE 

	def __repr__(self):
		return (f"Block #{self.piece_num}-{self.num}")

class PieceWriter:
    def __init__(self, directory_name, file):
        self.file = file 
        self.directory = directory_name 
        Path(self.directory).mkdir(exist_ok=True)
    
    def __enter__(self):
        filepath = f"{self.directory}/{self.file.name}"
        self.target_file = open(filepath, "wb")
        return self 
    
    def write(self, piece):
        piece_index = piece.num - self.file.start_piece
        offset = (piece_index * piece.piece_length) - self.file.start_byte
        if offset < 0:
            offset = 0
        
        self.target_file.seek(offset)
        self.target_file.write(piece.data)
        logger.debug(f"PieceWriter: Successfully wrote {piece} data to file '{self.file.name}'")
    
    def __exit__(self, exc_type, exc_val, exc_traceback):
        self.target_file.close()

def gen_secure_peer_id():
    peer_id = PEER_ID_PREFIX + secrets.token_bytes(12)
    return peer_id

class PieceReader:
    @staticmethod
    def read(torrent_info: dict, index: int, begin: int, length: int) -> bytes:
        from .file_utils import FileTree
        from pathlib import Path
        
        piece_length = torrent_info["piece_length"]
        directory = torrent_info["name"]
        files = FileTree(torrent_info)
        
        abs_offset = index * piece_length + begin
        result = bytearray()
        bytes_to_read = length
        
        current_offset = 0
        for file in files:
            if current_offset + file.size <= abs_offset:
                current_offset += file.size
                continue 
                
            file_offset = abs_offset - current_offset
            read_len = min(bytes_to_read, file.size - file_offset)
            
            filepath = Path(directory) / file.name
            if filepath.exists():
                with open(filepath, "rb") as f:
                    f.seek(file_offset)
                    result.extend(f.read(read_len))
            else:
                return bytes()
                
            bytes_to_read -= read_len
            current_offset += file.size 
            abs_offset += read_len 
            
            if bytes_to_read <= 0:
                break 
                
        return bytes(result)