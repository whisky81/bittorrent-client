from struct import pack 
from .constants import (
    PROTOCOL_NAME,
    CHOKE,
    UNCHOKE,
    INTERESTED,
    NOT_INTERESTED,
    REQUEST,
    BLOCK_SIZE
)

def gen_handshake_msg(info_hash, peer_id):
    return pack(
        ">B19sQ20s20s",
        19,
        PROTOCOL_NAME,
        0,
        info_hash,
        peer_id
    )    

def gen_no_payload_msg(id):
    if id not in [CHOKE, UNCHOKE, INTERESTED, NOT_INTERESTED]:
        raise ValueError("Unknown, no payload peer message")
    return pack(">IB", 1, id)

def gen_request_msg(index, offset, length=BLOCK_SIZE):
    return pack(
        ">IBIII",
        13,
        REQUEST,
        index,
        offset,
        length 
    )
