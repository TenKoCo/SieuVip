#!/data/data/com.termux/files/usr/bin/python
"""
SieuVip Roblox Rejoin Engine & Cookie Auth System for Android Root.
Ho tro tuy chon package linh hoat (nhap tay hoac quet tu dong).
"""

from __future__ import annotations

import argparse
import dataclasses
import html
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import random
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import fcntl
except ImportError:
    fcntl = None

APP_NAME = "sieuvip-rejoin"
DEFAULT_CONFIG_PATH = Path("/sdcard/Download/sieuvip_config.json")
DEFAULT_COOKIE_SOURCE_PATH = Path("/sdcard/Download/cookie.txt")
DEFAULT_LOG_PATH = Path("/sdcard/Download/sieuvip_rejoin.log")
DEFAULT_LOCK_PATH = Path("/sdcard/Download/sieuvip_rejoin.lock")
DEFAULT_BLOX_FRUITS_PLACE_ID = "2753915549"
HEALTH_POLL_SECONDS = 5.0
PACKAGE_RE = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+$")
USER_AGENT = "Roblox/Android (Android 10; Mobile; Build/10.0)"


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
    bounds: Optional[str] = None

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> TargetConfig:
        return cls(
            package=str(raw.get("package", "")).strip(),
            link=str(raw.get("link", DEFAULT_BLOX_FRUITS_PLACE_ID)).strip(),
            enabled=bool(raw.get("enabled", True)),
            bounds=raw.get("bounds"),
        )


@dataclasses.dataclass
class RejoinConfig:
    targets: List[TargetConfig]
    interval_seconds: int = 900
    warmup_seconds: float = 2.5
    between_apps_seconds: float = 1.0
    retries: int = 2
    retry_backoff_seconds: float = 2.0
    command_timeout_seconds: float = 25.0
    wake_lock: bool = True
    freeform: bool = False
    auto_arrange: bool = False
    auto_login_cookies: bool = True
    health_check_method: str = "online"
    health_check_timeout_seconds: int = 180

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> RejoinConfig:
        targets_raw = raw.get("targets", [])
        targets = [TargetConfig.from_dict(t) for t in targets_raw if isinstance(t, dict)]
        return cls(
            targets=targets,
            interval_seconds=int(raw.get("interval_seconds", 900)),
            warmup_seconds=float(raw.get("warmup_seconds", 2.5)),
            between_apps_seconds=float(raw.get("between_apps_seconds", 1.0)),
            retries=int(raw.get("retries", 2)),
            retry_backoff_seconds=float(raw.get("retry_backoff_seconds", 2.0)),
            command_timeout_seconds=float(raw.get("command_timeout_seconds", 25.0)),
            wake_lock=bool(raw.get("wake_lock", True)),
            freeform=bool(raw.get("freeform", False)),
            auto_arrange=bool(raw.get("auto_arrange", False)),
            auto_login_cookies=bool(raw.get("auto_login_cookies", True)),
            health_check_method=str(raw.get("health_check_method", "online")).lower(),
            health_check_timeout_seconds=int(raw.get("health_check_timeout_seconds", 180)),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "interval_seconds": self.interval_seconds,
            "warmup_seconds": self.warmup_seconds,
            "between_apps_seconds": self.between_apps_seconds,
            "retries": self.retries,
            "retry_backoff_seconds": self.retry_backoff_seconds,
            "command_timeout_seconds": self.command_timeout_seconds,
            "wake_lock": self.wake_lock,
            "freeform": self.freeform,
            "auto_arrange": self.auto_arrange,
            "auto_login_cookies": self.auto_login_cookies,
            "health_check_method": self.health_check_method,
            "health_check_timeout_seconds": self.health_check_timeout_seconds,
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
        print(f"Loi luu config: {e}")


class RobloxAuthSystem:
    @staticmethod
    def clean_cookie(raw: str) -> str:
        cookie = str(raw).strip().strip("'\"")
        match = re.search(r"(?i)\.ROBLOSECURITY\s*=\s*([^;\s]+)", cookie)
        if match:
            cookie = match.group(1).strip()
        if "_|WARNING:" in cookie:
            parts = cookie.split("|_")
            cookie = parts[-1].strip() if len(parts) > 1 else cookie
        return re.sub(r"\\([_.|\-])", r"\1", cookie)

    @classmethod
    def get_auth_ticket(cls, cookie: str) -> Tuple[bool, Optional[str], Optional[str], str]:
        token = cls.clean_cookie(cookie)
        if len(token) < 50:
            return False, None, None, "Cookie qua ngan hoac khong hop le"

        # 1. Kiem tra user
        cmd_user = [
            "curl", "-s", "-m", "10",
            "https://users.roblox.com/v1/users/authenticated",
            "-H", f"Cookie: .ROBLOSECURITY={token}",
            "-H", f"User-Agent: {USER_AGENT}",
            "-H", "Accept: application/json"
        ]
        res_user = subprocess.run(cmd_user, capture_output=True, text=True)
        try:
            user_data = json.loads(res_user.stdout)
            if "name" not in user_data:
                return False, None, None, "Cookie da het han"
            username = user_data["name"]
        except Exception:
            return False, None, None, "Khong ket noi duoc API Roblox"

        # 2. Lay CSRF token
        cmd_csrf = [
            "curl", "-s", "-i", "-m", "10",
            "-X", "POST", "https://auth.roblox.com/v1/authentication-ticket/",
            "-H", f"Cookie: .ROBLOSECURITY={token}",
            "-H", f"User-Agent: {USER_AGENT}",
            "-H", "Origin: https://www.roblox.com",
            "-H", "Referer: https://www.roblox.com/",
            "-H", "Content-Length: 0"
        ]
        res_csrf = subprocess.run(cmd_csrf, capture_output=True, text=True)
        csrf_match = re.search(r"(?i)x-csrf-token:\s*([^\r\n]+)", res_csrf.stdout)
        if not csrf_match:
            return False, username, None, "Khong lay duoc x-csrf-token"
        csrf_token = csrf_match.group(1).strip()

        # 3. Yeu cau Auth Ticket
        cmd_ticket = [
            "curl", "-s", "-i", "-m", "10",
            "-X", "POST", "https://auth.roblox.com/v1/authentication-ticket/",
            "-H", f"Cookie: .ROBLOSECURITY={token}",
            "-H", f"x-csrf-token: {csrf_token}",
            "-H", "RBXAuthenticationNegotiation: 1",
            "-H", f"User-Agent: {USER_AGENT}",
            "-H", "Origin: https://www.roblox.com",
            "-H", "Referer: https://www.roblox.com/",
            "-H", "Content-Type: application/json",
            "-d", "{}"
        ]
        res_ticket = subprocess.run(cmd_ticket, capture_output=True, text=True)
        ticket_match = re.search(r"(?i)rbx-authentication-ticket:\s*([^\r\n]+)", res_ticket.stdout)
        if ticket_match:
            return True, username, ticket_match.group(1).strip(), "Cap Ticket thanh cong"
        return False, username, None, "Roblox tu choi cap ticket"


class RootController:
    @staticmethod
    def run(cmd: str, timeout: float = 15.0) -> Tuple[bool, str]:
        try:
            res = subprocess.run(
                ["su", "-c", cmd],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False
            )
            return res.returncode == 0, (res.stdout + "\n" + res.stderr).strip()
        except Exception as e:
            return False, str(e)

    @classmethod
    def list_installed_packages(cls) -> List[str]:
        ok, out = cls.run("pm list packages")
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
    def force_stop(cls, package: str) -> bool:
        ok, _ = cls.run(f"am force-stop {package}")
        return ok

    @classmethod
    def inject_storage(cls, package: str, cookie: str) -> None:
        token = RobloxAuthSystem.clean_cookie(cookie)
        full_session = f"_|WARNING:-DO-NOT-SHARE-THIS.--Sharing-this-will-allow-someone-to-log-into-your-account-and-rob-your-robox.--|_{token}"
        script = f"""
pkg="{package}"
app_dir="/data/data/$pkg"
[ ! -d "$app_dir" ] && app_dir="/data/user/0/$pkg"
if [ -d "$app_dir" ]; then
    owner=$(stat -c '%u:%g' "$app_dir" 2>/dev/null || echo "10000:10000")
    mkdir -p "$app_dir/shared_prefs"
    for xml in "com.roblox.client_preferences.xml" "${{pkg}}_preferences.xml"; do
        cat << 'EOF_XML' > "$app_dir/shared_prefs/$xml"
<?xml version='1.0' encoding='utf-8' standalone='yes' ?>
<map>
    <string name="RBXSession">{full_session}</string>
    <string name="RBXSessionToken">{full_session}</string>
    <string name=".ROBLOSECURITY">{token}</string>
</map>
EOF_XML
        chmod 660 "$app_dir/shared_prefs/$xml"
    done
    chown -R "$owner" "$app_dir/shared_prefs"
    chmod 771 "$app_dir/shared_prefs"
fi
"""
        cls.run(script)

    @classmethod
    def launch(cls, package: str, link_or_id: str, ticket: Optional[str] = None) -> bool:
        cls.force_stop(package)
        time.sleep(0.5)

        raw = link_or_id.strip()
        params: Dict[str, str] = {}
        if raw.isdigit():
            params["placeId"] = raw
        elif "placeId=" in raw:
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
        cmd = f"am start -W -a android.intent.action.VIEW -d '{deep_link}' -p {package}"
        ok, out = cls.run(cmd)
        return ok and "error" not in out.lower()


def load_cookie_list(path: Path) -> List[str]:
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip() and not line.startswith("#")]
    except Exception:
        return []


def menu_login_cookie_action(config: RejoinConfig) -> None:
    if not config.targets:
        print(f"{Colors.YELLOW}[!] Hay chon Package o muc 3 truoc.{Colors.RESET}")
        return

    cookies = load_cookie_list(DEFAULT_COOKIE_SOURCE_PATH)
    if not cookies:
        print(f"{Colors.RED}[!] File {DEFAULT_COOKIE_SOURCE_PATH} dang rong.{Colors.RESET}")
        return

    print(f"\n{Colors.CYAN}[*] Dang thuc hien dang nhap Cookie cho {len(config.targets)} package...{Colors.RESET}")
    for idx, target in enumerate(config.targets):
        raw_cookie = cookies[idx % len(cookies)]
        print(f"\n[*] Xu ly: {Colors.BOLD}{target.package}{Colors.RESET}")

        ok, user, ticket, msg = RobloxAuthSystem.get_auth_ticket(raw_cookie)
        if not ok:
            print(f"{Colors.RED}[-] {msg}{Colors.RESET}")
            continue

        print(f"{Colors.GREEN}[+] Tai khoan: {user} | Da lay Auth Ticket.{Colors.RESET}")
        RootController.inject_storage(target.package, raw_cookie)
        
        launched = RootController.launch(target.package, target.link, ticket=ticket)
        if launched:
            print(f"{Colors.GREEN}[+] Da khoi chay game thanh cong cho {target.package}!{Colors.RESET}")
        else:
            print(f"{Colors.RED}[-] Khong the mo app qua Intent.{Colors.RESET}")
        time.sleep(2.0)


def menu_choose_packages(config: RejoinConfig, path: Path) -> None:
    print("\n" + "=" * 50)
    print(f"{Colors.CYAN}TUY CHON PACKAGE ROBLOX{Colors.RESET}")
    print("1. Nhap thu cong ten Package (Vi du: free.nokaA)")
    print("2. Quet tu dong danh sach Package tren may")
    print("=" * 50)
    
    sub = input("Chon phuong thuc [1/2]: ").strip()
    if sub == "1":
        pkg_input = input("\nNhap ten package: ").strip()
        if not PACKAGE_RE.fullmatch(pkg_input):
            print(f"{Colors.RED}[!] Package khong hop le.{Colors.RESET}")
            return
        config.targets = [TargetConfig(package=pkg_input, link=DEFAULT_BLOX_FRUITS_PLACE_ID)]
    else:
        all_pkgs = RootController.list_installed_packages()
        prefix = input("\nNhap tien to package can loc (Enter = com): ").strip() or "com"
        selected = [p for p in all_pkgs if p.lower().startswith(prefix.lower())]
        if not selected:
            print(f"{Colors.YELLOW}[!] Khong tim thay package nao voi tien to: {prefix}{Colors.RESET}")
            return
        config.targets = [TargetConfig(package=p, link=DEFAULT_BLOX_FRUITS_PLACE_ID) for p in selected]

    save_config(path, config)
    print(f"{Colors.GREEN}[+] Da luu {len(config.targets)} package thanh cong!{Colors.RESET}")


def menu_set_link(config: RejoinConfig, path: Path) -> None:
    if not config.targets:
        print(f"{Colors.YELLOW}[!] Hay chon Package o muc 3 truoc.{Colors.RESET}")
        return
    raw = input(f"\nNhap Place ID / Link Server VIP [Enter = Blox Fruits]: ").strip()
    link = raw or DEFAULT_BLOX_FRUITS_PLACE_ID
    for t in config.targets:
        t.link = link
    save_config(path, config)
    print(f"{Colors.GREEN}[+] Da cap nhat link cho tat ca package!{Colors.RESET}")


def interactive_dashboard():
    config = load_config(DEFAULT_CONFIG_PATH)

    while True:
        print("\033[2J\033[H", end="")
        print(f"{' '*18}⚡ {Colors.CYAN}{Colors.BOLD}SieuVipPro Dashboard{Colors.RESET}\n")
        print("┌──────┬────────────────────────────────────────────────────────┐")
        print(f"│ {Colors.MAGENTA}   1{Colors.RESET}  │ {Colors.CYAN}Start Auto Rejoin Engine (Chay tu dong 24/7)           {Colors.RESET}│")
        print(f"│ {Colors.MAGENTA}   2{Colors.RESET}  │ {Colors.CYAN}Nhap Game ID / Link Server VIP                         {Colors.RESET}│")
        print(f"│ {Colors.MAGENTA}   3{Colors.RESET}  │ {Colors.CYAN}Tuy chon Package (Nhap tay free.nokaA / Quet list)     {Colors.RESET}│")
        print(f"│ {Colors.MAGENTA}   4{Colors.RESET}  │ {Colors.CYAN}Mo tat ca App len nen (Khoi chay ngay)                 {Colors.RESET}│")
        print(f"│ {Colors.MAGENTA}   5{Colors.RESET}  │ {Colors.GREEN}Dang nhap Cookie (Lay Auth Ticket & Vao Game)          {Colors.RESET}│")
        print(f"│ {Colors.MAGENTA}   0{Colors.RESET}  │ {Colors.RED}Thoat He Thong                                         {Colors.RESET}│")
        print("└──────┴────────────────────────────────────────────────────────┘")

        choice = input(f"\n{Colors.MAGENTA}Execute -> {Colors.RESET}").strip()
        if choice == "0":
            break
        elif choice == "1":
            print(f"\n{Colors.GREEN}[*] Engine Auto Rejoin dang chay... Nhan Ctrl+C de dung.{Colors.RESET}")
            try:
                while True:
                    menu_login_cookie_action(config)
                    time.sleep(config.interval_seconds)
            except KeyboardInterrupt:
                print("\n[*] Da dung Engine.")
        elif choice == "2":
            menu_set_link(config, DEFAULT_CONFIG_PATH)
        elif choice == "3":
            menu_choose_packages(config, DEFAULT_CONFIG_PATH)
        elif choice == "4":
            for t in config.targets:
                RootController.launch(t.package, t.link)
        elif choice == "5":
            menu_login_cookie_action(config)

        input(f"\n{Colors.YELLOW}Nhan Enter de quay lai menu...{Colors.RESET}")


if __name__ == "__main__":
    interactive_dashboard()
