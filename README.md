# bittorrent-client

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
