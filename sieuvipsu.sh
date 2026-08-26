#!/bin/bash
# ==========================================================
#  SieuVip Auto-Installer & Optimizer for Termux (Android)
# ==========================================================

# Dung khi gap loi nghiem trong
set -e

# Mau sac hien thi
CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${CYAN}==============================================${NC}"
echo -e "${CYAN}       🚀 CAI DAT SIEUVIP REJOIN ENGINE        ${NC}"
echo -e "${CYAN}==============================================${NC}"

# 1. Kiem tra va cap quyen Storage
echo -e "\n${YELLOW}[1/5] Kiem tra quyen truy cap bo nho...${NC}"
if [ ! -d "$HOME/storage" ] || [ ! -w "/sdcard" ]; then
    echo -e "${CYAN}[*] Dang yeu cau quyen Storage, vui long nhan CHO PHEP tren man hinh...${NC}"
    termux-setup-storage
    sleep 2
fi

# 2. Cap nhat he thong va cai dat cac goi can thiet trong 1 lenh (Nhanh hon)
echo -e "\n${YELLOW}[2/5] Cap nhat Package & Cai dat moi truong core...${NC}"
export DEBIAN_FRONTEND=noninteractive
pkg update -y -o Dpkg::Options::="--force-confold"
pkg install -y -o Dpkg::Options::="--force-confold" \
    python \
    python-pip \
    python-psutil \
    curl \
    tsu \
    termux-api

# 3. Cai dat cac thu vien Python
echo -e "\n${YELLOW}[3/5] Cai dat thu vien Python can thiet...${NC}"
pip install --no-cache-dir --upgrade pip
pip install --no-cache-dir requests rich prettytable pytz pycryptodome colorama

# 4. Tai file script ve /sdcard/Download
echo -e "\n${YELLOW}[4/5] Dang tai ban SieuVip moi nhat...${NC}"
mkdir -p /sdcard/Download
TARGET_SCRIPT="/sdcard/Download/sieuvip.py"
curl -Ls "https://raw.githubusercontent.com/TenKoCo/SieuVip/refs/heads/main/sieuvip.py" -o "$TARGET_SCRIPT"
chmod +x "$TARGET_SCRIPT"

# 5. Tao lenh tat chay nhanh 'sieuvip'
echo -e "\n${YELLOW}[5/5] Tao phim tat chay nhanh...${NC}"
ALIAS_PATH="$PREFIX/bin/sieuvip"
cat << 'EOF' > "$ALIAS_PATH"
#!/bin/bash
python /sdcard/Download/sieuvip.py "$@"
EOF
chmod +x "$ALIAS_PATH"

echo -e "\n${GREEN}==============================================${NC}"
echo -e "${GREEN}  ✅ CAI DAT HOAN TAT THANH CONG!${NC}"
echo -e "${GREEN}==============================================${NC}"
echo -e "${CYAN}[*] Cach 1:${NC} Go lenh ${YELLOW}sieuvip${NC} de mo tool ngay."
echo -e "${CYAN}[*] Cach 2:${NC} Go lenh ${YELLOW}python /sdcard/Download/sieuvip.py${NC}"
