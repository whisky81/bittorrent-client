from pytorrent.torrent import Torrent
import asyncio
    
async def run():
    torrent = Torrent("./torrent_files/BD1E86549A7A5043C43B804C8731C8FAB096AE6B.torrent")
    print(torrent.torrent_info['piece_length'])
    await torrent.init()
    await torrent.download(torrent.files[1]) # type: ignore
    
asyncio.run(run())
