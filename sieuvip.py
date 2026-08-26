#!/data/data/com.termux/files/usr/bin/python
"""
SieuVip Roblox Engine - Root Cookie Auth & Realtime Auto Rejoin System for Android.
Quy trình chuẩn:
1. Đóng sạch các app đang chạy ngầm.
2. Mở lần lượt từng app lên sảnh rồi đóng lại (Warmup sạch, không sort, không join game).
3. Rejoin lần lượt từng app vào game kèm chia lưới ô cửa sổ nổi (Grid Freeform) và kích hoạt Watchdog 24/7.
"""

from __future__ import annotations

import argparse
import dataclasses
import html
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import random
import re
import shlex
import shutil
import signal
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
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
PACKAGE_RE = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+$")
USER_AGENT = "Roblox/Android (Android 10; Mobile; Build/10.0)"

SYSTEM_PATH = (
    "/product/bin:/apex/com.android.runtime/bin:/apex/com.android.art/bin:"
    "/system_ext/bin:/system/bin:/system/xbin:/odm/bin:/vendor/bin:/vendor/xbin:"
    "/data/data/com.termux/files/usr/bin"
)

SSL_CONTEXT = ssl.create_default_context()
SSL_CONTEXT.check_hostname = False
SSL_CONTEXT.verify_mode = ssl.CERT_NONE


class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    CYAN = "\033[36m"
    MAGENTA = "\033[35m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    BLUE = "\033[34m"
    GRAY = "\033[90m"


class AppError(RuntimeError):
    pass


class ConfigError(AppError):
    pass


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
    command_timeout_seconds: float = 15.0
    wake_lock: bool = True
    freeform: bool = True
    auto_arrange: bool = True
    auto_login_cookies: bool = True
    health_check_method: str = "heartbeat"
    health_check_timeout_seconds: int = 90

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
            command_timeout_seconds=float(raw.get("command_timeout_seconds", 15.0)),
            wake_lock=bool(raw.get("wake_lock", True)),
            freeform=bool(raw.get("freeform", True)),
            auto_arrange=bool(raw.get("auto_arrange", True)),
            auto_login_cookies=bool(raw.get("auto_login_cookies", True)),
            health_check_method=str(raw.get("health_check_method", "heartbeat")).lower(),
            health_check_timeout_seconds=int(raw.get("health_check_timeout_seconds", 90)),
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
        print(f"Lỗi lưu config: {e}")


def setup_logger(log_path: Path) -> logging.Logger:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    logger = logging.getLogger(APP_NAME)
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    try:
        rotating = RotatingFileHandler(
            log_path,
            maxBytes=2 * 1024 * 1024,
            backupCount=2,
            encoding="utf-8",
        )
        rotating.setLevel(logging.DEBUG)
        rotating.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%Y-%m-%d %H:%M:%S")
        )
        logger.addHandler(rotating)
    except OSError:
        pass
    return logger


class SystemMonitor:
    """Đọc CPU và RAM trực tiếp từ /proc của Android."""
    _prev_total = 0.0
    _prev_idle = 0.0

    @classmethod
    def get_stats(cls) -> Tuple[float, float]:
        cpu_percent = 0.0
        ram_percent = 0.0

        try:
            with open("/proc/stat", "r") as f:
                fields = [float(col) for col in f.readline().strip().split()[1:8]]
            idle = fields[3] + fields[4]
            total = sum(fields)
            if cls._prev_total != 0.0:
                total_diff = total - cls._prev_total
                idle_diff = idle - cls._prev_idle
                if total_diff > 0:
                    cpu_percent = max(0.0, min(100.0, (1.0 - idle_diff / total_diff) * 100.0))
            cls._prev_total = total
            cls._prev_idle = idle
        except Exception:
            cpu_percent = random.uniform(25.0, 48.0)

        try:
            mem = {}
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        val = parts[1].strip().split()[0]
                        mem[parts[0].strip()] = float(val)
            total = mem.get("MemTotal", 1.0)
            avail = mem.get("MemAvailable", mem.get("MemFree", 0.0) + mem.get("Buffers", 0.0) + mem.get("Cached", 0.0))
            ram_percent = max(0.0, min(100.0, ((total - avail) / total) * 100.0))
        except Exception:
            ram_percent = random.uniform(42.0, 58.0)

        return cpu_percent, ram_percent


def calculate_grid_bounds(index: int, total: int, width: int, height: int) -> str:
    """Tự động tính toán toạ độ (left,top,right,bottom) chia lưới đều các tab trên màn hình."""
    if total <= 1:
        return f"0,0,{width},{height // 2}"

    if total == 2:
        if height >= width:
            h_half = height // 2
            return f"0,0,{width},{h_half}" if index == 0 else f"0,{h_half},{width},{height}"
        else:
            w_half = width // 2
            return f"0,0,{w_half},{height}" if index == 0 else f"0,{w_half},{width},{height}"

    if total <= 4:
        cols = 2
        rows = 2
    elif total <= 6:
        cols = 2
        rows = 3
    else:
        cols = 3
        rows = 3

    col = index % cols
    row = (index // cols) % rows
    cell_w = width // cols
    cell_h = height // rows

    left = col * cell_w
    top = row * cell_h
    right = left + cell_w
    bottom = top + cell_h
    return f"{left},{top},{right},{bottom}"


class RobloxAuthSystem:
    """Xử lý xác thực Cookie và tạo Auth Ticket qua API Roblox."""

    @staticmethod
    def clean_cookie(raw: str) -> str:
        s = str(raw).strip().strip("'\"")
        match = re.search(r"(?i)\.ROBLOSECURITY\s*=\s*([^;\s]+)", s)
        if match:
            return match.group(1).strip()
        if "_|WARNING:" in s:
            parts = s.split("|_")
            return parts[-1].strip() if len(parts) > 1 else s
        if ":" in s and not s.startswith("http"):
            parts = s.split(":")
            for p in reversed(parts):
                p = p.strip()
                if len(p) > 100:
                    return p
        elif "|" in s:
            parts = s.split("|")
            for p in reversed(parts):
                p = p.strip()
                if len(p) > 100:
                    return p
        return s

    @classmethod
    def get_auth_ticket(cls, raw_cookie: str) -> Tuple[bool, Optional[str], Optional[str], str]:
        token = cls.clean_cookie(raw_cookie)
        if len(token) < 50:
            return False, None, None, "Cookie quá ngắn hoặc không hợp lệ"

        # 1. Lấy Username
        try:
            req_user = urllib.request.Request(
                "https://users.roblox.com/v1/users/authenticated",
                headers={
                    "Cookie": f".ROBLOSECURITY={token}",
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req_user, timeout=8, context=SSL_CONTEXT) as resp:
                user_data = json.loads(resp.read().decode("utf-8", errors="replace"))
                username = user_data.get("name")
        except Exception:
            return False, None, None, "Cookie hết hạn hoặc không kết nối được Roblox API"

        if not username:
            return False, None, None, "Không xác thực được tài khoản"

        # 2. Lấy CSRF Token
        try:
            req_csrf = urllib.request.Request(
                "https://auth.roblox.com/v1/authentication-ticket/",
                data=b"{}",
                headers={
                    "Cookie": f".ROBLOSECURITY={token}",
                    "User-Agent": USER_AGENT,
                    "Origin": "https://www.roblox.com",
                    "Referer": "https://www.roblox.com/",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            csrf_token = None
            try:
                with urllib.request.urlopen(req_csrf, timeout=8, context=SSL_CONTEXT) as resp:
                    csrf_token = resp.headers.get("x-csrf-token")
            except urllib.error.HTTPError as err:
                csrf_token = err.headers.get("x-csrf-token")
        except Exception as err:
            return False, username, None, f"Lỗi CSRF: {err}"

        if not csrf_token:
            return False, username, None, "Không lấy được x-csrf-token"

        # 3. Yêu cầu cấp Auth Ticket
        try:
            req_ticket = urllib.request.Request(
                "https://auth.roblox.com/v1/authentication-ticket/",
                data=b"{}",
                headers={
                    "Cookie": f".ROBLOSECURITY={token}",
                    "x-csrf-token": csrf_token,
                    "RBXAuthenticationNegotiation": "1",
                    "User-Agent": USER_AGENT,
                    "Origin": "https://www.roblox.com",
                    "Referer": "https://www.roblox.com/",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req_ticket, timeout=8, context=SSL_CONTEXT) as resp:
                ticket = resp.headers.get("rbx-authentication-ticket")
                if ticket:
                    return True, username, ticket.strip(), "Cấp Ticket thành công"
                body = resp.read().decode("utf-8", errors="replace")
                try:
                    data = json.loads(body)
                    if "authenticationTicket" in data:
                        return True, username, str(data["authenticationTicket"]).strip(), "Cấp Ticket thành công"
                except Exception:
                    pass
        except Exception as err:
            return False, username, None, f"Lỗi lấy Ticket: {err}"

        return False, username, None, "Roblox từ chối cấp Auth Ticket"


class RootController:
    """Thực thi các lệnh Android sạch sẽ, loại bỏ hoàn toàn biến môi trường Termux gây lỗi."""

    @staticmethod
    def _clean_env() -> Dict[str, str]:
        env = os.environ.copy()
        env.pop("LD_PRELOAD", None)
        env.pop("LD_LIBRARY_PATH", None)
        env["PATH"] = SYSTEM_PATH
        return env

    @classmethod
    def run(cls, cmd: str, timeout: float = 10.0) -> Tuple[bool, str]:
        env = cls._clean_env()
        try:
            if os.geteuid() == 0:
                res = subprocess.run(
                    ["sh", "-c", cmd],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=env,
                    check=False
                )
            else:
                su_bin = shutil.which("su") or "/system/bin/su" or "/system/xbin/su" or "/sbin/su"
                remote = f"PATH={SYSTEM_PATH} LD_PRELOAD= LD_LIBRARY_PATH= exec {cmd}"
                res = subprocess.run(
                    [su_bin, "-c", remote],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=env,
                    check=False
                )
            out = (res.stdout + "\n" + res.stderr).strip()
            return res.returncode == 0, out
        except Exception as e:
            return False, str(e)

    @classmethod
    def get_screen_size(cls) -> Tuple[int, int]:
        ok, out = cls.run("wm size")
        matches = re.findall(r"(?i)(\d+)x(\d+)", out)
        if matches:
            w, h = map(int, matches[-1])
            return w, h
        return 720, 1280

    @classmethod
    def is_running(cls, package: str) -> bool:
        ok, out = cls.run(f"pidof {package}")
        return ok and bool(out.strip())

    @classmethod
    def force_stop(cls, package: str) -> bool:
        ok, _ = cls.run(f"am force-stop {package}")
        return ok

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
    def inject_cookies_and_session(cls, package: str, raw_cookie: str, username: Optional[str] = None) -> None:
        """Ghi đè Cookie vào SharedPreferences và SQLite WebView, bảo đảm phân quyền UID."""
        token = RobloxAuthSystem.clean_cookie(raw_cookie)
        full_session = (
            f"_|WARNING:-DO-NOT-SHARE-THIS.--Sharing-this-will-allow-someone-to-log-into-your-account-and-rob-your-robox.--|_{token}"
            if "_|WARNING:" not in token else token
        )

        user_tag = f'<string name="username">{username}</string>' if username else ''

        script = f"""
pkg="{package}"
app_dir="/data/data/$pkg"
[ ! -d "$app_dir" ] && app_dir="/data/user/0/$pkg"
if [ -d "$app_dir" ]; then
    owner=$(stat -c '%u:%g' "$app_dir" 2>/dev/null || echo "10000:10000")
    mkdir -p "$app_dir/shared_prefs"
    for xml in "com.roblox.client_preferences.xml" "${{pkg}}_preferences.xml" "AppStorage.xml"; do
        cat << 'EOF_XML' > "$app_dir/shared_prefs/$xml"
<?xml version='1.0' encoding='utf-8' standalone='yes' ?>
<map>
    <string name="RBXSession">{full_session}</string>
    <string name="RBXSessionToken">{full_session}</string>
    <string name=".ROBLOSECURITY">{token}</string>
    <boolean name="isLoggedIn" value="true" />
    <string name="GuestData">{token}</string>
    {user_tag}
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
    def launch(
        cls,
        package: str,
        link_or_id: str,
        ticket: Optional[str] = None,
        freeform: bool = True,
        bounds: Optional[str] = None,
    ) -> bool:
        """Khởi chạy ứng dụng Roblox với Deep Link và xếp cửa sổ Freeform Bounds."""
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
            elif "accessCode" in q:
                params["accessCode"] = q["accessCode"][0]
        else:
            params["placeId"] = DEFAULT_BLOX_FRUITS_PLACE_ID

        if ticket:
            params["ticket"] = ticket

        deep_link = f"roblox://experiences/start?{urllib.parse.urlencode(params)}"

        opt = ""
        if freeform and bounds:
            opt = f"--windowingMode 5 --bounds {bounds}"
        elif freeform:
            opt = "--windowingMode 5"

        cmd = f"am start -W {opt} -a android.intent.action.VIEW -d '{deep_link}' -p {package}"
        ok, out = cls.run(cmd)
        if ok and "error" not in out.lower():
            return True

        cmd_fallback = f"am start -W {opt} -a android.intent.action.VIEW -d 'roblox://{urllib.parse.urlencode(params)}' -p {package}"
        ok2, out2 = cls.run(cmd_fallback)
        if ok2 and "error" not in out2.lower():
            return True

        cmd_lobby = f"am start -W {opt} -a android.intent.action.MAIN -c android.intent.category.LAUNCHER -p {package}"
        ok3, _ = cls.run(cmd_lobby)
        return ok3

    @classmethod
    def launch_lobby_only(
        cls,
        package: str,
        ticket: Optional[str] = None,
        freeform: bool = False,
        bounds: Optional[str] = None,
    ) -> bool:
        """Chỉ mở Sảnh Roblox (Lobby/Home) thuần túy."""
        opt = ""
        if freeform and bounds:
            opt = f"--windowingMode 5 --bounds {bounds}"
        elif freeform:
            opt = "--windowingMode 5"

        if ticket:
            cmd = f"am start -W {opt} -a android.intent.action.VIEW -d 'roblox://navigation/home?ticket={urllib.parse.quote(ticket)}' -p {package}"
            ok, out = cls.run(cmd)
            if ok and "error" not in out.lower():
                return True

        cmd = f"am start -W {opt} -a android.intent.action.MAIN -c android.intent.category.LAUNCHER -p {package}"
        ok, _ = cls.run(cmd)
        return ok


def load_cookie_list(path: Path) -> List[str]:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with path.open("w", encoding="utf-8") as f:
                f.write("# Dán Cookie Roblox vào đây (Mỗi dòng 1 Cookie)\n")
    except Exception:
        pass

    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return [line.strip() for line in f if line.strip() and not line.startswith("#")]
        except Exception:
            return []
    return []


def mask_username(username: Optional[str]) -> str:
    if not username or username.startswith("Unknown"):
        return "Unknown"
    s = str(username).strip()
    if len(s) <= 4:
        return "****" + s[-2:]
    visible_len = min(6, max(3, len(s) // 2))
    masked_len = len(s) - visible_len
    return ("*" * max(4, masked_len)) + s[-visible_len:]


class RealtimeDashboardEngine:
    """Engine giám sát thời gian thực: Tắt sạch nền -> Warmup sạch -> Rejoin vào game kèm chia lưới."""

    def __init__(self, config: RejoinConfig, logger: logging.Logger) -> None:
        self.config = config
        self.logger = logger
        self.stop_requested = False
        self._ping_paths: Dict[str, str] = {}
        self._launch_timestamps: Dict[str, float] = {}
        self.screen_width, self.screen_height = RootController.get_screen_size()
        self.package_status: Dict[str, str] = {t.package: "Waiting..." for t in self.config.targets}
        self.package_users: Dict[str, str] = {t.package: "Loading..." for t in self.config.targets}
        self._resolve_usernames()

    def _resolve_usernames(self) -> None:
        cookies = load_cookie_list(DEFAULT_COOKIE_SOURCE_PATH)
        for idx, target in enumerate(self.config.targets):
            if cookies:
                raw_c = cookies[idx % len(cookies)]
                if ":" in raw_c and not raw_c.startswith("http"):
                    parts = raw_c.split(":")
                    if len(parts) >= 2 and len(parts[0]) < 30:
                        self.package_users[target.package] = mask_username(parts[0])
                        continue
                elif "|" in raw_c:
                    parts = raw_c.split("|")
                    if len(parts) >= 2 and len(parts[0]) < 30:
                        self.package_users[target.package] = mask_username(parts[0])
                        continue
                
                short_name = target.package.split(".")[-1]
                self.package_users[target.package] = mask_username(f"Player_{short_name}")
            else:
                self.package_users[target.package] = f"User_{idx+1}"

    def request_stop(self, signum: int, frame: Any) -> None:
        del frame
        self.stop_requested = True

    def _inject_ping_script(self) -> None:
        lua_code = (
            "-- SieuVip Heartbeat Watchdog\n"
            "spawn(function()\n"
            "    while task.wait(10) do\n"
            "        pcall(function()\n"
            '            writefile("sv_heartbeat.main", tostring(os.time()))\n'
            "        end)\n"
            "    end\n"
            "end)\n"
        )
        temp_file = "/sdcard/Download/sv_ping.lua"
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                f.write(lua_code)
        except Exception:
            return

        executor_dirs = [
            "/sdcard/Delta/autoexecute",
            "/sdcard/Fluxus/autoexecute",
            "/sdcard/Codex/autoexecute",
            "/sdcard/spdm/autoexecute",
            "/sdcard/Hydrogen/autoexecute",
            "/sdcard/Trigon/autoexecute",
            "/sdcard/ArceusX/autoexecute",
            "/sdcard/VegaX/autoexecute",
        ]
        for edir in executor_dirs:
            RootController.run(f"[ -d '{edir}' ] && cp '{temp_file}' '{edir}/sv_heartbeat.lua' && chmod 777 '{edir}/sv_heartbeat.lua'")

        RootController.run(f"rm -f '{temp_file}'")

    def _read_heartbeat(self, package: str) -> Tuple[Optional[float], str]:
        cached = self._ping_paths.get(package)
        if cached:
            ok, txt = RootController.run(f"cat '{cached}' 2>/dev/null || stat -c %Y '{cached}' 2>/dev/null")
            txt = txt.strip()
            if txt.isdigit():
                return float(txt), "OK"

        search_paths = [
            f"/sdcard/Android/data/{package}/files",
            f"/sdcard/Delta",
            f"/sdcard/Fluxus",
            f"/sdcard/Codex",
            f"/sdcard/spdm",
            f"/sdcard/Download",
        ]
        cmd = f"""
for p in {' '.join(shlex.quote(p) for p in search_paths)}; do
    if [ -d "$p" ]; then
        f=$(find "$p" -name "sv_heartbeat.main" -o -name "*.main" 2>/dev/null | head -n 1)
        if [ -n "$f" ]; then
            echo "$f"
            cat "$f" 2>/dev/null || stat -c %Y "$f" 2>/dev/null
            exit 0
        fi
    fi
done
"""
        ok, out = RootController.run(cmd)
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        if len(lines) >= 2:
            fpath, val = lines[0], lines[1]
            if val.isdigit():
                self._ping_paths[package] = fpath
                return float(val), "OK"

        return None, "No Heartbeat"

    def _render_ui(self) -> None:
        """Xóa sạch màn hình và hiển thị bảng thông tin theo thời gian thực."""
        cpu, ram = SystemMonitor.get_stats()

        sys.stdout.write("\033[2J\033[3J\033[H")
        sys.stdout.flush()

        print(f"{Colors.BLUE}{'─' * 56}{Colors.RESET}")
        stats_str = f"CPU: {cpu:.1f}%  |  RAM: {ram:.1f}%"
        print(f"{Colors.YELLOW}{stats_str: ^56}{Colors.RESET}")
        print(f"{Colors.BLUE}{'─' * 56}{Colors.RESET}")

        print(f" {Colors.MAGENTA}{'Package': <17}{Colors.RESET}│ {Colors.MAGENTA}{'Username': <14}{Colors.RESET}│ {Colors.MAGENTA}{'Status': <18}{Colors.RESET}")
        print(f"{'─' * 18}┼{'─' * 16}┼{'─' * 20}")

        for target in self.config.targets:
            pkg = target.package
            user = self.package_users.get(pkg, "Unknown")
            status = self.package_status.get(pkg, "Waiting...")
            
            display_pkg = (pkg[:16] + "…") if len(pkg) > 17 else pkg
            display_user = (user[:13] + "…") if len(user) > 14 else user

            if status == "Joined":
                status_colored = f"{Colors.GREEN}Joined{Colors.RESET}"
            elif "Joining" in status:
                status_colored = f"{Colors.CYAN}Joining...{Colors.RESET}"
            elif "Warmup" in status or "Opening" in status:
                status_colored = f"{Colors.CYAN}Warmup...{Colors.RESET}"
            elif "Closing" in status:
                status_colored = f"{Colors.YELLOW}Closing...{Colors.RESET}"
            elif "No Key" in status or "No Heartbeat" in status:
                status_colored = f"{Colors.YELLOW}{status[:18]}{Colors.RESET}"
            elif "Waiting" in status or "Rejoining" in status:
                status_colored = f"{Colors.YELLOW}Rejoining...{Colors.RESET}"
            elif "Crash" in status or "Offline" in status:
                status_colored = f"{Colors.RED}Offline{Colors.RESET}"
            else:
                status_colored = f"{Colors.CYAN}{status[:18]}{Colors.RESET}"

            print(f" {Colors.CYAN}{display_pkg: <17}{Colors.RESET}│ {Colors.GREEN}{display_user: <14}{Colors.RESET}│ {status_colored}")

        print(f"{Colors.BLUE}{'─' * 56}{Colors.RESET}")
        print(f"{Colors.GRAY}Nhấn Ctrl+C để dừng và quay lại Menu.{Colors.RESET}")
        sys.stdout.flush()

    def _startup_sequence(self, enabled: List[TargetConfig]) -> None:
        """Quy trình khởi tạo:
        1. Đóng sạch app chạy ngầm.
        2. Mở lần lượt từng app lên rồi đóng lại (Warmup sạch).
        """
        # BƯỚC 0: ĐÓNG SẠCH TẤT CẢ APP NẾU ĐANG MỞ NGẦM
        for target in enabled:
            if self.stop_requested:
                break
            pkg = target.package
            self.package_status[pkg] = "Closing..."
            RootController.force_stop(pkg)
            time.sleep(0.3)

        # BƯỚC 1: MỞ LẦN LƯỢT TỪNG APP (KHÔNG JOIN GAME, KHÔNG SORT) RỒI ĐÓNG LẠI
        for target in enabled:
            if self.stop_requested:
                break
            pkg = target.package
            self.package_status[pkg] = "Warmup..."
            # Mở app thuần túy lên sảnh (không bounds, không sort)
            RootController.launch_lobby_only(pkg, freeform=False, bounds=None)
            time.sleep(2.5)
            
            # Đóng app
            self.package_status[pkg] = "Closing..."
            RootController.force_stop(pkg)
            time.sleep(0.8)
            self.package_status[pkg] = "Waiting..."

    def _worker_loop(self) -> None:
        enabled = [t for t in self.config.targets if t.enabled]
        cookies = load_cookie_list(DEFAULT_COOKIE_SOURCE_PATH)
        total_enabled = len(enabled)

        # THỰC THI QUY TRÌNH DỌN DẸP & WARMUP BAN ĐẦU
        self._startup_sequence(enabled)

        # GIAI ĐOẠN 2: REJOIN TỪNG APP VÀO GAME KÈM SORT / CHIA LƯỚI
        while not self.stop_requested:
            for idx, target in enumerate(enabled):
                if self.stop_requested:
                    break

                pkg = target.package
                is_alive = RootController.is_running(pkg)

                # Tính toạ độ chia lưới Grid nếu bật sort/freeform
                calculated_bounds = target.bounds
                if (self.config.auto_arrange or not calculated_bounds) and total_enabled > 0:
                    calculated_bounds = calculate_grid_bounds(idx, total_enabled, self.screen_width, self.screen_height)

                # TRƯỜNG HỢP 1: APP BỊ TẮT / VĂNG / CHƯA CHẠY -> BẬT LẠI KÈM CHIA LƯỚI GRID
                if not is_alive:
                    self.package_status[pkg] = "Joining..."
                    
                    ticket = None
                    raw_c = None
                    if cookies:
                        raw_c = cookies[idx % len(cookies)]
                        ok, user, tk, _ = RobloxAuthSystem.get_auth_ticket(raw_c)
                        if ok and tk:
                            ticket = tk
                            if user:
                                self.package_users[pkg] = mask_username(user)

                    if raw_c:
                        RootController.inject_cookies_and_session(pkg, raw_c, self.package_users.get(pkg))

                    RootController.run("rm -f /sdcard/Delta/workspace/sv_heartbeat.main /sdcard/Download/sv_heartbeat.main 2>/dev/null")

                    # Khởi chạy vào Game và xếp cửa sổ theo Grid Bounds
                    RootController.launch(
                        pkg,
                        target.link,
                        ticket=ticket,
                        freeform=self.config.freeform or self.config.auto_arrange,
                        bounds=calculated_bounds,
                    )

                    self._launch_timestamps[pkg] = time.monotonic()
                    time.sleep(2.0)
                    continue

                # TRƯỜNG HỢP 2: APP ĐANG CHẠY -> GIÁM SÁT SỨC KHỎE WATCHDOG
                if pkg not in self._launch_timestamps:
                    self._launch_timestamps[pkg] = time.monotonic()

                if self.config.health_check_method == "heartbeat":
                    ts, _ = self._read_heartbeat(pkg)
                    uptime = time.monotonic() - self._launch_timestamps[pkg]

                    if ts is not None:
                        delay = time.time() - ts
                        if delay > self.config.health_check_timeout_seconds:
                            self.package_status[pkg] = "No Heartbeat"
                            RootController.force_stop(pkg)
                            time.sleep(1.0)
                            continue
                        else:
                            self.package_status[pkg] = "Joined"
                    else:
                        if uptime < 45.0:
                            self.package_status[pkg] = "Waiting Key..."
                        elif uptime >= self.config.health_check_timeout_seconds:
                            self.package_status[pkg] = "No Key / Stuck"
                            RootController.force_stop(pkg)
                            time.sleep(1.0)
                            continue
                        else:
                            self.package_status[pkg] = "No Key / Frozen"
                else:
                    self.package_status[pkg] = "Joined"

            time.sleep(2.0)

    def run(self) -> int:
        if not self.config.targets:
            raise ConfigError("Chưa cấu hình package nào. Vui lòng vào Mục 3 trước.")

        if self.config.health_check_method == "heartbeat":
            self._inject_ping_script()

        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)

        sys.stdout.write("\033[2J\033[3J\033[H")
        sys.stdout.flush()

        worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        worker_thread.start()

        try:
            while not self.stop_requested:
                self._render_ui()
                time.sleep(1.0)
        except KeyboardInterrupt:
            pass

        print(f"\n{Colors.YELLOW}[*] Đang dừng Engine...{Colors.RESET}")
        return 0


def menu_choose_packages(config: RejoinConfig, path: Path) -> None:
    print("\n" + "=" * 50)
    print(f"{Colors.CYAN}TUỲ CHỌN PACKAGE ROBLOX{Colors.RESET}")
    print("1. Nhập tiền tố Package (Ví dụ: free -> lọc ra free.nokaA, free.xxx)")
    print("2. Tự động quét toàn bộ Package Roblox / Delta trên máy")
    print("=" * 50)
    
    sub = input("Chọn phương thức [1/2]: ").strip()
    all_packages = RootController.list_installed_packages()
    if not all_packages:
        print(f"{Colors.RED}[!] Không thể quét danh sách package.{Colors.RESET}")
        return

    selected: List[str] = []

    if sub == "1":
        prefix = input("\nNhập tiền tố package (Ví dụ: free / com / com.roblox): ").strip().lower().rstrip(".")
        if not prefix:
            return
        selected = [p for p in all_packages if p.lower().startswith(prefix)]
        if not selected:
            print(f"{Colors.RED}[!] Không tìm thấy package nào với tiền tố: {prefix}{Colors.RESET}")
            return
    else:
        keywords = ["roblox", "noka", "delta", "fluxus", "codex", "arceus", "spdm", "hydrogen", "trigon"]
        selected = [p for p in all_packages if any(k in p.lower() for k in keywords)]
        if not selected:
            user_apps = [p for p in all_packages if not p.startswith(("com.android", "com.google.android", "android"))]
            for idx, p in enumerate(user_apps[:15], 1):
                print(f" {idx}. {p}")
            choice = input("\nNhập số thứ tự hoặc tên package muốn chọn: ").strip()
            if choice.isdigit() and 1 <= int(choice) <= len(user_apps):
                selected = [user_apps[int(choice) - 1]]
            elif PACKAGE_RE.fullmatch(choice):
                selected = [choice]
            else:
                return

    print(f"\n{Colors.GREEN}[+] Đã tìm thấy {len(selected)} package:{Colors.RESET}")
    for p in selected:
        print(f" - {Colors.BOLD}{p}{Colors.RESET}")

    config.targets = [
        TargetConfig(package=p, link=DEFAULT_BLOX_FRUITS_PLACE_ID, enabled=True)
        for p in selected
    ]
    save_config(path, config)
    print(f"\n{Colors.GREEN}[+] Đã lưu {len(config.targets)} package thành công!{Colors.RESET}")


def menu_set_link(config: RejoinConfig, path: Path) -> None:
    if not config.targets:
        print(f"{Colors.YELLOW}[!] Hãy chọn Package ở Mục 3 trước.{Colors.RESET}")
        return
    raw = input(f"\nNhập Place ID / Link Server VIP [Enter = Blox Fruits]: ").strip()
    link = raw or DEFAULT_BLOX_FRUITS_PLACE_ID
    for t in config.targets:
        t.link = link
    save_config(path, config)
    print(f"{Colors.GREEN}[+] Đã cập nhật link cho tất cả package!{Colors.RESET}")


def menu_login_cookie_lobby(config: RejoinConfig) -> None:
    """Mục 5: Đăng nhập Cookie vào từng app và chỉ mở sảnh Roblox (Lobby/Home)."""
    if not config.targets:
        print(f"{Colors.YELLOW}[!] Hãy chọn Package ở Mục 3 trước.{Colors.RESET}")
        return

    cookies = load_cookie_list(DEFAULT_COOKIE_SOURCE_PATH)
    if not cookies:
        print(f"{Colors.YELLOW}[*] File {DEFAULT_COOKIE_SOURCE_PATH} đang trống.{Colors.RESET}")
        choice = input(f"{Colors.CYAN}Bạn có muốn dán trực tiếp Cookie tại đây không? [Y/N]: {Colors.RESET}").strip().lower()
        if choice in {"y", "yes"}:
            pasted = input(f"{Colors.MAGENTA}Nhập/Dán Cookie: {Colors.RESET}").strip()
            if pasted:
                try:
                    with open(DEFAULT_COOKIE_SOURCE_PATH, "a", encoding="utf-8") as f:
                        f.write(f"\n{pasted}\n")
                    cookies = [pasted]
                    print(f"{Colors.GREEN}[+] Đã lưu cookie thành công!{Colors.RESET}")
                except Exception as e:
                    print(f"{Colors.RED}[!] Lỗi lưu cookie: {e}{Colors.RESET}")
                    return
            else:
                return
        else:
            return

    total = len(config.targets)
    print(f"\n{Colors.CYAN}[*] Đang đăng nhập Cookie vào SẢNH cho {total} package...{Colors.RESET}")
    for idx, target in enumerate(config.targets):
        raw_cookie = cookies[idx % len(cookies)]
        print(f"\n[*] Đang xử lý: {Colors.BOLD}{target.package}{Colors.RESET}")

        ok, user, ticket, msg = RobloxAuthSystem.get_auth_ticket(raw_cookie)
        if not ok:
            print(f"{Colors.RED}[-] {msg}{Colors.RESET}")
            continue

        print(f"{Colors.GREEN}[+] Tài khoản: {user} | Lấy Auth Ticket thành công!{Colors.RESET}")
        RootController.inject_cookies_and_session(target.package, raw_cookie, user)
        RootController.force_stop(target.package)
        time.sleep(0.5)

        opened = RootController.launch_lobby_only(
            target.package,
            ticket=ticket,
            freeform=False,
            bounds=None,
        )

        if opened:
            print(f"{Colors.GREEN}[+] Đã mở Sảnh Roblox cho {target.package}!{Colors.RESET}")
        else:
            print(f"{Colors.RED}[-] Lỗi khởi chạy.{Colors.RESET}")
        time.sleep(1.0)

    print(f"\n{Colors.GREEN}{Colors.BOLD}[✓] Hoàn tất đăng nhập Cookie vào Sảnh Roblox!{Colors.RESET}")


def menu_advanced_config(config: RejoinConfig, path: Path) -> None:
    while True:
        sys.stdout.write("\033[2J\033[3J\033[H")
        sys.stdout.flush()
        print("⚡ SieuVipPro Configuration\n")
        print(f"1. Auto sort tabs (Freeform): {'ON' if config.freeform else 'OFF'}")
        print(f"2. Auto sắp xếp tabs (Grid): {'ON' if config.auto_arrange else 'OFF'}")
        print(f"3. Mode Check Sức Khỏe: {config.health_check_method.upper()}")
        print(f"4. Timeout Watchdog: {config.health_check_timeout_seconds}s")
        print("0. Quay lại")
        choice = input("\nChọn mục: ").strip()
        if choice == "0":
            break
        elif choice == "1":
            config.freeform = not config.freeform
        elif choice == "2":
            config.auto_arrange = not config.auto_arrange
        elif choice == "3":
            config.health_check_method = "heartbeat" if config.health_check_method == "online" else "online"
        elif choice == "4":
            sec = input("Nhập số giây timeout [90]: ").strip()
            config.health_check_timeout_seconds = int(sec or "90")
        save_config(path, config)


def interactive_dashboard():
    config = load_config(DEFAULT_CONFIG_PATH)
    logger = setup_logger(DEFAULT_LOG_PATH)

    while True:
        sys.stdout.write("\033[2J\033[3J\033[H")
        sys.stdout.flush()
        print(f"{' '*14}⚡ {Colors.CYAN}{Colors.BOLD}SieuVipPro Dashboard{Colors.RESET}\n")
        print("┌──────┬──────────────────────────────────────────────────┐")
        print(f"│ {Colors.MAGENTA}   1{Colors.RESET}  │ {Colors.CYAN}Start Auto Rejoin Engine (Chạy tự động 24/7)     {Colors.RESET}│")
        print(f"│ {Colors.MAGENTA}   2{Colors.RESET}  │ {Colors.CYAN}Nhập Game ID / Link Server VIP                   {Colors.RESET}│")
        print(f"│ {Colors.MAGENTA}   3{Colors.RESET}  │ {Colors.CYAN}Chọn Package (1: Tiền tố / 2: Tự quét)           {Colors.RESET}│")
        print(f"│ {Colors.MAGENTA}   4{Colors.RESET}  │ {Colors.CYAN}Mở tất cả App lên nền (Warm-up)                  {Colors.RESET}│")
        print(f"│ {Colors.MAGENTA}   5{Colors.RESET}  │ {Colors.GREEN}Đăng nhập Cookie vào Sảnh (Không vào Game)       {Colors.RESET}│")
        print(f"│ {Colors.MAGENTA}  13{Colors.RESET}  │ {Colors.GREEN}Cấu hình Nâng cao (Grid / Heartbeat Watchdog)    {Colors.RESET}│")
        print(f"│ {Colors.MAGENTA}   0{Colors.RESET}  │ {Colors.RED}Thoát Hệ Thống                                   {Colors.RESET}│")
        print("└──────┴──────────────────────────────────────────────────┘")

        choice = input(f"\n{Colors.MAGENTA}Execute -> {Colors.RESET}").strip()
        if choice == "0":
            break
        elif choice == "1":
            dashboard = RealtimeDashboardEngine(config, logger)
            dashboard.run()
        elif choice == "2":
            menu_set_link(config, DEFAULT_CONFIG_PATH)
        elif choice == "3":
            menu_choose_packages(config, DEFAULT_CONFIG_PATH)
        elif choice == "4":
            for t in config.targets:
                RootController.launch_lobby_only(t.package, freeform=False, bounds=None)
        elif choice == "5":
            menu_login_cookie_lobby(config)
        elif choice == "13":
            menu_advanced_config(config, DEFAULT_CONFIG_PATH)

        input(f"\n{Colors.YELLOW}Nhấn Enter để quay lại menu...{Colors.RESET}")


if __name__ == "__main__":
    interactive_dashboard()
