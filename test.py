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
FILE_INDEX = 1 # Download the .docx file as per user's script

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
    save_dir = Path.cwd() / "downloads" 

    console.print(Panel(
        f"[bold cyan]Pytorrent Client[/bold cyan]\n"
        f"🚀 Torrent: [green]{name}[/green]\n"
        f"📦 File: [yellow]{file.name}[/yellow] ({fmt_bytes(file.size)})",
        border_style="cyan"
    ))

    # We always need to init if we want to seed or download
    console.print("[bold blue]🔍 Initializing network...[/bold blue]")
    if downloadManager.is_already_downloaded(info_hash, name, [file.name]):
        console.print("[bold green]✅ This file is already downloaded. Skipping to SEED mode.[/bold green]")
        return
        # Still need to init to load the save_dir into torrent_info for seeding
        # await torrent.init(save_dir=save_dir)
    else:
        console.print("[bold blue]⬇  Starting download...[/bold blue]")
        await torrent.init(save_dir=save_dir)
        
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
        console.print("\n[bold green]🎉 Download complete![/bold green]")
        torrent.torrent_info["event"] = "completed"

    # ── Seeding Phase (10 Minutes) ────────────────────────────────────────────
    SEED_MINUTES = 10
    console.print(Panel(
        f"[bold green]🌱 Entering SEED mode for {SEED_MINUTES} minutes.[/bold green]\n"
        "Tracker will be updated and PWP listener is active.",
        border_style="green"
    ))

    # Starting seeding task (tracker announce loop + listener)
    seed_task = asyncio.create_task(torrent.seed())
    
    start_time = time.time()
    end_time = start_time + (SEED_MINUTES * 60)
    
    try:
        while time.time() < end_time:
            now = time.time()
            remaining = end_time - now
            
            # Statistics to print every minute
            uploaded = torrent.torrent_info.get("uploaded", 0)
            downloaded = torrent.torrent_info.get("downloaded", 0)
            active_peers = sum(1 for p in torrent.peers if getattr(p, "active", False))
            trackers_count = len(torrent.trackers)
            
            # Print stats
            from rich.table import Table
            table = Table(title=f"📊 Statistics at {time.strftime('%H:%M:%S')}", box=None)
            table.add_column("Property", style="cyan")
            table.add_column("Value", style="magenta")
            
            table.add_row("Uploaded", fmt_bytes(uploaded))
            table.add_row("Downloaded", fmt_bytes(downloaded))
            table.add_row("Active Peers", str(active_peers))
            table.add_row("Active Trackers", str(trackers_count))
            table.add_row("Time Remaining", f"{int(remaining // 60)}m {int(remaining % 60)}s")
            
            console.print(table)
            
            # Wait for 1 minute before next update
            await asyncio.sleep(60)
            
    except asyncio.CancelledError:
        pass
    finally:
        seed_task.cancel()
        console.print("[bold yellow]⏰ Seeding duration finished. Shutting down.[/bold yellow]")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[bold red]❌ Stopped by user.[/bold red]")
