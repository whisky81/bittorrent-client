import asyncio
import threading
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.append(str(PROJECT_ROOT))

from flask import Flask, render_template, jsonify, request
from pytorrent.torrent import Torrent
from pytorrent.download_manager import DownloadManager

app = Flask(__name__)
download_manager = DownloadManager()

# Global storage for active torrents
# Key: info_hash (str), Value: Torrent object
active_torrents: dict[str, Torrent] = {}

# Speed tracking
# Key: info_hash, Value: {last_dl, last_ul, dl_speed, ul_speed}
torrent_stats: dict[str, dict] = {}

# Store torrent file paths separately
torrent_file_paths: dict[str, Path] = {}

# Dedicated asyncio loop for torrent tasks
torrent_loop = asyncio.new_event_loop()


def run_async_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


threading.Thread(target=run_async_loop, args=(torrent_loop,), daemon=True).start()


async def update_stats():
    """Background task to calculate speeds every second."""
    while True:
        try:
            for info_hash, torrent in list(active_torrents.items()):
                if info_hash not in torrent_stats:
                    torrent_stats[info_hash] = {
                        "last_dl": 0, "last_ul": 0,
                        "dl_speed": 0, "ul_speed": 0,
                    }

                curr_dl = torrent.torrent_info.get("downloaded", 0)
                curr_ul = torrent.torrent_info.get("uploaded", 0)
                stats = torrent_stats[info_hash]
                stats["dl_speed"] = max(0, curr_dl - stats["last_dl"])
                stats["ul_speed"] = max(0, curr_ul - stats["last_ul"])
                stats["last_dl"] = curr_dl
                stats["last_ul"] = curr_ul
        except Exception:
            pass
        await asyncio.sleep(1)


asyncio.run_coroutine_threadsafe(update_stats(), torrent_loop)


def fmt_bytes(n: float) -> str:
    if n is None:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"


def get_tracker_url(tracker) -> str:
    """BUG FIX: Trackers have no .url attribute, build it from components."""
    scheme = getattr(tracker, "scheme", "udp")
    hostname = getattr(tracker, "hostname", "unknown")
    port = getattr(tracker, "port", 0)
    path = getattr(tracker, "path", "/announce")
    return f"{scheme}://{hostname}:{port}{path}"


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/torrents", methods=["GET"])
def get_torrents():
    data = []
    for info_hash, torrent in active_torrents.items():
        total_size = torrent.torrent_info.get("size", 1) or 1
        downloaded = torrent.torrent_info.get("downloaded", 0)

        file_names = [f.name for f in torrent.files] if torrent.files else []
        is_done = download_manager.is_already_downloaded(
            info_hash, torrent.name, file_names
        )
        progress = 100.0 if is_done else min((downloaded / total_size) * 100, 100.0)

        # BUG FIX: Peer uses .pieces not .bitfield for their piece availability
        active_peers_list = [p for p in torrent.peers if getattr(p, "active", False)]
        stats = torrent_stats.get(info_hash, {"dl_speed": 0, "ul_speed": 0})

        peer_details = []
        for p in active_peers_list:
            peer_progress = 0
            # BUG FIX: use p.pieces (BitArray), not p.bitfield
            pieces_bf = getattr(p, "pieces", None)
            if pieces_bf and len(pieces_bf) > 0:
                try:
                    peer_progress = round(
                        (pieces_bf.count(True) / len(pieces_bf)) * 100, 1
                    )
                except Exception:
                    peer_progress = 0

            peer_details.append({
                "address": f"{p.address[0]}:{p.address[1]}",
                "client": "Unknown",
                "progress": peer_progress,
                "choked": getattr(p, "choking_me", True),
            })

        # BUG FIX: build tracker URL from scheme/hostname/port attributes
        tracker_details = []
        for tr in torrent.trackers:
            status = "Working" if getattr(tr, "active", False) else "Failed"
            announce_resp = getattr(tr, "announce_response", {}) or {}
            seeders = announce_resp.get("seeders", 0)
            leechers = announce_resp.get("leechers", 0)

            tracker_details.append({
                "url": get_tracker_url(tr),
                "status": status,
                "seeders": seeders,
                "leechers": leechers,
            })

        # Determine status label
        if progress >= 100:
            status = "Seeding"
        elif stats["dl_speed"] > 0:
            status = "Downloading"
        else:
            status = "Stalled"

        # ETA calculation
        eta = ""
        if stats["dl_speed"] > 0 and progress < 100:
            remaining_bytes = total_size - downloaded
            secs = int(remaining_bytes / stats["dl_speed"])
            if secs < 3600:
                eta = f"{secs // 60}m {secs % 60}s"
            else:
                eta = f"{secs // 3600}h {(secs % 3600) // 60}m"

        ratio = round(
            torrent.torrent_info.get("uploaded", 0) / max(downloaded, 1), 3
        )

        data.append({
            "info_hash": info_hash,
            "name": torrent.name,
            "size_raw": total_size,
            "size": fmt_bytes(total_size),
            "downloaded_raw": downloaded,
            "downloaded": fmt_bytes(downloaded),
            "uploaded": fmt_bytes(torrent.torrent_info.get("uploaded", 0)),
            "progress": round(progress, 2),
            "peers_count": len(active_peers_list),
            "dl_speed": fmt_bytes(stats["dl_speed"]) + "/s",
            "ul_speed": fmt_bytes(stats["ul_speed"]) + "/s",
            "dl_speed_raw": stats["dl_speed"],
            "ul_speed_raw": stats["ul_speed"],
            "status": status,
            "eta": eta or "∞" if progress < 100 else "",
            "ratio": ratio,
            "num_files": len(file_names),
            "details": {
                "peers": peer_details,
                "trackers": tracker_details,
                "files": [
                    {
                        "name": f.name,
                        "size": fmt_bytes(f.size),
                        "progress": f.get_download_progress(),
                    }
                    for f in (torrent.files or [])
                ],
            },
        })
    return jsonify(data)


async def start_torrent_task(torrent: Torrent, info_hash: str):
    """Sequence the torrent initialization, download, and seeding."""
    try:
        save_dir = Path.cwd() / "downloads"
        await torrent.init(save_dir=save_dir)

        if torrent.files:
            file = torrent.files[0]
            file_names = [f.name for f in torrent.files]

            if not download_manager.is_already_downloaded(
                info_hash, torrent.name, file_names
            ):
                await torrent.download(file)
                torrent_fp = torrent_file_paths.get(info_hash, Path(""))
                download_manager.mark_downloaded(
                    info_hash, torrent.save_dir, torrent_fp
                )

        await torrent.seed()
    except Exception as e:
        print(f"Error in torrent task {info_hash}: {e}")


UPLOAD_FOLDER = Path.cwd() / "downloads" / "torrents"
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)


@app.route("/api/upload", methods=["POST"])
def upload_torrent():
    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400

    file = request.files["file"]
    if not file or not file.filename:
        return jsonify({"error": "No selected file"}), 400

    if not file.filename.endswith(".torrent"):
        return jsonify({"error": "Only .torrent files are allowed"}), 400

    filename = file.filename
    save_path = UPLOAD_FOLDER / filename
    file.save(str(save_path))

    try:
        torrent = Torrent(str(save_path))
        info_hash = torrent.torrent_info["info_hash"].hex()

        if info_hash in active_torrents:
            return jsonify({"error": "Torrent already active"}), 400

        active_torrents[info_hash] = torrent
        torrent_file_paths[info_hash] = save_path

        asyncio.run_coroutine_threadsafe(
            start_torrent_task(torrent, info_hash),
            torrent_loop,
        )

        return jsonify({
            "success": True,
            "info_hash": info_hash,
            "name": torrent.name,
        })
    except Exception as e:
        return jsonify({"error": f"Failed to initialize torrent: {str(e)}"}), 500


@app.route("/api/add", methods=["POST"])
def add_torrent():
    """Add torrent by file path on disk (for CLI/testing use)."""
    body = request.get_json(silent=True) or {}
    torrent_path_str = body.get("path", "")
    if not torrent_path_str:
        return jsonify({"error": "Missing 'path' field"}), 400

    torrent_path = Path(torrent_path_str)
    if not torrent_path.exists():
        return jsonify({"error": f"File not found: {torrent_path_str}"}), 404

    try:
        torrent = Torrent(str(torrent_path))
        info_hash = torrent.torrent_info["info_hash"].hex()

        if info_hash in active_torrents:
            return jsonify({"error": "Torrent already active"}), 400

        active_torrents[info_hash] = torrent
        torrent_file_paths[info_hash] = torrent_path

        asyncio.run_coroutine_threadsafe(
            start_torrent_task(torrent, info_hash),
            torrent_loop,
        )

        return jsonify({
            "success": True,
            "info_hash": info_hash,
            "name": torrent.name,
        })
    except Exception as e:
        return jsonify({"error": f"Failed to initialize torrent: {str(e)}"}), 500


@app.route("/api/delete/<info_hash>", methods=["DELETE"])
def delete_torrent(info_hash):
    if info_hash in active_torrents:
        active_torrents.pop(info_hash, None)
        torrent_stats.pop(info_hash, None)
        torrent_file_paths.pop(info_hash, None)
        return jsonify({"success": True})
    return jsonify({"error": "Not found"}), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)
