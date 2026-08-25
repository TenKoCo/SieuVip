#!/data/data/com.termux/files/usr/bin/python
"""
SieuVip Roblox Engine & Cookie Login Dashboard for Android Root.
Tích hợp kiểm tra Cookie, xin cấp Auth Ticket và mở game tự động.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Sequence, Tuple


DEFAULT_CONFIG_PATH = Path("/sdcard/Download/sieuvip_config.json")
DEFAULT_COOKIE_PATH = Path("/sdcard/Download/cookie.txt")
DEFAULT_BLOX_FRUITS_PLACE_ID = "2753915549"
PACKAGE_RE = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+$")


class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    CYAN = "\033[36m"
    MAGENTA = "\033[35m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"


@dataclasses.dataclass
class TargetConfig:
    package: str
    link: str = DEFAULT_BLOX_FRUITS_PLACE_ID
    enabled: bool = True

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> TargetConfig:
        return cls(
            package=str(raw.get("package", "")).strip(),
            link=str(raw.get("link", DEFAULT_BLOX_FRUITS_PLACE_ID)).strip(),
            enabled=bool(raw.get("enabled", True)),
        )


@dataclasses.dataclass
class RejoinConfig:
    targets: List[TargetConfig]
    interval_seconds: int = 900

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> RejoinConfig:
        targets_raw = raw.get("targets", [])
        targets = [TargetConfig.from_dict(t) for t in targets_raw if isinstance(t, dict)]
        return cls(
            targets=targets,
            interval_seconds=int(raw.get("interval_seconds", 900)),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "interval_seconds": self.interval_seconds,
            "targets": [dataclasses.asdict(t) for t in self.targets],
        }


def load_config(path: Path) -> RejoinConfig:
    if not path.exists():
        return RejoinConfig(targets=[])
    try:
        with open(path, "r", encoding="utf-8") as f:
            return RejoinConfig.from_dict(json.load(f))
    except Exception:
        return RejoinConfig(targets=[])


def save_config(path: Path, config: RejoinConfig) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(config.to_dict(), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Lỗi lưu cấu hình: {e}")


class RobloxCookieAuth:
    """Xử lý xác thực Cookie và tạo vé Auth Ticket."""

    USER_AGENT = "Roblox/Android (Android 10; Mobile; Build/10.0)"

    @classmethod
    def clean_cookie(cls, raw_cookie: str) -> str:
        cookie = str(raw_cookie).strip().strip("'\"")
        header_match = re.search(r"(?i)(?:^|[;\s])\.ROBLOSECURITY\s*=\s*([^;\s]+)", cookie)
        if header_match:
            cookie = header_match.group(1).strip()
        if "_|WARNING:" in cookie:
            parts = cookie.split("|_")
            cookie = parts[-1].strip() if len(parts) > 1 else cookie
        return re.sub(r"\\([_.|\-])", r"\1", cookie)

    @classmethod
    def validate_and_get_ticket(cls, cookie: str) -> Tuple[bool, Optional[str], Optional[str], str]:
        """
        Kiểm tra Cookie và sinh Auth Ticket qua cURL.
        Trả về: (Trạng thái, Tên tài khoản, Auth Ticket, Thông báo).
        """
        token = cls.clean_cookie(cookie)
        if len(token) < 50:
            return False, None, None, "Chuỗi Cookie không hợp lệ hoặc quá ngắn"

        # 1. Kiểm tra tài khoản
        cmd_user = [
            "curl", "-s", "-m", "10",
            "https://users.roblox.com/v1/users/authenticated",
            "-H", f"Cookie: .ROBLOSECURITY={token}",
            "-H", f"User-Agent: {cls.USER_AGENT}",
            "-H", "Accept: application/json"
        ]
        res_user = subprocess.run(cmd_user, capture_output=True, text=True)
        if res_user.returncode != 0 or not res_user.stdout:
            return False, None, None, "Không thể kết nối tới API Roblox"

        try:
            data = json.loads(res_user.stdout)
            if "name" not in data:
                return False, None, None, "Cookie đã hết hạn hoặc không hợp lệ"
            user_name = data["name"]
        except Exception:
            return False, None, None, "Không đọc được dữ liệu phản hồi từ máy chủ"

        # 2. Lấy x-csrf-token
        cmd_csrf = [
            "curl", "-s", "-i", "-m", "10",
            "-X", "POST", "https://auth.roblox.com/v1/authentication-ticket/",
            "-H", f"Cookie: .ROBLOSECURITY={token}",
            "-H", f"User-Agent: {cls.USER_AGENT}",
            "-H", "Origin: https://www.roblox.com",
            "-H", "Referer: https://www.roblox.com/",
            "-H", "Content-Length: 0"
        ]
        res_csrf = subprocess.run(cmd_csrf, capture_output=True, text=True)
        csrf_match = re.search(r"(?i)x-csrf-token:\s*([^\r\n]+)", res_csrf.stdout)
        if not csrf_match:
            return False, user_name, None, "Không lấy được mã x-csrf-token"

        csrf_token = csrf_match.group(1).strip()

        # 3. Yêu cầu cấp Authentication Ticket
        cmd_ticket = [
            "curl", "-s", "-i", "-m", "10",
            "-X", "POST", "https://auth.roblox.com/v1/authentication-ticket/",
            "-H", f"Cookie: .ROBLOSECURITY={token}",
            "-H", f"x-csrf-token: {csrf_token}",
            "-H", "RBXAuthenticationNegotiation: 1",
            "-H", f"User-Agent: {cls.USER_AGENT}",
            "-H", "Origin: https://www.roblox.com",
            "-H", "Referer: https://www.roblox.com/",
            "-H", "Content-Type: application/json",
            "-d", "{}"
        ]
        res_ticket = subprocess.run(cmd_ticket, capture_output=True, text=True)
        ticket_match = re.search(r"(?i)rbx-authentication-ticket:\s*([^\r\n]+)", res_ticket.stdout)
        ticket = ticket_match.group(1).strip() if ticket_match else None

        if ticket:
            return True, user_name, ticket, "Xác thực và cấp Ticket thành công"
        return False, user_name, None, "Máy chủ từ chối cấp Ticket"


class AndroidRootController:
    """Quản lý các thao tác điều khiển ứng dụng qua quyền Root."""

    @staticmethod
    def run_cmd(cmd: str, timeout: float = 15.0) -> Tuple[bool, str]:
        try:
            res = subprocess.run(
                ["su", "-c", cmd],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False
            )
            out = (res.stdout + "\n" + res.stderr).strip()
            return res.returncode == 0, out
        except Exception as e:
            return False, str(e)

    @classmethod
    def list_packages(cls) -> List[str]:
        ok, out = cls.run_cmd("pm list packages")
        if not ok:
            return []
        pkgs = []
        for line in out.splitlines():
            if line.startswith("package:"):
                p = line.split(":", 1)[1].strip()
                if PACKAGE_RE.fullmatch(p):
                    pkgs.append(p)
        return sorted(pkgs)

    @classmethod
    def force_stop(cls, pkg: str) -> bool:
        ok, _ = cls.run_cmd(f"am force-stop {pkg}")
        return ok

    @classmethod
    def inject_session_files(cls, pkg: str, cookie: str) -> None:
        """Ghi đè session vào SharedPreferences của package qua Root."""
        norm = RobloxCookieAuth.clean_cookie(cookie)
        full_token = f"_|WARNING:-DO-NOT-SHARE-THIS.--Sharing-this-will-allow-someone-to-log-into-your-account-and-rob-your-robox.--|_{norm}"
        
        script = f"""
pkg="{pkg}"
app_dir="/data/data/$pkg"
[ ! -d "$app_dir" ] && app_dir="/data/user/0/$pkg"

if [ -d "$app_dir" ]; then
    owner=$(stat -c '%u:%g' "$app_dir" 2>/dev/null || echo "10000:10000")
    mkdir -p "$app_dir/shared_prefs"
    
    for xml in "com.roblox.client_preferences.xml" "${{pkg}}_preferences.xml"; do
        cat << 'EOF' > "$app_dir/shared_prefs/$xml"
<?xml version='1.0' encoding='utf-8' standalone='yes' ?>
<map>
    <string name="RBXSession">{full_token}</string>
    <string name="RBXSessionToken">{full_token}</string>
    <string name=".ROBLOSECURITY">{norm}</string>
</map>
EOF
        chmod 660 "$app_dir/shared_prefs/$xml"
    done
    chown -R "$owner" "$app_dir/shared_prefs"
    chmod 771 "$app_dir/shared_prefs"
fi
"""
        cls.run_cmd(script)

    @classmethod
    def start_game(cls, pkg: str, link_or_id: str, ticket: Optional[str] = None) -> bool:
        """Khởi chạy ứng dụng vào game qua Deep Link kèm Auth Ticket."""
        cls.force_stop(pkg)
        time.sleep(0.5)

        raw = link_or_id.strip()
        params: Dict[str, str] = {}
        if raw.isdigit():
            params["placeId"] = raw
        else:
            if "placeId=" in raw:
                parsed = urllib.parse.urlparse(raw)
                q = urllib.parse.parse_qs(parsed.query)
                params["placeId"] = q.get("placeId", [DEFAULT_BLOX_FRUITS_PLACE_ID])[0]
                if "linkCode" in q:
                    params["linkCode"] = q["linkCode"][0]
            else:
                params["placeId"] = DEFAULT_BLOX_FRUITS_PLACE_ID

        if ticket:
            params["ticket"] = ticket

        deep_link = f"roblox://experiences/start?{urllib.parse.urlencode(params)}"
        cmd = f"am start -W -a android.intent.action.VIEW -d '{deep_link}' -p {pkg}"
        ok, out = cls.run_cmd(cmd)
        return ok and "error" not in out.lower()


def load_cookies(path: Path) -> List[str]:
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip() and not line.startswith("#")]
    except Exception:
        return []


def menu_login_with_cookies(config: RejoinConfig) -> None:
    if not config.targets:
        print(f"{Colors.YELLOW}[!] Hãy chọn Package ở mục 3 trước.{Colors.RESET}")
        return

    cookies = load_cookies(DEFAULT_COOKIE_PATH)
    if not cookies:
        print(f"{Colors.RED}[!] Không tìm thấy cookie trong file: {DEFAULT_COOKIE_PATH}{Colors.RESET}")
        return

    print(f"\n{Colors.CYAN}[*] Bắt đầu xác thực và đăng nhập cookie cho {len(config.targets)} package...{Colors.RESET}")
    for idx, target in enumerate(config.targets):
        cookie = cookies[idx % len(cookies)]
        print(f"\n[*] Xử lý [{target.package}]...")

        ok, user, ticket, msg = RobloxCookieAuth.validate_and_get_ticket(cookie)
        if not ok:
            print(f"{Colors.RED}[✗] {msg}{Colors.RESET}")
            continue

        print(f"{Colors.GREEN}[✓] Tài khoản: {user} | Lấy Ticket thành công.{Colors.RESET}")
        print(f"[*] Nạp session và kích hoạt ứng dụng...")
        
        AndroidRootController.inject_session_files(target.package, cookie)
        launched = AndroidRootController.start_game(target.package, target.link, ticket=ticket)
        
        if launched:
            print(f"{Colors.GREEN}[+] Đã mở game cho {target.package}!{Colors.RESET}")
        else:
            print(f"{Colors.RED}[-] Mở game thất bại.{Colors.RESET}")
        time.sleep(2.0)


def menu_choose_packages(config: RejoinConfig, path: Path) -> None:
    print(f"\n{Colors.CYAN}[*] Đang quét danh sách package Roblox...{Colors.RESET}")
    all_pkgs = AndroidRootController.list_packages()
    if not all_pkgs:
        print(f"{Colors.RED}[!] Không lấy được danh sách package qua Root.{Colors.RESET}")
        return

    prefix = input("Nhập tiền tố package (ví dụ: com hoặc com.roblox): ").strip()
    selected = [p for p in all_pkgs if p.lower().startswith(prefix.lower())]
    if not selected:
        print(f"{Colors.YELLOW}[!] Không tìm thấy package khớp với: {prefix}{Colors.RESET}")
        return

    config.targets = [TargetConfig(package=p, link=DEFAULT_BLOX_FRUITS_PLACE_ID) for p in selected]
    save_config(path, config)
    print(f"{Colors.GREEN}[+] Đã lưu {len(config.targets)} package thành công!{Colors.RESET}")


def menu_set_link(config: RejoinConfig, path: Path) -> None:
    if not config.targets:
        print(f"{Colors.YELLOW}[!] Hãy chọn Package ở mục 3 trước.{Colors.RESET}")
        return
    raw = input(f"Nhập Place ID / Server VIP [Enter = {DEFAULT_BLOX_FRUITS_PLACE_ID}]: ").strip()
    link = raw or DEFAULT_BLOX_FRUITS_PLACE_ID
    for t in config.targets:
        t.link = link
    save_config(path, config)
    print(f"{Colors.GREEN}[+] Đã cập nhật link cho toàn bộ package!{Colors.RESET}")


def menu_launch_all(config: RejoinConfig) -> None:
    if not config.targets:
        print(f"{Colors.YELLOW}[!] Chưa có package nào được cấu hình.{Colors.RESET}")
        return
    for t in config.targets:
        if t.enabled:
            print(f"[*] Đang mở {t.package}...")
            AndroidRootController.start_game(t.package, t.link)
            time.sleep(2.0)
    print(f"{Colors.GREEN}[+] Hoàn tất mở tất cả ứng dụng!{Colors.RESET}")


def interactive_dashboard():
    config = load_config(DEFAULT_CONFIG_PATH)

    while True:
        print("\033[2J\033[H", end="")
        print(f"{' '*18}⚡ {Colors.CYAN}{Colors.BOLD}SieuVipPro Dashboard{Colors.RESET}\n")
        print("┌──────┬────────────────────────────────────────────────────────┐")
        print(f"│ {Colors.MAGENTA}   1{Colors.RESET}  │ {Colors.CYAN}Start Auto Rejoin Engine (Tự động mở lại 24/7)         {Colors.RESET}│")
        print(f"│ {Colors.MAGENTA}   2{Colors.RESET}  │ {Colors.CYAN}Nhập Game ID / Link Server VIP                         {Colors.RESET}│")
        print(f"│ {Colors.MAGENTA}   3{Colors.RESET}  │ {Colors.CYAN}Chọn Package Roblox để chạy                            {Colors.RESET}│")
        print(f"│ {Colors.MAGENTA}   4{Colors.RESET}  │ {Colors.CYAN}Mở tất cả App lên nền (Khởi chạy ngay)                 {Colors.RESET}│")
        print(f"│ {Colors.MAGENTA}   5{Colors.RESET}  │ {Colors.GREEN}Đăng nhập Cookie & Mở Game qua Vé Auth Ticket          {Colors.RESET}│")
        print(f"│ {Colors.MAGENTA}   0{Colors.RESET}  │ {Colors.RED}Thoát Hệ Thống                                         {Colors.RESET}│")
        print("└──────┴────────────────────────────────────────────────────────┘")

        choice = input(f"\n{Colors.MAGENTA}Execute -> {Colors.RESET}").strip()
        if choice == "0":
            break
        elif choice == "1":
            print(f"\n{Colors.GREEN}[*] Engine đang chạy Rejoin... Nhấn Ctrl+C để dừng.{Colors.RESET}")
            try:
                while True:
                    menu_launch_all(config)
                    time.sleep(config.interval_seconds)
            except KeyboardInterrupt:
                print("\n[*] Đã dừng Engine.")
        elif choice == "2":
            menu_set_link(config, DEFAULT_CONFIG_PATH)
        elif choice == "3":
            menu_choose_packages(config, DEFAULT_CONFIG_PATH)
        elif choice == "4":
            menu_launch_all(config)
        elif choice == "5":
            menu_login_with_cookies(config)

        input(f"\n{Colors.YELLOW}Nhấn Enter để quay lại menu...{Colors.RESET}")


if __name__ == "__main__":
    interactive_dashboard()
