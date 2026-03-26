import asyncio
import time
from pathlib import Path
from pytorrent.torrent import Torrent 
from pytorrent.download_manager import DownloadManager
from rich.progress import Progress, BarColumn, DownloadColumn, TransferSpeedColumn, TimeRemainingColumn, TextColumn
from rich.console import Console
from rich.panel import Panel

console = Console()
downloadManager = DownloadManager()

TORRENT_FILE = "./torrent_files/BD1E86549A7A5043C43B804C8731C8FAB096AE6B.torrent"
FILE_INDEX = 0 # Download the .docx file as per user's script

def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.2f} {unit}"
        n //= 1024
    return f"{n:.2f} TB"

async def main():
    torrent_file_path = Path(TORRENT_FILE).absolute()
    if not torrent_file_path.exists():
        console.print(f"[bold red]Error:[/bold red] Torrent file not found at {torrent_file_path}")
        return

    torrent = Torrent(str(torrent_file_path))
    name = torrent.torrent_info['name']
    info_hash = torrent.torrent_info['info_hash'].hex()
    
    # Check if index is valid
    if FILE_INDEX >= len(torrent.files): # type: ignore
        console.print(f"[bold red]Error:[/bold red] File index {FILE_INDEX} out of range (max {len(torrent.files)-1})") # type: ignore
        return
        
    file = torrent.files[FILE_INDEX] # type: ignore
    save_dir = Path.cwd() / name 

    console.print(Panel(
        f"[bold cyan]Pytorrent Client[/bold cyan]\n"
        f"🚀 Torrent: [green]{name}[/green]\n"
        f"📦 File: [yellow]{file.name}[/yellow] ({fmt_bytes(file.size)})",
        border_style="cyan"
    ))

    if downloadManager.is_already_downloaded(info_hash, name, [file.name]):
        console.print("[bold green]✅ This file is already downloaded and recorded in our database.[/bold green]")
    else:
        console.print("[bold blue]🔍 Initializing network and starting download...[/bold blue]")
        await torrent.init()
        
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
            task_id = progress.add_task(f"[cyan]⬇  {file.name}", total=file.size)
            download_task = asyncio.create_task(torrent.download(file))

            while not download_task.done():
                written = file.get_bytes_written()
                progress.update(task_id, completed=written)
                await asyncio.sleep(0.5)

            await download_task
            progress.update(task_id, completed=file.size)

        downloadManager.mark_downloaded(info_hash, save_dir, torrent_file_path)
        console.print(f"\n[bold green]🎉 Success![/bold green] Download of [white]{file.name}[/white] complete.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[bold red]❌ Stopped by user.[/bold red]")
