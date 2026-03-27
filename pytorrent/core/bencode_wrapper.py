import fastbencode # type: ignore
from .constants import ENCODING

def to_py(data):
    '''
        raise UnicodeDecodeError
    '''
    if isinstance(data, bytes):
        try:
            return data.decode(ENCODING) 
        except UnicodeDecodeError:
            return data 
    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            new_k = k.decode(ENCODING) if isinstance(k, bytes) else k 
            result[new_k] = to_py(v) 
        return result 
    
    if isinstance(data, list):
        return [to_py(v) for v in data] 
    return data 

def to_bytes(data):
    """
        raise UnicodeEncodeError
    """
    if isinstance(data, str):
        return data.encode(ENCODING)
    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            new_k = k.encode(ENCODING) if isinstance(k, str) else k
            result[new_k] = to_bytes(v)
        return result 
    if isinstance(data, list):
        return [to_bytes(v) for v in data]
    return data 

def bencode(data):
    return fastbencode.bencode(to_bytes(data)) # type: ignore

def bdecode(data):
    return to_py(fastbencode.bdecode(data)) # type: ignore