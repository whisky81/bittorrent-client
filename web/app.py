import asyncio
import threading
import os
import json
import sys
from pathlib import Path

# Fix import path for pytorrent
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.append(str(PROJECT_ROOT))

from flask import Flask, render_template, jsonify, request
from pytorrent.torrent import Torrent
from pytorrent.download_manager import DownloadManager

app = Flask(__name__)
download_manager = DownloadManager()

# Global storage for active torrents
# Key: info_hash (str), Value: Torrent object
active_torrents = {}

# Speed tracking
# Key: info_hash, Value: {"last_dl": int, "last_ul": int, "dl_speed": float, "ul_speed": float}
torrent_stats = {}

# Dedicated asyncio loop for torrent tasks
torrent_loop = asyncio.new_event_loop()

def run_async_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

# Start the background thread
threading.Thread(target=run_async_loop, args=(torrent_loop,), daemon=True).start()

async def update_stats():
    """Background task to calculate speeds every second."""
    while True:
        try:
            for info_hash, torrent in list(active_torrents.items()):
                if info_hash not in torrent_stats:
                    torrent_stats[info_hash] = {"last_dl": 0, "last_ul": 0, "dl_speed": 0, "ul_speed": 0}
                
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

# Start stats update in the background loop
asyncio.run_coroutine_threadsafe(update_stats(), torrent_loop)

def fmt_bytes(n: float) -> str:
    if n is None: return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/torrents", methods=["GET"])
def get_torrents():
    data = []
    for info_hash, torrent in active_torrents.items():
        # Calculate progress
        total_size = torrent.torrent_info.get("size", 1)
        downloaded = torrent.torrent_info.get("downloaded", 0)
        
        # Robust progress calculation
        is_done = download_manager.is_already_downloaded(info_hash, torrent.name, [f.name for f in torrent.files]) if torrent.files else False
        progress = 100.0 if is_done else (downloaded / total_size) * 100
        if progress > 100: progress = 100.0
        
        active_peers = [p for p in torrent.peers if getattr(p, "active", False)]
        stats = torrent_stats.get(info_hash, {"dl_speed": 0, "ul_speed": 0})
        
        # Gather detailed peer info
        peer_details = []
        for p in active_peers:
            peer_progress = 0
            if hasattr(p, 'bitfield') and p.bitfield: # type: ignore
                peer_progress = round((p.bitfield.count(True) / len(p.bitfield)) * 100, 1) # type: ignore
                
            peer_details.append({
                "address": f"{p.address[0]}:{p.address[1]}",
                "progress": peer_progress,
                "client": "Unknown" # Could parse from peer_id
            })

        # Gather detailed tracker info
        tracker_details = []
        for tr in torrent.trackers:
            status = "Working" if getattr(tr, "active", False) else "Failed"
            peers_found = 0
            if hasattr(tr, "announce_response") and tr.announce_response:
                res = tr.announce_response
                peers_found = len(res.get("peers", res.get(b"peers", [])))
            
            tracker_details.append({
                "url": getattr(tr, "url", "Unknown"),
                "status": status,
                "peers": peers_found
            })

        data.append({
            "info_hash": info_hash,
            "name": torrent.name,
            "size": fmt_bytes(total_size),
            "downloaded": fmt_bytes(downloaded),
            "uploaded": fmt_bytes(torrent.torrent_info.get("uploaded", 0)),
            "progress": round(progress, 2),
            "peers_count": len(active_peers),
            "dl_speed": fmt_bytes(stats["dl_speed"]) + "/s",
            "ul_speed": fmt_bytes(stats["ul_speed"]) + "/s",
            "status": "Seeding" if progress >= 100 else "Downloading",
            "details": {
                "peers": peer_details,
                "trackers": tracker_details
            }
        })
    return jsonify(data)

async def start_torrent_task(torrent, info_hash):
    """Sequence the torrent initialization, download, and seeding."""
    try:
        # 1. Initialize
        save_dir = Path.cwd() / "downloads"
        await torrent.init(save_dir=save_dir)
        
        # 2. Check and Download (simplified: first file)
        if torrent.files:
            file = torrent.files[0]
            if not download_manager.is_already_downloaded(info_hash, torrent.name, [file.name]):
                await torrent.download(file)
                # Mark as downloaded after success
                download_manager.mark_downloaded(info_hash, torrent.save_dir, Path(torrent.torrent_file))
            
            # 3. Always enter seed mode after download or if already existing
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
    if file.filename == "":
        return jsonify({"error": "No selected file"}), 400
    
    if file and file.filename.endswith(".torrent"):
        # Save the file to torrents folder
        filename = file.filename
        save_path = UPLOAD_FOLDER / filename
        file.save(str(save_path))
        
        # Now add it exactly like the previous add_torrent did
        try:
            torrent = Torrent(str(save_path))
            torrent.torrent_file = str(save_path) # Store path for mark_downloaded
            info_hash = torrent.torrent_info["info_hash"].hex()
            
            if info_hash in active_torrents:
                return jsonify({"error": "Torrent already active"}), 400
            
            active_torrents[info_hash] = torrent
            
            # Start the sequence in the background loop
            asyncio.run_coroutine_threadsafe(
                start_torrent_task(torrent, info_hash), 
                torrent_loop
            )
            
            return jsonify({
                "success": True, 
                "info_hash": info_hash,
                "name": torrent.name
            })
        except Exception as e:
            return jsonify({"error": f"Failed to initialize torrent: {str(e)}"}), 500
            
    return jsonify({"error": "Only .torrent files are allowed"}), 400

@app.route("/api/add", methods=["POST"])
def add_torrent():
    # Keep this for backward compatibility or direct path testing
    torrent_path = request.json.get("path")
    # ... (remains same or can be simplified)

@app.route("/api/delete/<info_hash>", methods=["DELETE"])
def delete_torrent(info_hash):
    if info_hash in active_torrents:
        # Note: We should properly stop tasks here
        active_torrents.pop(info_hash, None)
        return jsonify({"success": True})
    return jsonify({"error": "Not found"}), 404

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)
