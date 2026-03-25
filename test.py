from pytorrent.torrent import Torrent
import asyncio
import time
from rich.progress import Progress, BarColumn, DownloadColumn, TransferSpeedColumn, TimeRemainingColumn, TextColumn
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

console = Console()

TORRENT_FILE = "./torrent_files/BD1E86549A7A5043C43B804C8731C8FAB096AE6B.torrent"
FILE_INDEX = 0

# ─────────────────────────────────────────
# Utility
# ─────────────────────────────────────────
def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.2f} {unit}"
        n //= 1024
    return f"{n:.2f} TB"

def build_stats_table(torrent: Torrent, file, elapsed: float) -> Table:
    t = Table(box=box.ROUNDED, show_header=False, expand=True, border_style="bright_black")
    t.add_column("Key", style="bold cyan", width=22)
    t.add_column("Value", style="white")

    downloaded: int = torrent.torrent_info.get("downloaded", 0)
    uploaded: int = torrent.torrent_info.get("uploaded", 0)
    total_size: int = torrent.torrent_info.get("size", 1)
    left = max(0, total_size - downloaded)

    pieces_total = len(torrent.torrent_info.get("piece_hashes", []))
    pieces_have = torrent.bitfield.count(True) if hasattr(torrent, "bitfield") else 0

    active_peers = sum(1 for p in torrent.peers if getattr(p, "active", False))

    t.add_row("📁 File", file.name)
    t.add_row("📏 Size", fmt_bytes(file.size))
    t.add_row("⬇  Downloaded", fmt_bytes(downloaded))
    t.add_row("⬆  Uploaded", fmt_bytes(uploaded))
    t.add_row("📦 Remaining", fmt_bytes(left))
    t.add_row("🧩 Pieces", f"{pieces_have} / {pieces_total}")
    t.add_row("🔗 Active Peers", str(active_peers))
    t.add_row("⏱  Elapsed", f"{elapsed:.0f}s")
    return t


# ─────────────────────────────────────────
# Main
# ─────────────────────────────────────────
async def run():
    console.print(Panel(
        "[bold cyan]Pytorrent Client[/bold cyan] — Download + Seed Mode",
        border_style="cyan"
    ))

    torrent = Torrent(TORRENT_FILE)
    console.print(f"[bold green]🚀 Torrent:[/bold green] [cyan]{torrent.name}[/cyan]")

    await torrent.init()

    file_to_download = torrent.files[FILE_INDEX]  # type: ignore
    file_size = file_to_download.size

    console.print(
        f"[bold yellow]📦 Downloading:[/bold yellow] {file_to_download.name} "
        f"({fmt_bytes(file_size)})"
    )

    # ── Download Phase ───────────────────────────────────────────────────
    start_time = time.monotonic()

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
        task_id = progress.add_task("[cyan]⬇  Downloading", total=file_size)
        download_task = asyncio.create_task(torrent.download(file_to_download))

        while not download_task.done():
            written = file_to_download.get_bytes_written()
            progress.update(task_id, completed=written)
            await asyncio.sleep(0.5)

        await download_task
        progress.update(task_id, completed=file_size)

    elapsed_download = time.monotonic() - start_time
    console.print(
        f"\n[bold green]✅ Download complete:[/bold green] {file_to_download.name} "
        f"in [white]{elapsed_download:.1f}s[/white]"
    )

    # Mark torrent completed so trackers advertise as seeder
    torrent.torrent_info["event"] = "completed"

    # ── Seed Phase ───────────────────────────────────────────────────────
    console.print(Panel(
        "[bold green]🌱 Entering SEED mode[/bold green] — listening on port [cyan]6887[/cyan]\n"
        "Press [bold red]Ctrl+C[/bold red] to stop.",
        border_style="green"
    ))

    seed_start = time.monotonic()

    # Print live stats table every 5 seconds while seeding
    seed_task = asyncio.create_task(torrent.seed())

    try:
        while not seed_task.done():
            elapsed_seed = time.monotonic() - seed_start
            stats = build_stats_table(torrent, file_to_download, elapsed_download + elapsed_seed)
            console.print(Panel(stats, title="📊 Statistics", border_style="bright_blue", expand=False))
            await asyncio.sleep(5)
    except asyncio.CancelledError:
        pass
    finally:
        seed_task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        console.print("\n[bold red]❌ Stopped by user.[/bold red]")