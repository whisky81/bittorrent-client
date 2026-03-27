from pathlib import Path
import json 
from datetime import datetime

class DownloadManager:
    def __init__(self, file_name: str="downloaded_files.json"):
        self.data_dir: Path = Path.cwd() / "data"
        if not self.data_dir.exists(): 
            self.data_dir.mkdir(exist_ok=True)
        self.downloaded_files: Path = self.data_dir / file_name
        self.data: dict = self._load()
    
    def _load(self):
        try:
            with self.downloaded_files.open(encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
            print(e) 
            return {}
    
    def _save(self):
        with self.downloaded_files.open("w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
    
    def is_already_downloaded(
        self, 
        info_hash: str, 
        name: str, 
        files: list[str], 
        torrent_file_path: Path|None=None
    ):
        save_dir = None
        if info_hash in self.data:
            save_dir = Path(self.data[info_hash]["save_dir"])
        else:
            # Speculative check: try default downloads/<name> folder
            default_save_dir = Path.cwd() / "downloads" / name
            if default_save_dir.exists() and default_save_dir.is_dir():
                save_dir = default_save_dir
        
        if not save_dir:
            return False 
            
        # Verify specific folder name matches (redundancy check)
        if save_dir.name != name:
            return False
        
        for file_name in files:
            file_path = save_dir / file_name
            if not (file_path.exists() and file_path.is_file()):
                return False 
        
        # If we arrived here via speculative check, record it
        if info_hash not in self.data:
            # Explicit check to satisfy type checker
            actual_path = torrent_file_path if torrent_file_path is not None else Path("")
            self.mark_downloaded(info_hash, save_dir, actual_path)
        
        if torrent_file_path and torrent_file_path.exists():
            self.data[info_hash]["torrent_file"] = str(torrent_file_path)
            self._save()
        return True 
    
    def mark_downloaded(self, info_hash: str, save_dir: Path, torrent_file_path: Path):
        self.data[info_hash] = {
            "save_dir": str(save_dir),
            "torrent_file": str(torrent_file_path),
            "completed_at": datetime.now().isoformat()
        }
        self._save()
    