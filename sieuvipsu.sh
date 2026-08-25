#!/bin/bash

# 1. Cấp quyền bộ nhớ và cập nhật toàn bộ kho gói Termux
termux-setup-storage
pkg update -y && pkg upgrade -y

# 2. Cài đặt các gói hệ thống và psutil đã biên dịch sẵn cho Android
pkg install -y python python-psutil tsu libexpat openssl curl

# 3. Cài đặt các thư viện Python thuần qua pip
pip install --upgrade pip
pip install requests Flask colorama aiohttp pycryptodome loguru prettytable

# 4. Tải file script về thư mục Download
curl -Ls "https://raw.githubusercontent.com/TenKoCo/SieuVip/refs/heads/main/sieuvip.py" -o /sdcard/Download/sieuvip.py

echo "[+] Cài đặt hoàn tất! File đã được lưu tại /sdcard/Download/sieuvip.py"
