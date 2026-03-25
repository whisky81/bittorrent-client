from pytorrent.torrent import Torrent
import asyncio
from rich.progress import Progress, BarColumn, DownloadColumn, TransferSpeedColumn, TimeRemainingColumn

async def run():
    torrent = Torrent("./torrent_files/BD1E86549A7A5043C43B804C8731C8FAB096AE6B.torrent")
    await torrent.init()
    
    file_to_download = torrent.files[1] # type: ignore
    file_size = file_to_download.size
    
    # Cấu hình progress bar với nhiều cột thông tin
    with Progress(
        "[progress.description]{task.description}",
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
    ) as progress:
        
        task = progress.add_task(f"[cyan]Đang tải {file_to_download.name}...", total=file_size)
        download_task = asyncio.create_task(torrent.download(file_to_download))
        
        while not download_task.done():
            downloaded = file_to_download.get_bytes_downloaded()
            progress.update(task, completed=downloaded)
            await asyncio.sleep(0.5)
        
        await download_task
        progress.update(task, completed=file_size)
    
    print("\nTải xuống hoàn tất!")

if __name__ == "__main__":
    asyncio.run(run())