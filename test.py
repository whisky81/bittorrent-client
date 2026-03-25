from pytorrent.torrent import Torrent
import asyncio
from rich.progress import Progress, BarColumn, DownloadColumn, TransferSpeedColumn, TimeRemainingColumn, TextColumn
from rich.console import Console

console = Console()

async def run():
    torrent = Torrent("./torrent_files/BD1E86549A7A5043C43B804C8731C8FAB096AE6B.torrent")
    console.print(f"[bold green]🚀 Khởi tạo Torrent:[/bold green] [cyan]{torrent.name}[/cyan]")
    
    await torrent.init()
    
    file_to_download = torrent.files[1] # type: ignore # Thử tải tệp đầu tiên
    file_size = file_to_download.size
    
    console.print(f"[bold yellow]📦 Bắt đầu tải tệp:[/bold yellow] {file_to_download.name} ({file_size / 1024 / 1024:.2f} MB)")
    
    # Sử dụng Rich Progress Bar xịn xò 
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=40),
        "[progress.percentage]{task.percentage:>3.1f}%",
        "•",
        DownloadColumn(),
        "•",
        TransferSpeedColumn(),
        "•",
        TimeRemainingColumn(),
        console=console,
        transient=False,
    ) as progress:
        
        task_id = progress.add_task("[cyan]Downloading", total=file_size)
        
        # Chạy block tải trên async loop 
        download_task = asyncio.create_task(torrent.download(file_to_download))
        
        while not download_task.done():
            # Sử dụng đúng trỏ chuẩn class thay vì get_bytes_downloaded
            downloaded = file_to_download.get_bytes_written()
            progress.update(task_id, completed=downloaded)
            await asyncio.sleep(0.5)
        
        await download_task
        progress.update(task_id, completed=file_size)
    
    console.print(f"\n[bold green]✅ Tải xuống hoàn tất tệp:[/bold green] {file_to_download.name}!")

if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        console.print("[bold red]❌ Đã hủy tải xuống bởi người dùng.[/bold red]")