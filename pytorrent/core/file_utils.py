class File:
    def __init__(self, file_info: dict, offset: int, piece_size: int) -> None:
        self.size = file_info["length"]

        filename = file_info["path"]
        self.name = filename[-1] if isinstance(filename, list) else filename
        self.__bytes_written = 0
        self.__bytes_downloaded = 0

        start_piece, start_byte = divmod(offset, piece_size)
        end_piece, end_byte = divmod((offset + self.size), piece_size)

        self.start_piece = start_piece
        self.start_byte = start_byte
        self.end_piece = end_piece
        self.end_byte = end_byte

    def __repr__(self):
        return f"{self.name} ({self.size})"

    def get_bytes_written(self):
        return self.__bytes_written

    def _set_bytes_written(self, value):
        self.__bytes_written = value

    def get_bytes_downloaded(self):
        return self.__bytes_downloaded

    def _set_bytes_downloaded(self, value):
        self.__bytes_downloaded = value

    def get_download_progress(self, precision=2):
        download_progress = (self.__bytes_written / self.size) * 100
        return round(download_progress, precision)


class FileTree(list):
    def __init__(self, torrent_info: dict) -> None:
        offset = 0
        piece_size = torrent_info["piece_length"]

        if not isinstance(torrent_info["files"], list):
            file = {
                "path": torrent_info["files"],
                "length": torrent_info["size"],
            }
            self.append(File(file, offset, piece_size))
            return

        for file in torrent_info["files"]:
            self.append(File(file, offset, piece_size))
            offset += file["length"]
