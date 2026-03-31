# bittorrent-client
- app sử dụng giao diện web + tuân thủ bep, tương thích và có thể tải file từ mạng bittorrent thực tế 
- có thể tương tác với các client v1 (ví dụ qTorrent, Transmission, ...)
## Hướng dẫn cài đặt và chạy
```bash
# sau khi clone thực hiện các lệnh sau ở thư mục gốc
python -m venv .venv

# linux
source ./.venv/bin/activate
# window 
.venv\Scripts\activate.bat


pip install -r requirements.txt

# chạy app bằng lệnh 
python web/app.py

# chương trình sẽ chạy tại http://127.0.0.1:5000
# files hoặc directory tải về từ mạng bittorrent sẽ được lưu trữ trong thư mục downloads
```
Lưu ý: Nếu quá trình cài đặt gặp lỗi (mất kết nối, thiếu công cụ build, xung đột phiên bản), hãy cài lại từng gói bằng lệnh
```bash
pip install <package_name>
```

## Giao diện web sau khi chạy thành công
- Lưu ý: nên sử dụng file torrent [abc.torrent](./torrent_files/abc.torrent) để tải
1. Trang chính
![home](./imgs/home.png)
2. Thêm file torrent ở nút Add ở góc màn hình
- file torrent trong thư mục `torrent_files` hoặc tải từ các trang web như `1337x`
![](./imgs/add_torrent_file.png)
3. Chọn file để tải 
![](./imgs/choose.png)
4. Giao diện khi đang thực hiện tải xuống
![](./imgs/general.png)
5. Trackers, Peers, Files 
- [Thông tin về  trackers của torrent file hiện tại](./imgs/trackers.png)
- [Thông tin về peers đang kết nối (choke, unchoke)](./imgs/peer.png)
- [Thông tin về files được chọn và tiến trình tải của nó](./imgs/files.png)