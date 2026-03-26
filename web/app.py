import asyncio
import threading
import time
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.append(str(PROJECT_ROOT))

from flask import Flask, render_template, jsonify, request
from pytorrent.torrent import Torrent
from pytorrent.download_manager import DownloadManager

app = Flask(__name__)
download_manager = DownloadManager()

# ── Global state ────────────────────────────────────────────────────────────

# Torrents that have been parsed but not yet started (waiting for user to
# select files in the UI).
pending_torrents: dict[str, dict] = {}
# key → {torrent: Torrent, save_path: Path}

# Active torrents being downloaded / seeded.
active_torrents: dict[str, Torrent] = {}

# Speed tracking: info_hash → {last_dl, last_ul, dl_speed, ul_speed}
torrent_stats: dict[str, dict] = {}

# File paths of .torrent files on disk: info_hash → Path
torrent_file_paths: dict[str, Path] = {}

# Which file indices the user selected to download: info_hash → [int, ...]
torrent_selected_files: dict[str, list[int]] = {}

# Completion notifications waiting to be shown in the UI.
# Each entry: {type, info_hash, name, files, save_dir, timestamp}
pending_notifications: list[dict] = []

# Dedicated asyncio event-loop running in a background thread
torrent_loop = asyncio.new_event_loop()


def run_async_loop(loop: asyncio.AbstractEventLoop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


threading.Thread(target=run_async_loop, args=(torrent_loop,), daemon=True).start()


# ── Background speed tracking ────────────────────────────────────────────────

async def update_stats():
    while True:
        try:
            for info_hash, torrent in list(active_torrents.items()):
                if info_hash not in torrent_stats:
                    torrent_stats[info_hash] = {
                        "last_dl": 0, "last_ul": 0,
                        "dl_speed": 0.0, "ul_speed": 0.0,
                    }
                curr_dl = torrent.torrent_info.get("downloaded", 0)
                curr_ul = torrent.torrent_info.get("uploaded", 0)
                s = torrent_stats[info_hash]
                s["dl_speed"] = max(0.0, float(curr_dl - s["last_dl"]))
                s["ul_speed"] = max(0.0, float(curr_ul - s["last_ul"]))
                s["last_dl"] = curr_dl
                s["last_ul"] = curr_ul
        except Exception:
            pass
        await asyncio.sleep(1)


asyncio.run_coroutine_threadsafe(update_stats(), torrent_loop)


# ── Helpers ──────────────────────────────────────────────────────────────────

def fmt_bytes(n: float) -> str:
    if not n:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"


def get_tracker_url(tracker) -> str:
    scheme = getattr(tracker, "scheme", "udp")
    hostname = getattr(tracker, "hostname", "unknown")
    port = getattr(tracker, "port", 0)
    path = getattr(tracker, "path", "/announce")
    return f"{scheme}://{hostname}:{port}{path}"


def _selected_progress(torrent: Torrent, info_hash: str) -> tuple[float, float, float]:
    """
    Returns (progress_pct, downloaded_bytes, total_bytes) restricted to the
    files the user selected.  Falls back to all files if nothing is selected.
    """
    if not torrent.files:
        return 0.0, 0.0, 1.0

    indices = torrent_selected_files.get(info_hash) or list(range(len(torrent.files)))
    valid = [i for i in indices if 0 <= i < len(torrent.files)]
    if not valid:
        return 0.0, 0.0, 1.0

    total = sum(torrent.files[i].size for i in valid)
    downloaded = sum(torrent.files[i].get_bytes_written() for i in valid)
    pct = min((downloaded / max(total, 1)) * 100.0, 100.0)
    return pct, float(downloaded), float(total)


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/torrents", methods=["GET"])
def get_torrents():
    data = []
    for info_hash, torrent in active_torrents.items():
        progress, dl_bytes, total_bytes = _selected_progress(torrent, info_hash)

        # Check if truly done (all selected files fully written)
        selected = torrent_selected_files.get(info_hash) or list(range(len(torrent.files or [])))
        all_done = download_manager.is_already_downloaded(
            info_hash, torrent.name,
            [torrent.files[i].name for i in selected if torrent.files and i < len(torrent.files)]
        )
        if all_done:
            progress = 100.0

        active_peers_list = [p for p in torrent.peers if getattr(p, "active", False)]
        stats = torrent_stats.get(info_hash, {"dl_speed": 0.0, "ul_speed": 0.0})

        # Status
        if progress >= 100.0:
            status = "Seeding"
        elif stats["dl_speed"] > 0:
            status = "Downloading"
        else:
            status = "Stalled"

        # ETA
        eta = ""
        if stats["dl_speed"] > 0 and progress < 100:
            remaining = total_bytes - dl_bytes
            secs = int(remaining / stats["dl_speed"])
            eta = f"{secs // 3600}h {(secs % 3600) // 60}m" if secs >= 3600 else f"{secs // 60}m {secs % 60}s"

        ratio = round(torrent.torrent_info.get("uploaded", 0) / max(dl_bytes, 1), 3)

        # Peer details (use .pieces BitArray, not .bitfield)
        peer_details = []
        for p in active_peers_list:
            peer_progress = 0
            bf = getattr(p, "pieces", None)
            if bf and len(bf) > 0:
                try:
                    peer_progress = round((bf.count(True) / len(bf)) * 100, 1)
                except Exception:
                    pass
            peer_details.append({
                "address": f"{p.address[0]}:{p.address[1]}",
                "client": "Unknown",
                "progress": peer_progress,
                "choked": getattr(p, "choking_me", True),
            })

        # Tracker details
        tracker_details = []
        for tr in torrent.trackers:
            resp = getattr(tr, "announce_response", {}) or {}
            tracker_details.append({
                "url": get_tracker_url(tr),
                "status": "Working" if getattr(tr, "active", False) else "Failed",
                "seeders": resp.get("seeders", 0),
                "leechers": resp.get("leechers", 0),
                "interval": resp.get("interval", 0),
            })

        # File details (selected files only)
        file_details = []
        if torrent.files:
            for i, f in enumerate(torrent.files):
                is_sel = i in selected
                file_details.append({
                    "index": i,
                    "name": f.name,
                    "size": fmt_bytes(f.size),
                    "size_raw": f.size,
                    "progress": f.get_download_progress() if is_sel else 0.0,
                    "selected": is_sel,
                })

        data.append({
            "info_hash": info_hash,
            "name": torrent.name,
            "size": fmt_bytes(total_bytes),
            "size_raw": total_bytes,
            "downloaded": fmt_bytes(dl_bytes),
            "downloaded_raw": dl_bytes,
            "uploaded": fmt_bytes(torrent.torrent_info.get("uploaded", 0)),
            "progress": round(progress, 2),
            "peers_count": len(active_peers_list),
            "dl_speed": fmt_bytes(stats["dl_speed"]) + "/s",
            "ul_speed": fmt_bytes(stats["ul_speed"]) + "/s",
            "dl_speed_raw": stats["dl_speed"],
            "ul_speed_raw": stats["ul_speed"],
            "status": status,
            "eta": eta or ("∞" if progress < 100 else ""),
            "ratio": ratio,
            "num_files": len(torrent.files) if torrent.files else 0,
            "save_dir": str(torrent.save_dir),
            "details": {
                "peers": peer_details,
                "trackers": tracker_details,
                "files": file_details,
            },
        })
    return jsonify(data)


@app.route("/api/notifications", methods=["GET"])
def get_notifications():
    """Return and clear pending notifications."""
    notifs = list(pending_notifications)
    pending_notifications.clear()
    return jsonify(notifs)


# ── Upload flow (two-step) ────────────────────────────────────────────────────

UPLOAD_FOLDER = Path.cwd() / "downloads" / "torrents"
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)


@app.route("/api/upload", methods=["POST"])
def upload_torrent():
    """
    Step 1 of 2.
    Parse the .torrent file and return its file list so the UI can show a
    file-selection dialog.  The torrent is NOT started yet.
    """
    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400

    f = request.files["file"]
    if not f or not f.filename:
        return jsonify({"error": "No file selected"}), 400
    if not f.filename.endswith(".torrent"):
        return jsonify({"error": "Only .torrent files are allowed"}), 400

    save_path = UPLOAD_FOLDER / f.filename
    f.save(str(save_path))

    try:
        torrent = Torrent(str(save_path))
        info_hash = torrent.torrent_info["info_hash"].hex()

        if info_hash in active_torrents:
            return jsonify({"error": "Torrent already active"}), 400

        # Store as pending until the user confirms file selection
        pending_torrents[info_hash] = {
            "torrent": torrent,
            "save_path": save_path,
        }

        files = []
        if torrent.files:
            for i, file_obj in enumerate(torrent.files):
                files.append({
                    "index": i,
                    "name": file_obj.name,
                    "size": fmt_bytes(file_obj.size),
                    "size_raw": file_obj.size,
                })

        return jsonify({
            "success": True,
            "info_hash": info_hash,
            "name": torrent.name,
            "total_size": fmt_bytes(torrent.torrent_info["size"]),
            "files": files,
        })
    except Exception as e:
        return jsonify({"error": f"Failed to parse torrent: {str(e)}"}), 500


@app.route("/api/start", methods=["POST"])
def start_torrent():
    """
    Step 2 of 2.
    Start the torrent with the file indices the user selected.
    Body JSON: { "info_hash": "...", "selected_indices": [0, 1, ...] }
    """
    body = request.get_json(silent=True) or {}
    info_hash = body.get("info_hash", "")
    selected = body.get("selected_indices", [])

    if not info_hash:
        return jsonify({"error": "Missing info_hash"}), 400

    entry = pending_torrents.pop(info_hash, None)
    if not entry:
        if info_hash in active_torrents:
            return jsonify({"error": "Torrent already active"}), 400
        return jsonify({"error": "Torrent not found in pending list"}), 404

    torrent: Torrent = entry["torrent"]
    save_path: Path = entry["save_path"]

    # Default: download all files if nothing selected
    if not selected and torrent.files:
        selected = list(range(len(torrent.files)))

    active_torrents[info_hash] = torrent
    torrent_file_paths[info_hash] = save_path
    torrent_selected_files[info_hash] = selected

    asyncio.run_coroutine_threadsafe(
        _start_torrent_task(torrent, info_hash, selected),
        torrent_loop,
    )

    return jsonify({"success": True, "info_hash": info_hash, "name": torrent.name})


async def _start_torrent_task(torrent: Torrent, info_hash: str, file_indices: list[int]):
    """Init → download selected files → notify → seed."""
    try:
        save_dir = Path.cwd() / "downloads"
        await torrent.init(save_dir=save_dir)

        if torrent.files and file_indices:
            valid = [i for i in file_indices if 0 <= i < len(torrent.files)]
            file_names = [torrent.files[i].name for i in valid]

            if not download_manager.is_already_downloaded(
                info_hash, torrent.name, file_names
            ):
                await torrent.download_files(valid)

                # Mark all selected files as downloaded
                torrent_fp = torrent_file_paths.get(info_hash, Path(""))
                download_manager.mark_downloaded(info_hash, torrent.save_dir, torrent_fp)

            # Push a completion notification for the UI
            pending_notifications.append({
                "type": "download_complete",
                "info_hash": info_hash,
                "name": torrent.name,
                "files": [torrent.files[i].name for i in valid],
                "num_files": len(valid),
                "save_dir": str(torrent.save_dir),
                "timestamp": time.time(),
            })

        await torrent.seed()
    except Exception as e:
        print(f"[torrent task {info_hash}] error: {e}")


# ── Legacy add-by-path endpoint ───────────────────────────────────────────────

@app.route("/api/add", methods=["POST"])
def add_torrent():
    """Add torrent by file-system path (CLI / testing)."""
    body = request.get_json(silent=True) or {}
    path_str = body.get("path", "")
    if not path_str:
        return jsonify({"error": "Missing 'path' field"}), 400

    torrent_path = Path(path_str)
    if not torrent_path.exists():
        return jsonify({"error": f"File not found: {path_str}"}), 404

    try:
        torrent = Torrent(str(torrent_path))
        info_hash = torrent.torrent_info["info_hash"].hex()

        if info_hash in active_torrents:
            return jsonify({"error": "Torrent already active"}), 400

        selected = list(range(len(torrent.files))) if torrent.files else []
        active_torrents[info_hash] = torrent
        torrent_file_paths[info_hash] = torrent_path
        torrent_selected_files[info_hash] = selected

        asyncio.run_coroutine_threadsafe(
            _start_torrent_task(torrent, info_hash, selected),
            torrent_loop,
        )

        return jsonify({"success": True, "info_hash": info_hash, "name": torrent.name})
    except Exception as e:
        return jsonify({"error": f"Failed: {str(e)}"}), 500


@app.route("/api/delete/<info_hash>", methods=["DELETE"])
def delete_torrent(info_hash):
    active_torrents.pop(info_hash, None)
    torrent_stats.pop(info_hash, None)
    torrent_file_paths.pop(info_hash, None)
    torrent_selected_files.pop(info_hash, None)
    pending_torrents.pop(info_hash, None)
    return jsonify({"success": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)
