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