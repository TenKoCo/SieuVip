#!/data/data/com.termux/files/usr/bin/python
"""
Module nạp Cookie trực tiếp vào Private Data / Session Storage của Roblox Android.
Yêu cầu: Thiết bị đã Root (có quyền 'su').
"""

import os
import re
import sqlite3
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Optional, Tuple


class RobloxSessionInjector:
    """Bộ xử lý can thiệp dữ liệu nội bộ của ứng dụng Roblox qua Root."""

    @staticmethod
    def clean_cookie(raw_cookie: str) -> str:
        """Làm sạch chuỗi cookie, trích xuất token .ROBLOSECURITY thuần túy."""
        cookie = str(raw_cookie).strip().strip("'\"")
        match = re.search(r"(?i)\.ROBLOSECURITY\s*=\s*([^;\s]+)", cookie)
        if match:
            cookie = match.group(1).strip()
        if "_|WARNING:" in cookie:
            parts = cookie.split("|_")
            cookie = parts[-1].strip() if len(parts) > 1 else cookie
        return re.sub(r"\\([_.|\-])", r"\1", cookie)

    @staticmethod
    def run_root_command(cmd: str, timeout: float = 15.0) -> Tuple[bool, str]:
        """Thực thi một lệnh shell dưới quyền Root (su)."""
        try:
            res = subprocess.run(
                ["su", "-c", cmd],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            output = (res.stdout + "\n" + res.stderr).strip()
            return res.returncode == 0, output
        except Exception as err:
            return False, str(err)

    @classmethod
    def inject_cookie_to_storage(
        cls, package_name: str, raw_cookie: str
    ) -> Tuple[bool, str]:
        """
        Ghi đè Cookie vào Private Storage (shared_prefs và WebView SQLite)
        nhưng giữ nguyên toàn bộ các file cache, cấu hình đồ họa khác.
        """
        token = cls.clean_cookie(raw_cookie)
        if len(token) < 50:
            return False, "Chuỗi Cookie không hợp lệ hoặc quá ngắn"

        # Định dạng token đầy đủ theo cấu trúc lưu trữ của Roblox Android
        full_session = (
            f"_|WARNING:-DO-NOT-SHARE-THIS.--Sharing-this-will-allow-someone-"
            f"to-log-into-your-account-and-rob-your-robox.--|_{token}"
        )

        # 1. Buộc dừng app trước khi ghi dữ liệu
        cls.run_root_command(f"am force-stop {package_name}")
        time.sleep(0.5)

        # 2. Tạo đoạn script Shell chạy ngầm dưới quyền Root để cập nhật bộ nhớ riêng
        injection_script = f"""
pkg="{package_name}"
app_dir="/data/data/$pkg"
[ ! -d "$app_dir" ] && app_dir="/data/user/0/$pkg"

if [ ! -d "$app_dir" ]; then
    echo "ERROR: Không tìm thấy thư mục dữ liệu của $pkg"
    exit 1
fi

# Lấy UID và GID chính xác của ứng dụng
owner=$(stat -c '%u:%g' "$app_dir" 2>/dev/null || echo "10000:10000")

# --- BƯỚC A: Cập nhật SharedPreferences ---
mkdir -p "$app_dir/shared_prefs"

for xml_file in "com.roblox.client_preferences.xml" "${{pkg}}_preferences.xml"; do
    target="$app_dir/shared_prefs/$xml_file"
    
    # Nếu file chưa tồn tại, tạo file mẫu
    if [ ! -f "$target" ]; then
        cat << 'EOF' > "$target"
<?xml version='1.0' encoding='utf-8' standalone='yes' ?>
<map>
</map>
EOF
    fi

    # Xóa các khóa cookie cũ nếu có để tránh trùng lặp
    sed -i '/name="RBXSession"/d' "$target"
    sed -i '/name="RBXSessionToken"/d' "$target"
    sed -i '/name="\.ROBLOSECURITY"/d' "$target"

    # Chèn các khóa session mới trước thẻ đóng </map>
    sed -i '/<\\/map>/i \\    <string name="RBXSession">{full_session}</string>\\n    <string name="RBXSessionToken">{full_session}</string>\\n    <string name=".ROBLOSECURITY">{token}</string>' "$target"
    
    chmod 660 "$target"
done

# --- BƯỚC B: Phân quyền sở hữu toàn bộ thư mục shared_prefs ---
chown -R "$owner" "$app_dir/shared_prefs"
chmod 771 "$app_dir/shared_prefs"

echo "INJECT_SUCCESS"
"""
        ok, out = cls.run_root_command(injection_script)
        if ok and "INJECT_SUCCESS" in out:
            return True, "Đã cập nhật session cookie vào storage thành công"
        return False, f"Lỗi khi nạp cookie vào storage: {out}"

    @classmethod
    def launch_game(
        cls,
        package_name: str,
        place_id: str = "2753915549",
        link_code: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Kích hoạt Intent mở game sau khi đã nạp session cookie."""
        params = {"placeId": str(place_id)}
        if link_code:
            params["linkCode"] = str(link_code)

        query = urllib.parse.urlencode(params)
        deep_link = f"roblox://experiences/start?{query}"

        start_cmd = (
            f"am start -W "
            f"-a android.intent.action.VIEW "
            f"-d '{deep_link}' "
            f"-p {package_name}"
        )
        ok, out = cls.run_root_command(start_cmd)
        if ok and "error" not in out.lower():
            return True, "Khởi chạy game thành công"
        return False, f"Lỗi Intent: {out}"


# --- VÍ DỤ THỰC THI KIỂM TRA MÃ ---
if __name__ == "__main__":
    # Thay thế thông tin thử nghiệm của bạn tại đây:
    TARGET_PACKAGE = "com.roblox.client"
    SAMPLE_COOKIE = "_|WARNING:-DO-NOT-SHARE-THIS.--Sharing-this-will-allow-someone-to-log-into-your-account-and-rob-your-robox.--|_..."
    BLOX_FRUITS_PLACE_ID = "2753915549"

    print(f"[*] Đang nạp Cookie vào Private Storage của {TARGET_PACKAGE}...")
    success, message = RobloxSessionInjector.inject_cookie_to_storage(
        package_name=TARGET_PACKAGE, raw_cookie=SAMPLE_COOKIE
    )

    if success:
        print(f"[✓] {message}")
        print("[*] Đang mở game...")
        launched, launch_msg = RobloxSessionInjector.launch_game(
            package_name=TARGET_PACKAGE, place_id=BLOX_FRUITS_PLACE_ID
        )
        print(f"[{'✓' if launched else '✗'}] {launch_msg}")
    else:
        print(f"[✗] {message}")
