#!/bin/bash
cd

# 1. Cấp quyền bộ nhớ và cập nhật toàn bộ kho gói Termux
# 1. Khởi tạo lại quyền truy cập bộ nhớ
if [ -e "/data/data/com.termux/files/home/storage" ]; then
    rm -rf /data/data/com.termux/files/home/storage
fi
termux-setup-storage
pkg update -y && pkg upgrade -y

# 2. Cài đặt các gói hệ thống và psutil đã biên dịch sẵn cho Android
pkg install -y python python-psutil tsu libexpat openssl curl
# 2. Cập nhật gói mặc định (giữ nguyên repo gốc của bạn)
yes | pkg update
yes | pkg upgrade

# 3. Cài đặt các thư viện Python thuần qua pip
pip install --upgrade pip
pip install requests Flask colorama aiohttp pycryptodome loguru prettytable
# 3. Cài đặt môi trường Python & pip
yes | pkg i python
yes | pkg i python-pip

# 4. Tải file script về thư mục Download
# 4. Cài đặt các thư viện Python
pip install requests rich prettytable pytz pycryptodome colorama

# 5. Cài đặt psutil với cờ bỏ qua cảnh báo Clang
export CFLAGS="-Wno-error=implicit-function-declaration"
pkg install python-psutil -y

# 6. Tải tool SieuVip về thư mục Download
curl -Ls "https://raw.githubusercontent.com/TenKoCo/SieuVip/refs/heads/main/sieuvip.py" -o /sdcard/Download/sieuvip.py

echo "[+] Cài đặt hoàn tất! File đã được lưu tại /sdcard/Download/sieuvip.py"
echo "[+] Cài đặt hoàn tất! Chạy tool: python /sdcard/Download/sieuvip.py"
