import asyncio
import threading
import time
import sys
import subprocess
import platform
import os
import json
import logging
from pathlib import Path
from collections import deque

# Fix 2: Tắt werkzeug HTTP access log trước khi import Flask
# (tránh hàng triệu dòng log mỗi giờ do SSE/polling)
logging.getLogger("werkzeug").setLevel(logging.ERROR)

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.append(str(PROJECT_ROOT))

from flask import Flask, render_template, jsonify, request, Response, stream_with_context
from pytorrent.torrent import Torrent
from pytorrent.download_manager import DownloadManager

app = Flask(__name__)
download_manager = DownloadManager()

# ── Fix 5: Giới hạn số torrent đang seed đồng thời ──────────────────────────
MAX_ACTIVE_SEEDS = 5

# ── Fix 4: Lock cho trạng thái dùng chung giữa Flask thread và asyncio loop ─
_notifications_lock = threading.Lock()

# ── Global state ─────────────────────────────────────────────────────────────
pending_torrents: dict[str, dict] = {}
active_torrents: dict[str, Torrent] = {}
torrent_stats: dict[str, dict] = {}
torrent_file_paths: dict[str, Path] = {}
torrent_selected_files: dict[str, list[int]] = {}

# Fix 1: Tập hợp các torrent đã thực sự hoàn thành download trong session này
# Chỉ force 100% khi có trong set này, không dùng is_already_downloaded() để ép UI
completed_hashes: set[str] = set()

# Fix 4: deque thread-safe, maxlen ngăn memory leak khi notifications chất đống
pending_notifications: deque = deque(maxlen=100)

torrent_loop = asyncio.new_event_loop()


def run_async_loop(loop: asyncio.AbstractEventLoop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


threading.Thread(target=run_async_loop, args=(torrent_loop,), daemon=True).start()


# ── Fix 6: Speed tracking giữ nguyên (chạy trên asyncio loop) ───────────────

async def update_stats():
    while True:
        try:
            now = time.time()
            for info_hash, torrent in list(active_torrents.items()):
                if info_hash not in torrent_stats:
                    torrent_stats[info_hash] = {
                        "last_dl": 0, "last_ul": 0,
                        "dl_speed": 0.0, "ul_speed": 0.0,
                        "last_time": now,
                    }
                curr_dl = torrent.torrent_info.get("downloaded", 0)
                curr_ul = torrent.torrent_info.get("uploaded", 0)
                s = torrent_stats[info_hash]
                elapsed = now - s.get("last_time", now)
                if elapsed > 0:
                    s["dl_speed"] = max(0.0, (curr_dl - s["last_dl"]) / elapsed)
                    s["ul_speed"] = max(0.0, (curr_ul - s["last_ul"]) / elapsed)
                s["last_dl"] = curr_dl
                s["last_ul"] = curr_ul
                s["last_time"] = now
        except Exception:
            pass
        await asyncio.sleep(1)


asyncio.run_coroutine_threadsafe(update_stats(), torrent_loop)


# ── Folder opener ─────────────────────────────────────────────────────────────

def open_folder(path: str) -> tuple[bool, str]:
    folder = Path(path)
    if not folder.exists():
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return False, f"Directory does not exist and could not be created: {e}"

    system = platform.system()
    try:
        if system == "Windows":
            os.startfile(str(folder)) # type: ignore
        elif system == "Darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            for fm in ["xdg-open", "nautilus", "dolphin", "thunar", "nemo", "pcmanfm"]:
                try:
                    subprocess.Popen([fm, str(folder)])
                    break
                except FileNotFoundError:
                    continue
            else:
                return False, "No compatible file manager found on this system"
        return True, ""
    except Exception as e:
        return False, str(e)


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
    Fix 1: Tính progress thuần từ bytes_written, KHÔNG override bằng is_already_downloaded.
    Việc force 100% chỉ xảy ra trong _build_torrents_data khi info_hash ∈ completed_hashes.
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


def _drain_notifications() -> list:
    """Fix 4: thread-safe drain dùng lock."""
    with _notifications_lock:
        notifs = list(pending_notifications)
        pending_notifications.clear()
    return notifs


def _build_torrents_data() -> list:
    """Xây dựng data cho SSE/REST. Dùng list(items()) để tránh race condition khi iterate."""
    data = []
    # Fix 4: snapshot dict để tránh RuntimeError khi dict thay đổi mid-iteration
    for info_hash, torrent in list(active_torrents.items()):
        try:
            progress, dl_bytes, total_bytes = _selected_progress(torrent, info_hash)

            # Fix 1: chỉ force 100% khi torrent thực sự hoàn thành trong session này
            if info_hash in completed_hashes:
                progress = 100.0
                dl_bytes = total_bytes

            active_peers_list = [p for p in torrent.peers if getattr(p, "active", False)]
            stats = torrent_stats.get(info_hash, {"dl_speed": 0.0, "ul_speed": 0.0})

            if progress >= 100.0:
                status = "Seeding"
            elif stats["dl_speed"] > 0:
                status = "Downloading"
            else:
                status = "Stalled"

            eta = ""
            if stats["dl_speed"] > 0 and progress < 100:
                remaining = total_bytes - dl_bytes
                secs = int(remaining / max(stats["dl_speed"], 1))
                eta = (
                    f"{secs // 3600}h {(secs % 3600) // 60}m"
                    if secs >= 3600
                    else f"{secs // 60}m {secs % 60}s"
                )

            ratio = round(torrent.torrent_info.get("uploaded", 0) / max(dl_bytes, 1), 3)
            selected = torrent_selected_files.get(info_hash) or list(range(len(torrent.files or [])))

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

            file_details = []
            if torrent.files:
                for i, f in enumerate(torrent.files):
                    is_sel = i in selected
                    file_details.append({
                        "index": i,
                        "name": f.name,
                        "size": fmt_bytes(f.size),
                        "size_raw": f.size,
                        # Fix 1: file progress cũng phải phản ánh bytes_written thực tế
                        "progress": f.get_download_progress() if is_sel else 0.0,
                        "selected": is_sel,
                    })

            save_dir_str = str(torrent.save_dir) if hasattr(torrent, "save_dir") else ""
            nat_st = torrent.get_nat_status() if hasattr(torrent, "get_nat_status") else {}

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
                "save_dir": save_dir_str,
                "nat": {
                    "method":        nat_st.get("method"),
                    "active":        nat_st.get("active", False),
                    "external_ip":   nat_st.get("external_ip"),
                    "external_port": nat_st.get("external_port"),
                },
                "details": {
                    "peers": peer_details,
                    "trackers": tracker_details,
                    "files": file_details,
                },
            })
        except Exception:
            pass
    return data


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# Fix 6: SSE endpoint thay thế 2 setInterval polling (/api/torrents + /api/notifications)
@app.route("/api/stream")
def stream_updates():
    """
    Server-Sent Events: đẩy torrents + notifications mỗi 1 giây.
    Browser tự reconnect khi mất kết nối (EventSource spec).
    Giảm đáng kể số kết nối HTTP so với polling mỗi 2s.
    """
    def event_gen():
        try:
            while True:
                try:
                    payload = {
                        "torrents": _build_torrents_data(),
                        "notifications": _drain_notifications(),
                    }
                    yield f"data: {json.dumps(payload)}\n\n"
                except Exception as e:
                    # Gửi lỗi nhưng không ngắt stream
                    yield f"data: {json.dumps({'error': str(e), 'torrents': [], 'notifications': []})}\n\n"
                time.sleep(1)
        except GeneratorExit:
            # Client đóng kết nối - generator kết thúc sạch
            pass

    return Response(
        stream_with_context(event_gen()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # Tắt Nginx buffering nếu deploy sau proxy
            "Connection": "keep-alive",
        },
    )


@app.route("/api/torrents", methods=["GET"])
def get_torrents():
    """REST endpoint giữ lại cho nút Refresh thủ công và backward compat."""
    return jsonify(_build_torrents_data())


@app.route("/api/notifications", methods=["GET"])
def get_notifications():
    """Giữ lại cho backward compat, SSE là cơ chế chính."""
    return jsonify(_drain_notifications())


@app.route("/api/open-folder", methods=["POST"])
def api_open_folder():
    body = request.get_json(silent=True) or {}
    path = body.get("path", "").strip()
    if not path:
        return jsonify({"error": "Missing 'path' field"}), 400
    success, error = open_folder(path)
    if success:
        return jsonify({"success": True})
    return jsonify({"error": error}), 500


# ── Upload flow ───────────────────────────────────────────────────────────────

UPLOAD_FOLDER = Path.cwd() / "downloads" / "torrents"
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)


@app.route("/api/upload", methods=["POST"])
def upload_torrent():
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

        pending_torrents[info_hash] = {"torrent": torrent, "save_path": save_path}

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
    """
    Init → download → notify → seed (với giới hạn Fix 5).
    Fix 1: khởi tạo bytes_written từ disk nếu đã tải xong trước đó.
    Fix 4: notifications dùng lock.
    """
    try:
        save_dir = Path.cwd() / "downloads"
        await torrent.init(save_dir=save_dir)

        if torrent.files and file_indices:
            valid = [i for i in file_indices if 0 <= i < len(torrent.files)]
            file_names = [torrent.files[i].name for i in valid]

            already_done = download_manager.is_already_downloaded(
                info_hash, torrent.name, file_names
            )

            if already_done:
                # Fix 1: khởi tạo bytes_written từ kích thước file thực trên disk
                # → file detail cũng hiện đúng progress thay vì 0%
                for i in valid:
                    f = torrent.files[i]
                    fp = torrent.save_dir / f.name
                    if fp.exists():
                        f._set_bytes_written(fp.stat().st_size)
                completed_hashes.add(info_hash)
            else:
                await torrent.download_files(valid)
                completed_hashes.add(info_hash)
                torrent_fp = torrent_file_paths.get(info_hash, Path(""))
                download_manager.mark_downloaded(info_hash, torrent.save_dir, torrent_fp)

            # Fix 4: lock khi ghi notification
            with _notifications_lock:
                pending_notifications.append({
                    "type": "download_complete",
                    "info_hash": info_hash,
                    "name": torrent.name,
                    "files": [torrent.files[i].name for i in valid],
                    "num_files": len(valid),
                    "save_dir": str(torrent.save_dir),
                    "timestamp": time.time(),
                })

        # Fix 5: chỉ seed khi số lượng seeding hiện tại < giới hạn
        # Đếm trong asyncio loop nên không có race condition (cooperative multitasking)
        current_seeding = sum(
            1 for t in active_torrents.values()
            if t.torrent_info.get("_seeding", False)
        )
        if current_seeding < MAX_ACTIVE_SEEDS:
            torrent.torrent_info["_seeding"] = True
            await torrent.seed()
        else:
            logging.getLogger(__name__).info(
                f"Seeding limit ({MAX_ACTIVE_SEEDS}) reached, "
                f"torrent '{torrent.name}' will not seed."
            )

    except Exception as e:
        import traceback
        print(f"[torrent task {info_hash}] error: {e}")
        traceback.print_exc()


# ── Legacy add-by-path endpoint ───────────────────────────────────────────────

@app.route("/api/add", methods=["POST"])
def add_torrent():
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
    torrent = active_torrents.pop(info_hash, None)
    if torrent and hasattr(torrent, "cleanup_nat"):
        try:
            torrent.cleanup_nat()
        except Exception:
            pass
    torrent_stats.pop(info_hash, None)
    torrent_file_paths.pop(info_hash, None)
    torrent_selected_files.pop(info_hash, None)
    pending_torrents.pop(info_hash, None)
    completed_hashes.discard(info_hash)
    return jsonify({"success": True})


if __name__ == "__main__":
    print("Running on http://127.0.0.1:5000")
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)
