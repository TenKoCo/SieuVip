#!/bin/bash
cd

# 1. Khởi tạo lại quyền truy cập bộ nhớ
if [ -e "/data/data/com.termux/files/home/storage" ]; then
    rm -rf /data/data/com.termux/files/home/storage
fi
termux-setup-storage

# 2. Cập nhật gói mặc định (giữ nguyên repo gốc của bạn)
yes | pkg update
yes | pkg upgrade

# 3. Cài đặt môi trường Python & pip
yes | pkg i python
yes | pkg i python-pip

# 4. Cài đặt các thư viện Python
pip install requests rich prettytable pytz pycryptodome colorama

# 5. Cài đặt psutil với cờ bỏ qua cảnh báo Clang
export CFLAGS="-Wno-error=implicit-function-declaration"
pkg install python-psutil -y

# 6. Tải tool SieuVip về thư mục Download
curl -Ls "https://raw.githubusercontent.com/TenKoCo/SieuVip/refs/heads/main/sieuvip.py" -o /sdcard/Download/sieuvip.py

echo "[+] Cài đặt hoàn tất! "
