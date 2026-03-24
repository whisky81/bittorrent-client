from pathlib import Path
from pytorrent.core.constants import BLOCK_SIZE

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
        # # # # # # # # # # # # # # # # # # # # # # #
        #											#
        #											#
        # piece.piece_size = piece_length?? 		#
        #											#
        #											#
        # # # # # # # # # # # # # # # # # # # # # # #
        offset = (piece_index * piece.piece_size) - self.file.start_byte
        if offset < 0:
            offset = 0
        
        self.target_file.seek(offset)
        self.target_file.write(piece.data)
        print(f"Wrote {piece} to {self.file.name}")
    
    def __exit__(self, exc_type, exc_val, exc_traceback):
        self.target_file.close()
