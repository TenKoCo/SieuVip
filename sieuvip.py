#!/data/data/com.termux/files/usr/bin/python
"""SieuVip Roblox rejoin engine designed for Termux on Android.

The script only uses Python's standard library. For reliable force-stop/rejoin it
needs one privileged backend: Magisk/KSU ``su`` or a connected local ``adb shell``.
An unprivileged soft backend is available, but Android 14+ can reject ``am`` calls.
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
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import fcntl
except ImportError:
    fcntl = None


APP_NAME = "sieuvip-rejoin"
DEFAULT_CONFIG_PATH = Path("/sdcard/Download/sieuvip_config.json")
DEFAULT_COOKIE_PATH = Path("/sdcard/Download/cookies_store.json")
DEFAULT_LOG_PATH = Path("/sdcard/Download/sieuvip_rejoin.log")
DEFAULT_LOCK_PATH = Path("/sdcard/Download/sieuvip_rejoin.lock")
DEFAULT_COOKIE_SOURCE_PATH = Path("/sdcard/Download/cookie.txt")
DEFAULT_BLOX_FRUITS_PLACE_ID = "2753915549"
PACKAGE_RE = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*$")
BOUNDS_RE = re.compile(r"^\d+,\d+,\d+,\d+$")
USER_AGENT = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36 Roblox/Android"
SYSTEM_PATH = (
    "/product/bin:/apex/com.android.runtime/bin:/apex/com.android.art/bin:"
    "/system_ext/bin:/system/bin:/system/xbin:/odm/bin:/vendor/bin:/vendor/xbin"
)
SYSTEM_COMMANDS = {
    "am",
    "cmd",
    "dumpsys",
    "getprop",
    "id",
    "monkey",
    "pidof",
    "pm",
    "settings",
    "wm",
}

# SSL Context
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


class BackendError(AppError):
    pass


@dataclasses.dataclass(frozen=True)
class CommandResult:
    argv: Tuple[str, ...]
    returncode: int
    output: str
    elapsed: float
    timed_out: bool = False
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


@dataclasses.dataclass
class TargetConfig:
    package: str
    link: str
    enabled: bool = True
    bounds: Optional[str] = None

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "TargetConfig":
        package = str(raw.get("package", "")).strip()
        link = str(raw.get("link", "")).strip()
        enabled = bool(raw.get("enabled", True))
        bounds_value = raw.get("bounds")
        bounds = str(bounds_value).strip() if bounds_value else None
        if not PACKAGE_RE.fullmatch(package):
            raise ConfigError(f"Package không hợp lệ: {package!r}")
        if not link:
            raise ConfigError(f"Package {package} chưa có link/Place ID")
        if bounds and not BOUNDS_RE.fullmatch(bounds):
            raise ConfigError(
                f"Bounds của {package} phải có dạng left,top,right,bottom"
            )
        return cls(package=package, link=link, enabled=enabled, bounds=bounds)


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
    randomize_android_id_each_cycle: bool = False
    auto_login_cookies: bool = False
    health_check_method: str = "heartbeat"
    health_check_timeout_seconds: int = 120

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "RejoinConfig":
        if not isinstance(raw, dict):
            raise ConfigError("Nội dung config phải là JSON object")

        targets_raw = raw.get("targets")
        targets: List[TargetConfig] = []
        if isinstance(targets_raw, list):
            for item in targets_raw:
                if not isinstance(item, dict):
                    raise ConfigError("Mỗi phần tử targets phải là JSON object")
                targets.append(TargetConfig.from_dict(item))
        else:
            packages = raw.get("packages", [])
            server_links = raw.get("server_links", {})
            if isinstance(packages, list) and isinstance(server_links, dict):
                for package in packages:
                    package_text = str(package).strip()
                    link = str(server_links.get(package_text, "")).strip()
                    if package_text and link:
                        targets.append(
                            TargetConfig.from_dict(
                                {"package": package_text, "link": link}
                            )
                        )

        interval = raw.get("interval_seconds")
        if interval is None:
            interval = float(raw.get("interval_minutes", 15)) * 60

        health_check_method = str(
            raw.get("health_check_method", "heartbeat")
        ).strip().lower()
        if health_check_method not in {"online", "heartbeat"}:
            health_check_method = "heartbeat"

        config = cls(
            targets=targets,
            interval_seconds=max(0, int(float(interval))),
            warmup_seconds=_clamp_float(raw.get("warmup_seconds", 2.5), 0, 60),
            between_apps_seconds=_clamp_float(
                raw.get("between_apps_seconds", 1.0), 0, 60
            ),
            retries=_clamp_int(raw.get("retries", 2), 0, 5),
            retry_backoff_seconds=_clamp_float(
                raw.get("retry_backoff_seconds", 2.0), 0, 60
            ),
            command_timeout_seconds=_clamp_float(
                raw.get("command_timeout_seconds", 25), 5, 120
            ),
            wake_lock=bool(raw.get("wake_lock", True)),
            freeform=bool(raw.get("freeform", raw.get("auto_resize", False))),
            auto_arrange=bool(raw.get("auto_arrange", False)),
            randomize_android_id_each_cycle=bool(
                raw.get("randomize_android_id_each_cycle", False)
            ),
            auto_login_cookies=bool(raw.get("auto_login_cookies", False)),
            health_check_method=health_check_method,
            health_check_timeout_seconds=_clamp_int(
                raw.get("health_check_timeout_seconds", 120), 15, 3600
            ),
        )
        return config

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
            "randomize_android_id_each_cycle": (
                self.randomize_android_id_each_cycle
            ),
            "auto_login_cookies": self.auto_login_cookies,
            "health_check_method": self.health_check_method,
            "health_check_timeout_seconds": self.health_check_timeout_seconds,
            "targets": [dataclasses.asdict(target) for target in self.targets],
        }


def _clamp_float(value: Any, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Giá trị số không hợp lệ: {value!r}") from exc
    return max(minimum, min(maximum, parsed))


def _clamp_int(value: Any, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Giá trị số nguyên không hợp lệ: {value!r}") from exc
    return max(minimum, min(maximum, parsed))


def load_config(path: Path) -> RejoinConfig:
    try:
        with path.open("r", encoding="utf-8") as file:
            raw = json.load(file)
    except FileNotFoundError as exc:
        raise ConfigError(
            f"Không thấy config {path}. Hãy chạy thiết lập package trước."
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Không đọc được config {path}: {exc}") from exc
    return RejoinConfig.from_dict(raw)


def save_config(path: Path, config: RejoinConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(config.to_dict(), file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ConfigError(f"Không lưu được config {path}: {exc}") from exc


def setup_logger(log_path: Path, verbose: bool = False) -> logging.Logger:
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
            backupCount=3,
            encoding="utf-8",
        )
        rotating.setLevel(logging.DEBUG)
        rotating.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)s | %(message)s", "%Y-%m-%d %H:%M:%S"
            )
        )
        logger.addHandler(rotating)
    except OSError:
        pass
    return logger


class SingleInstance:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.file: Optional[Any] = None

    def __enter__(self) -> "SingleInstance":
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.file = self.path.open("a+", encoding="utf-8")
            if fcntl is not None:
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.file.seek(0)
            self.file.truncate()
            self.file.write(str(os.getpid()))
            self.file.flush()
        except BlockingIOError as exc:
            raise AppError("Đã có một tiến trình auto rejoin khác đang chạy") from exc
        except OSError:
            pass
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.file is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            finally:
                self.file.close()


class WakeLock:
    def __init__(self, enabled: bool, logger: logging.Logger) -> None:
        self.enabled = enabled
        self.logger = logger
        self.acquired = False

    def __enter__(self) -> "WakeLock":
        command = shutil.which("termux-wake-lock")
        if not self.enabled or not command:
            return self
        try:
            result = subprocess.run(
                [command], capture_output=True, text=True, timeout=8, check=False
            )
            self.acquired = result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            pass
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if not self.acquired:
            return
        command = shutil.which("termux-wake-unlock")
        if command:
            try:
                subprocess.run(
                    [command], capture_output=True, text=True, timeout=8, check=False
                )
            except (OSError, subprocess.SubprocessError):
                pass


class AndroidBackend:
    def __init__(
        self,
        kind: str,
        *,
        su_path: Optional[str] = None,
        adb_path: Optional[str] = None,
        adb_serial: Optional[str] = None,
    ) -> None:
        self.kind = kind
        self.su_path = su_path
        self.adb_path = adb_path
        self.adb_serial = adb_serial

    @property
    def can_force_stop(self) -> bool:
        return self.kind in {"direct", "su", "adb"}

    @property
    def can_inspect_all_packages(self) -> bool:
        return self.kind in {"direct", "su", "adb"}

    @property
    def can_write_secure_settings(self) -> bool:
        return self.kind == "su" or (self.kind == "direct" and os.geteuid() == 0)

    @property
    def can_write_app_data(self) -> bool:
        return self.kind == "su" or (self.kind == "direct" and os.geteuid() == 0)

    @property
    def description(self) -> str:
        if self.kind == "direct":
            return f"direct uid={os.geteuid()}"
        if self.kind == "su":
            return f"root qua {self.su_path}"
        if self.kind == "adb":
            return f"adb shell ({self.adb_serial or 'auto'})"
        return "Termux soft mode"

    def run(
        self,
        argv: Sequence[str],
        timeout: float = 25.0,
        input_text: Optional[str] = None,
    ) -> CommandResult:
        if not argv:
            raise ValueError("argv không được rỗng")
        logical_argv = tuple(str(value) for value in argv)
        started = time.monotonic()
        command, env = self._build_process(logical_argv)
        try:
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
                env=env,
                input=input_text,
            )
            stdout = process.stdout.strip() if process.stdout else ""
            stderr = process.stderr.strip() if process.stderr else ""
            output = "\n".join(
                part.strip()
                for part in (stdout, stderr)
                if part and part.strip()
            )
            return CommandResult(
                argv=logical_argv,
                returncode=process.returncode,
                output=output,
                elapsed=time.monotonic() - started,
                stdout=stdout,
                stderr=stderr,
            )
        except subprocess.TimeoutExpired as exc:
            decoded_parts = []
            for value in (exc.stdout, exc.stderr):
                if isinstance(value, bytes):
                    value = value.decode("utf-8", errors="replace")
                decoded_parts.append(str(value).strip() if value else "")
            return CommandResult(
                argv=logical_argv,
                returncode=124,
                output="\n".join(part for part in decoded_parts if part)
                or f"Timeout sau {timeout:.1f}s",
                elapsed=time.monotonic() - started,
                timed_out=True,
                stdout=decoded_parts[0],
                stderr=decoded_parts[1],
            )
        except OSError as exc:
            return CommandResult(
                argv=logical_argv,
                returncode=127,
                output=str(exc),
                elapsed=time.monotonic() - started,
                stderr=str(exc),
            )

    def _build_process(
        self, logical_argv: Tuple[str, ...]
    ) -> Tuple[List[str], Optional[Dict[str, str]]]:
        system_argv = list(logical_argv)
        if logical_argv[0] in SYSTEM_COMMANDS:
            system_argv[0] = f"/system/bin/{logical_argv[0]}"

        if self.kind == "direct":
            env = _android_environment()
            return system_argv, env

        if self.kind == "su":
            if not self.su_path:
                raise BackendError("Backend su chưa có đường dẫn su")
            remote = (
                f"PATH={shlex.quote(SYSTEM_PATH)} LD_PRELOAD= LD_LIBRARY_PATH= "
                f"exec {shlex.join(system_argv)}"
            )
            return [self.su_path, "-c", remote], os.environ.copy()

        if self.kind == "adb":
            if not self.adb_path:
                raise BackendError("Backend adb chưa có đường dẫn adb")
            command = [self.adb_path]
            if self.adb_serial:
                command.extend(["-s", self.adb_serial])
            remote = (
                f"PATH={shlex.quote(SYSTEM_PATH)} LD_PRELOAD= LD_LIBRARY_PATH= "
                f"exec {shlex.join(system_argv)}"
            )
            command.extend(["shell", remote])
            return command, os.environ.copy()

        if self.kind == "soft":
            executable = shutil.which(logical_argv[0])
            if not executable:
                executable = system_argv[0]
            return [executable, *logical_argv[1:]], os.environ.copy()

        raise BackendError(f"Backend không xác định: {self.kind}")


def _android_environment() -> Dict[str, str]:
    env = os.environ.copy()
    env.pop("LD_PRELOAD", None)
    env.pop("LD_LIBRARY_PATH", None)
    env["PATH"] = SYSTEM_PATH
    return env


def select_backend(requested: str, adb_serial: Optional[str]) -> AndroidBackend:
    if requested not in {"auto", "direct", "su", "adb", "soft"}:
        raise BackendError(f"Backend không hợp lệ: {requested}")

    uid = os.geteuid()
    if requested in {"auto", "direct"} and uid in {0, 2000}:
        return AndroidBackend("direct")
    if requested == "direct":
        raise BackendError(f"direct cần uid root/shell, uid hiện tại là {uid}")

    if requested in {"auto", "su"}:
        su_path = _find_su()
        if su_path and _probe_su(su_path):
            return AndroidBackend("su", su_path=su_path)
        if requested == "su":
            raise BackendError("Không lấy được quyền root bằng su -c")

    if requested in {"auto", "adb"}:
        adb_path = shutil.which("adb")
        if adb_path:
            serial = _select_adb_device(adb_path, adb_serial)
            if serial:
                return AndroidBackend("adb", adb_path=adb_path, adb_serial=serial)

    am_path = shutil.which("am")
    if requested in {"auto", "soft"} and (am_path or Path("/system/bin/am").exists()):
        return AndroidBackend("soft")
    raise BackendError("Không tìm được backend Android có thể sử dụng")


def _probe_su(su_path: str) -> bool:
    try:
        result = subprocess.run(
            [su_path, "-c", "/system/bin/id -u"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and "0" in result.stdout.split()


def _find_su() -> Optional[str]:
    candidates = [
        shutil.which("su"),
        "/system/bin/su",
        "/system/xbin/su",
        "/sbin/su",
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _select_adb_device(adb_path: str, requested_serial: Optional[str]) -> Optional[str]:
    try:
        result = subprocess.run(
            [adb_path, "devices"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    devices = []
    for line in result.stdout.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "device":
            devices.append(fields[0])
    if requested_serial:
        return requested_serial if requested_serial in devices else None
    return devices[0] if len(devices) == 1 else None


# ==========================================
# ROBLOX AUTH SYSTEM (XU LY COOKIE CHUAN XAC)
# ==========================================
class RobloxAuthSystem:
    """Xử lý xác thực, trích xuất Cookie thông minh và tạo Auth Ticket."""

    @staticmethod
    def extract_raw_cookie(line: str) -> str:
        s = str(line).strip().strip("'\"")
        match = re.search(r"(?i)\.ROBLOSECURITY\s*=\s*([^;\s]+)", s)
        if match:
            return match.group(1).strip()
        if "_|WARNING:" in s:
            match = re.search(r"(_\|WARNING:[^;\r\n]+)", s)
            if match:
                return match.group(1).strip()
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
    def get_auth_ticket(cls, raw_input: str) -> Tuple[bool, Optional[str], Optional[str], str]:
        extracted = cls.extract_raw_cookie(raw_input)
        if len(extracted) < 50:
            return False, None, None, "Định dạng cookie không hợp lệ hoặc quá ngắn"

        candidate_tokens = [extracted]
        if "_|WARNING:" in extracted:
            parts = extracted.split("|_")
            if len(parts) > 1:
                candidate_tokens.append(parts[-1].strip())
        else:
            candidate_tokens.append(
                f"_|WARNING:-DO-NOT-SHARE-THIS.--Sharing-this-will-allow-someone-to-log-into-your-account-and-rob-your-robox.--|_{extracted}"
            )

        last_error = ""
        for token in candidate_tokens:
            token = re.sub(r"\\([_.|\-])", r"\1", token).strip()
            
            # 1. Kiểm tra User Authenticated
            req_user = urllib.request.Request(
                "https://users.roblox.com/v1/users/authenticated",
                headers={
                    "Cookie": f".ROBLOSECURITY={token}",
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                },
            )
            username = None
            try:
                with urllib.request.urlopen(req_user, timeout=12, context=SSL_CONTEXT) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
                    username = data.get("name")
            except urllib.error.HTTPError as err:
                if err.code == 401:
                    last_error = "Cookie bị từ chối (HTTP 401)"
                elif err.code == 429:
                    last_error = "Rate Limit (HTTP 429)"
                else:
                    last_error = f"Lỗi HTTP {err.code}"
                continue
            except Exception as err:
                last_error = f"Lỗi kết nối: {err}"
                continue

            if not username:
                continue

            # 2. Lấy x-csrf-token
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
                with urllib.request.urlopen(req_csrf, timeout=12, context=SSL_CONTEXT) as resp:
                    csrf_token = resp.headers.get("x-csrf-token")
            except urllib.error.HTTPError as err:
                csrf_token = err.headers.get("x-csrf-token")
            except Exception as err:
                return False, username, None, f"Lỗi CSRF: {err}"

            if not csrf_token:
                return False, username, None, "Không lấy được x-csrf-token"

            # 3. Yêu cầu cấp Auth Ticket
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
            try:
                with urllib.request.urlopen(req_ticket, timeout=12, context=SSL_CONTEXT) as resp:
                    ticket = resp.headers.get("rbx-authentication-ticket")
                    if ticket:
                        return True, username, ticket.strip(), "OK"
                    body = resp.read().decode("utf-8", errors="replace")
                    try:
                        data = json.loads(body)
                        if "authenticationTicket" in data:
                            return True, username, str(data["authenticationTicket"]).strip(), "OK"
                    except Exception:
                        pass
            except Exception as err:
                return False, username, None, f"Lỗi Ticket: {err}"

        return False, None, None, (last_error or "Không thể xác thực cookie")


def ensure_cookie_file(path: Path) -> List[str]:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with path.open("w", encoding="utf-8") as f:
                f.write("# Dán danh sách Cookie Roblox vào đây (Mỗi dòng 1 Cookie hoặc định dạng tk:mk:cookie)\n")
    except Exception:
        pass

    lines = []
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        except Exception:
            lines = []
    return lines


def mask_username(username: Optional[str]) -> str:
    if not username or username.startswith("Unknown"):
        return "Unknown"
    s = str(username).strip()
    if len(s) <= 4:
        return "****" + s[-2:]
    visible_len = min(6, max(3, len(s) // 2))
    masked_len = len(s) - visible_len
    return ("*" * max(4, masked_len)) + s[-visible_len:]


def inject_direct_root_cookies(backend: AndroidBackend, package: str, raw_cookie: str, username: Optional[str] = None) -> None:
    """Tiêm Cookie vĩnh viễn vào SQLite WebView & SharedPreferences và bảo toàn phân quyền để app không bị logout."""
    if not backend.can_write_app_data:
        return

    token = RobloxAuthSystem.extract_raw_cookie(raw_cookie)
    full_session = (
        token if "_|WARNING:" in token else
        f"_|WARNING:-DO-NOT-SHARE-THIS.--Sharing-this-will-allow-someone-to-log-into-your-account-and-rob-your-robox.--|_{token}"
    )

    temp_db = "/sdcard/Download/tmp_cookies.db"
    try:
        if os.path.exists(temp_db):
            os.remove(temp_db)
        conn = sqlite3.connect(temp_db)
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS cookies (
            creation_utc INTEGER NOT NULL, host_key TEXT NOT NULL, top_frame_site_key TEXT NOT NULL,
            name TEXT NOT NULL, value TEXT NOT NULL, encrypted_value BLOB NOT NULL, path TEXT NOT NULL,
            expires_utc INTEGER NOT NULL, is_secure INTEGER NOT NULL, is_httponly INTEGER NOT NULL,
            last_access_utc INTEGER NOT NULL, has_expires INTEGER NOT NULL, is_persistent INTEGER NOT NULL,
            priority INTEGER NOT NULL, samesite INTEGER NOT NULL, source_scheme INTEGER NOT NULL,
            source_port INTEGER NOT NULL, is_same_party INTEGER NOT NULL, last_update_utc INTEGER NOT NULL
        );
        """)
        now_micro = int((time.time() + 11644473600) * 1000000)
        expire_micro = int((time.time() + 11644473600 + 630720000) * 1000000) # 20 năm sau
        
        # Tiêm vào cả domain gốc và subdomain
        domains = [".roblox.com", "www.roblox.com", "roblox.com", ".web.roblox.com"]
        for dom in domains:
            cur.execute("""
            INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                now_micro, dom, "", ".ROBLOSECURITY", full_session, b"", "/",
                expire_micro, 1, 1, now_micro, 1, 1, 1, 0, 2, 443, 0, now_micro
            ))
            cur.execute("""
            INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                now_micro, dom, "", "RBXSessionTracker", full_session, b"", "/",
                expire_micro, 1, 0, now_micro, 1, 1, 1, 0, 2, 443, 0, now_micro
            ))
        conn.commit()
        conn.close()
    except Exception:
        pass

    user_tag = f'<string name="username">{username}</string>' if username else ''

    script = f"""
pkg="{package}"
app_dir="/data/data/$pkg"
[ ! -d "$app_dir" ] && app_dir="/data/user/0/$pkg"
if [ -d "$app_dir" ]; then
    # Lấy UID/GID chính xác của App để tránh bị lỗi quyền (Permission Denied) khiến App tự logout
    owner=$(stat -c '%u:%g' "$app_dir" 2>/dev/null)
    [ -z "$owner" ] && owner="10000:10000"

    # Xóa file lock/journal cũ để SQLite nạp dữ liệu sạch
    for wdir in "$app_dir/app_webview" "$app_dir/app_webview/Default"; do
        mkdir -p "$wdir"
        rm -f "$wdir/Cookies-journal" "$wdir/Cookies-wal" "$wdir/Cookies-shm"
        if [ -f "{temp_db}" ]; then
            cp -f "{temp_db}" "$wdir/Cookies" 2>/dev/null
            chmod 660 "$wdir/Cookies" 2>/dev/null
            chown "$owner" "$wdir/Cookies" 2>/dev/null
        fi
    done

    # Cập nhật SharedPreferences lưu trữ phiên đăng nhập vĩnh viễn
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
        chmod 660 "$app_dir/shared_prefs/$xml" 2>/dev/null
        chown "$owner" "$app_dir/shared_prefs/$xml" 2>/dev/null
    done

    # Cấp toàn quyền Read/Write cho UID của App đối với toàn bộ thư mục dữ liệu
    chown -R "$owner" "$app_dir" 2>/dev/null
    chmod 771 "$app_dir" "$app_dir/app_webview" "$app_dir/app_webview/Default" "$app_dir/shared_prefs" "$app_dir/databases" 2>/dev/null
fi
rm -f "{temp_db}"
"""
    backend.run(["sh", "-c", script], timeout=12)


@dataclasses.dataclass(frozen=True)
class RobloxLaunchSpec:
    raw: str
    place_id: Optional[str]
    game_instance_id: Optional[str]
    link_code: Optional[str]
    access_code: Optional[str]

    @classmethod
    def parse(cls, raw: str) -> "RobloxLaunchSpec":
        clean = html.unescape(str(raw).strip().strip("'\""))
        for _ in range(2):
            decoded = urllib.parse.unquote(clean)
            if decoded == clean:
                break
            clean = decoded

        if clean.isdigit():
            return cls(clean, clean, None, None, None)

        parsed = urllib.parse.urlparse(clean)
        query = {
            key.lower(): values
            for key, values in urllib.parse.parse_qs(
                parsed.query, keep_blank_values=False
            ).items()
        }

        def first(*names: str) -> Optional[str]:
            for name in names:
                values = query.get(name.lower())
                if values and values[0]:
                    return values[0]
            return None

        place_id = first("placeId")
        game_instance_id = first("gameInstanceId", "jobId")
        link_code = first("privateServerLinkCode", "linkCode")
        access_code = first("accessCode")

        if not place_id:
            match = re.search(r"(?i)/(?:games|experiences)/(\d+)", clean)
            place_id = match.group(1) if match else None
        if not place_id:
            match = re.search(r"(?i)\bplaceid=(\d+)", clean)
            place_id = match.group(1) if match else None
        if not game_instance_id:
            match = re.search(
                r"(?i)\b(?:gameinstanceid|jobid)=([a-z0-9-]+)", clean
            )
            game_instance_id = match.group(1) if match else None
        if not link_code:
            match = re.search(
                r"(?i)\b(?:privateserverlinkcode|linkcode)=([a-z0-9_-]+)",
                clean,
            )
            link_code = match.group(1) if match else None
        if not access_code:
            match = re.search(r"(?i)\baccesscode=([a-z0-9_-]+)", clean)
            access_code = match.group(1) if match else None

        return cls(clean, place_id, game_instance_id, link_code, access_code)

    def is_valid(self) -> bool:
        return bool(self.place_id or _is_roblox_url(self.raw))

    def candidate_urls(self, ticket: Optional[str] = None) -> List[str]:
        candidates: List[str] = []
        if self.place_id:
            params: Dict[str, str] = {"placeId": self.place_id}
            if self.game_instance_id:
                params["gameInstanceId"] = self.game_instance_id
            elif self.access_code:
                params["accessCode"] = self.access_code
            elif self.link_code:
                params["linkCode"] = self.link_code
            if ticket:
                params["ticket"] = ticket
            encoded = urllib.parse.urlencode(params)
            candidates.append("roblox://" + encoded)
            candidates.append("roblox://experiences/start?" + encoded)
        if _is_roblox_url(self.raw):
            if ticket:
                sep = "&" if "?" in self.raw else "?"
                candidates.append(f"{self.raw}{sep}ticket={urllib.parse.quote(ticket)}")
            else:
                candidates.append(self.raw)
        return list(dict.fromkeys(candidates))


def _is_roblox_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    scheme = parsed.scheme.lower()
    if scheme in {"roblox", "roblox-player"}:
        return True
    if scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    return host == "roblox.com" or host.endswith(".roblox.com") or host == "ro.blox.com"


class AndroidController:
    KNOWN_ACTIVITIES = (
        "com.roblox.client.ActivityProtocolLaunch",
        "com.roblox.client.RobloxActivity",
        "com.roblox.client.startup.StartupActivity",
    )
    FAILURE_MARKERS = (
        "error:",
        "exception",
        "securityexception",
        "permission denial",
        "unable to resolve intent",
        "does not exist",
        "no activities found",
        "not allowed",
    )

    def __init__(
        self,
        backend: AndroidBackend,
        logger: logging.Logger,
        command_timeout: float,
    ) -> None:
        self.backend = backend
        self.logger = logger
        self.command_timeout = command_timeout

    @classmethod
    def command_accepted(cls, result: CommandResult) -> bool:
        lowered = result.output.lower()
        return result.ok and not any(marker in lowered for marker in cls.FAILURE_MARKERS)

    def preflight(self) -> Tuple[bool, str]:
        current_user = self.backend.run(["am", "get-current-user"], timeout=12)
        if self.command_accepted(current_user) and re.search(
            r"(?m)^\s*\d+\s*$", current_user.stdout or current_user.output
        ):
            return True, current_user.output
        help_result = self.backend.run(["am", "help"], timeout=12)
        return help_result.ok, help_result.output

    def force_stop(self, package: str) -> Tuple[bool, str]:
        if not self.backend.can_force_stop:
            return False, "backend soft không có quyền force-stop"
        result = self.backend.run(
            ["am", "force-stop", package], timeout=self.command_timeout
        )
        return self.command_accepted(result), result.output

    def start_lobby(
        self,
        package: str,
        *,
        ticket: Optional[str] = None,
        freeform: bool = False,
        bounds: Optional[str] = None,
    ) -> Tuple[bool, str]:
        options: List[str] = []
        if freeform and bounds:
            options.extend(["--windowingMode", "5", "--bounds", bounds])
        elif freeform:
            options.extend(["--windowingMode", "5"])

        if ticket:
            res = self.backend.run(
                ["am", "start", "-W", *options, "-a", "android.intent.action.VIEW", "-d", f"roblox://navigation/home?ticket={urllib.parse.quote(ticket)}", "-p", package],
                timeout=self.command_timeout,
            )
            if self.command_accepted(res):
                return True, res.output

        result = self.backend.run(
            [
                "am",
                "start",
                "-W",
                *options,
                "-a",
                "android.intent.action.MAIN",
                "-c",
                "android.intent.category.LAUNCHER",
                "-p",
                package,
            ],
            timeout=self.command_timeout,
        )
        return self.command_accepted(result), result.output

    def start_deep_link(
        self,
        package: str,
        spec: RobloxLaunchSpec,
        *,
        freeform: bool = False,
        bounds: Optional[str] = None,
        ticket: Optional[str] = None,
    ) -> Tuple[bool, str]:
        if not spec.is_valid():
            return False, "Link/Place ID không hợp lệ"

        option_variants: List[List[str]] = []
        if freeform and bounds:
            option_variants.append(["--windowingMode", "5", "--bounds", bounds])
        if freeform:
            option_variants.append(["--windowingMode", "5"])
        option_variants.append([])

        errors: List[str] = []
        urls = spec.candidate_urls(ticket=ticket)

        for url in urls:
            for options in option_variants:
                intents: List[List[str]] = []
                for act in self.KNOWN_ACTIVITIES:
                    intents.append([
                        "am", "start", "-W", *options,
                        "-a", "android.intent.action.VIEW",
                        "-d", url,
                        "-n", f"{package}/{act}",
                    ])
                intents.append([
                    "am", "start", "-W", *options,
                    "-a", "android.intent.action.VIEW",
                    "-d", url,
                    "-p", package,
                ])
                if ticket and spec.place_id:
                    intents.append([
                        "am", "start", "-W", *options,
                        "-a", "android.intent.action.VIEW",
                        "-d", url,
                        "-p", package,
                        "--es", "ticket", ticket,
                        "--es", "placeId", spec.place_id,
                    ])

                for argv in intents:
                    result = self.backend.run(argv, timeout=self.command_timeout)
                    if self.command_accepted(result):
                        return True, result.output or "OK"
                    errors.append(result.output or f"rc={result.returncode}")

        return False, " | ".join(_compact(item, 180) for item in errors[-3:])

    def list_packages(self) -> Tuple[List[str], str]:
        result = self.backend.run(["pm", "list", "packages"], timeout=30)
        if not self.command_accepted(result):
            return [], result.output
        packages = []
        for line in result.output.splitlines():
            if line.startswith("package:"):
                package = line.split(":", 1)[1].strip().split("=")[-1]
                if PACKAGE_RE.fullmatch(package):
                    packages.append(package)
        return sorted(set(packages)), ""

    def is_process_running(self, package: str) -> Tuple[bool, str]:
        process_result = self.backend.run(["pidof", package], timeout=10)
        process_running = self.command_accepted(process_result) and bool(
            process_result.stdout.strip()
        )
        if not process_running:
            return False, "Không thấy PID"

        activity_result = self.backend.run(
            ["dumpsys", "activity", "-p", package, "activities"], timeout=15
        )
        if not activity_result.ok:
            return True, process_result.output
        package_lower = package.lower()
        activity_markers = (
            "activityrecord{", "mresumedactivity", "topresumedactivity",
            "realactivity=", "origactivity=", "topactivity=", "task{",
        )
        task_running = any(
            package_lower in line.lower()
            and any(marker in line.lower() for marker in activity_markers)
            for line in activity_result.output.splitlines()
        )
        return (True, "OK") if task_running else (False, "Không còn Activity")


class RealtimeDashboardEngine:
    """Dashboard tự động xóa sạch màn hình mỗi giây, không tràn chữ, không lag."""

    def __init__(
        self,
        config: RejoinConfig,
        controller: AndroidController,
        logger: logging.Logger,
    ) -> None:
        self.config = config
        self.controller = controller
        self.logger = logger
        self.stop_requested = False
        self._ping_paths: Dict[str, str] = {}
        self.package_status: Dict[str, str] = {t.package: "Waiting..." for t in self.config.targets}
        self.package_users: Dict[str, str] = {t.package: "Loading..." for t in self.config.targets}
        self._resolve_usernames()

    def _resolve_usernames(self) -> None:
        cookies = ensure_cookie_file(DEFAULT_COOKIE_SOURCE_PATH)
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

    def _inject_ping_script_silently(self) -> None:
        lua_code = (
            "-- SieuVip Heartbeat Watchdog\n"
            "spawn(function()\n"
            "    while task.wait(15) do\n"
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
            check_cmd = f"[ -d '{edir}' ] && cp '{temp_file}' '{edir}/sv_heartbeat.lua' && chmod 777 '{edir}/sv_heartbeat.lua'"
            self.controller.backend.run(["sh", "-c", check_cmd], timeout=5)

        self.controller.backend.run(["sh", "-c", f"rm -f '{temp_file}'"], timeout=5)

    def _read_local_heartbeat(self, package: str) -> Tuple[Optional[float], str]:
        cached = self._ping_paths.get(package)
        if cached:
            res = self.controller.backend.run(["sh", "-c", f"stat -c %Y '{cached}' 2>/dev/null || cat '{cached}' 2>/dev/null"], timeout=5)
            txt = res.stdout.strip()
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
        res = self.controller.backend.run(["sh", "-c", cmd], timeout=6)
        lines = [l.strip() for l in res.stdout.splitlines() if l.strip()]
        if len(lines) >= 2:
            fpath, val = lines[0], lines[1]
            if val.isdigit():
                self._ping_paths[package] = fpath
                return float(val), "OK"

        return None, "No Heartbeat"

    def _render_ui(self) -> None:
        """Xóa sạch màn hình hoàn toàn mỗi giây, bảng gọn gàng vừa màn hình điện thoại."""
        sys.stdout.write("\033[2J\033[3J\033[H")
        sys.stdout.flush()

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
            elif "Waiting" in status:
                status_colored = f"{Colors.YELLOW}Rejoining...{Colors.RESET}"
            elif "Crash" in status or "Offline" in status:
                status_colored = f"{Colors.RED}Offline{Colors.RESET}"
            else:
                status_colored = f"{Colors.CYAN}{status[:18]}{Colors.RESET}"

            print(f" {Colors.CYAN}{display_pkg: <17}{Colors.RESET}│ {Colors.GREEN}{display_user: <14}{Colors.RESET}│ {status_colored}")

        print(f"{Colors.BLUE}{'─' * 56}{Colors.RESET}")
        print(f"{Colors.GRAY}Nhấn Ctrl+C để dừng và quay lại Menu.{Colors.RESET}")
        sys.stdout.flush()

    def _worker_loop(self) -> None:
        enabled = [t for t in self.config.targets if t.enabled]
        cookies = ensure_cookie_file(DEFAULT_COOKIE_SOURCE_PATH)

        while not self.stop_requested:
            for idx, target in enumerate(enabled):
                if self.stop_requested:
                    break

                pkg = target.package
                running, _ = self.controller.is_process_running(pkg)
                
                if not running:
                    self.package_status[pkg] = "Joining..."
                    spec = RobloxLaunchSpec.parse(target.link)
                    
                    ticket = None
                    raw_c = None
                    if cookies:
                        raw_c = cookies[idx % len(cookies)]
                        ok, user, tk, _ = RobloxAuthSystem.get_auth_ticket(raw_c)
                        if ok and tk:
                            ticket = tk
                            if user:
                                self.package_users[pkg] = mask_username(user)
                    
                    # Bảo đảm nạp cookie trước khi khởi chạy lại
                    if raw_c and self.controller.backend.can_write_app_data:
                        inject_direct_root_cookies(self.controller.backend, pkg, raw_c, self.package_users.get(pkg))

                    self.controller.start_deep_link(
                        pkg,
                        spec,
                        freeform=self.config.freeform or self.config.auto_arrange,
                        bounds=target.bounds,
                        ticket=ticket,
                    )
                    time.sleep(2.5)
                    self.package_status[pkg] = "Joined"
                else:
                    if self.config.health_check_method == "heartbeat":
                        ts, _ = self._read_local_heartbeat(pkg)
                        if ts is not None and (time.time() - ts) > self.config.health_check_timeout_seconds:
                            self.package_status[pkg] = "Waiting for switch"
                            if self.controller.backend.can_force_stop:
                                self.controller.force_stop(pkg)
                            time.sleep(1.0)
                            continue
                    self.package_status[pkg] = "Joined"

            time.sleep(4.0)

    def run(self) -> int:
        if not self.config.targets:
            raise ConfigError("Chưa cấu hình package nào. Vui lòng vào Mục 3 trước.")

        ok, detail = self.controller.preflight()
        if not ok:
            raise BackendError("Backend không thể chạy Activity Manager: " + _compact(detail))

        if self.config.health_check_method == "heartbeat":
            self._inject_ping_script_silently()

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


def _compact(value: str, limit: int = 300) -> str:
    compact = " ".join(str(value).split())
    return compact if len(compact) <= limit else compact[:limit - 1] + "…"


def _load_menu_config(path: Path) -> RejoinConfig:
    return load_config(path) if path.exists() else RejoinConfig(targets=[])


def _configure_menu_packages(
    config: RejoinConfig, config_path: Path, controller: AndroidController
) -> None:
    print("\n" + "=" * 50)
    print(f"{Colors.CYAN}TUỲ CHỌN PACKAGE ROBLOX{Colors.RESET}")
    print("1. Nhập tiền tố Package (Ví dụ: free -> lọc ra free.nokaA, free.xxx)")
    print("2. Tự động quét toàn bộ Package Roblox / Delta trên máy")
    print("=" * 50)
    
    sub = input("Chọn phương thức [1/2]: ").strip()
    
    print(f"\n{Colors.CYAN}[*] Đang quét danh sách package trên thiết bị...{Colors.RESET}")
    all_packages, error = controller.list_packages()
    if not all_packages:
        raise BackendError("Không thể quét package: " + _compact(error))

    selected: List[str] = []

    if sub == "1":
        prefix = input("\nNhập tiền tố package (Ví dụ: free / com / com.roblox): ").strip().lower().rstrip(".")
        if not prefix:
            raise ConfigError("Chưa nhập tiền tố package.")
        
        selected = [
            p for p in all_packages 
            if p.lower() == prefix or p.lower().startswith(prefix + ".") or p.lower().startswith(prefix)
        ]
        
        if not selected:
            raise ConfigError(f"Không tìm thấy package nào có tiền tố: '{prefix}'")
            
    else:
        keywords = ["roblox", "noka", "delta", "fluxus", "codex", "arceus", "spdm", "hydrogen", "trigon"]
        selected = [p for p in all_packages if any(k in p.lower() for k in keywords)]

        if not selected:
            print(f"{Colors.YELLOW}[!] Không tìm thấy package chứa từ khóa Roblox/Delta. Hiển thị danh sách gợi ý:{Colors.RESET}")
            user_apps = [p for p in all_packages if not p.startswith(("com.android", "com.google.android", "android"))]
            for idx, p in enumerate(user_apps[:15], 1):
                print(f" {idx}. {p}")
            choice = input("\nNhập số thứ tự hoặc tên package muốn chọn: ").strip()
            if choice.isdigit() and 1 <= int(choice) <= len(user_apps):
                selected = [user_apps[int(choice) - 1]]
            elif PACKAGE_RE.fullmatch(choice):
                selected = [choice]
            else:
                raise ConfigError("Lựa chọn không hợp lệ.")

    print(f"\n{Colors.GREEN}[+] Đã tìm thấy {len(selected)} package:{Colors.RESET}")
    for p in selected:
        print(f" - {Colors.BOLD}{p}{Colors.RESET}")

    config.targets = [
        TargetConfig(package=p, link=DEFAULT_BLOX_FRUITS_PLACE_ID, enabled=True)
        for p in selected
    ]
    save_config(config_path, config)
    print(f"\n{Colors.GREEN}[+] Đã lưu {len(config.targets)} package thành công!{Colors.RESET}")


def _configure_menu_links(config: RejoinConfig, config_path: Path) -> None:
    if not config.targets:
        raise ConfigError("Chưa chọn package. Vui lòng vào mục 3 trước.")
    raw_link = input("\nNhập Game ID / Link Server VIP [Enter = Blox Fruits]: ").strip()
    link = raw_link or DEFAULT_BLOX_FRUITS_PLACE_ID
    for target in config.targets:
        target.link = link
    save_config(config_path, config)
    print("\n[+] Đã áp dụng Server Link cho tất cả package.")


def _menu_login_cookie(
    config: RejoinConfig,
    controller: AndroidController,
    logger: logging.Logger,
) -> None:
    """Mục 5: Đăng nhập Cookie vào từng app và chỉ mở sảnh Roblox (Lobby/Home), không vào game."""
    if not config.targets:
        print(f"\n{Colors.YELLOW}[!] Chưa chọn package nào. Vui lòng vào mục 3 trước.{Colors.RESET}")
        return

    cookies = ensure_cookie_file(DEFAULT_COOKIE_SOURCE_PATH)
    if not cookies:
        print(f"\n{Colors.YELLOW}[*] File {DEFAULT_COOKIE_SOURCE_PATH} đã được tự động tạo nhưng đang trống.{Colors.RESET}")
        choice = input(f"{Colors.CYAN}Bạn có muốn dán trực tiếp Cookie tại đây không? [Y/N]: {Colors.RESET}").strip().lower()
        if choice in {"y", "yes"}:
            pasted = input(f"{Colors.MAGENTA}Nhập/Dán Cookie: {Colors.RESET}").strip()
            if pasted:
                try:
                    with DEFAULT_COOKIE_SOURCE_PATH.open("a", encoding="utf-8") as f:
                        f.write(f"\n{pasted}\n")
                    cookies = [pasted]
                    print(f"{Colors.GREEN}[+] Đã lưu cookie thành công vào {DEFAULT_COOKIE_SOURCE_PATH}!{Colors.RESET}")
                except Exception as e:
                    print(f"{Colors.RED}[!] Lỗi lưu cookie: {e}{Colors.RESET}")
                    return
            else:
                print(f"{Colors.RED}[!] Chưa nhập cookie.{Colors.RESET}")
                return
        else:
            print(f"{Colors.YELLOW}[*] Hãy dán cookie vào file: {DEFAULT_COOKIE_SOURCE_PATH} rồi thử lại.{Colors.RESET}")
            return

    enabled_targets = [t for t in config.targets if t.enabled]
    if not enabled_targets:
        enabled_targets = config.targets

    print(f"\n{Colors.CYAN}[*] Đang thực hiện đăng nhập Cookie vào SẢNH cho {len(enabled_targets)} package...{Colors.RESET}")
    for idx, target in enumerate(enabled_targets):
        raw_cookie = cookies[idx % len(cookies)]
        print(f"\n[*] Đang xử lý: {Colors.BOLD}{target.package}{Colors.RESET}")

        ok, user, ticket, msg = RobloxAuthSystem.get_auth_ticket(raw_cookie)
        if not ok:
            print(f"{Colors.RED}[-] {msg}{Colors.RESET}")
            continue

        print(f"{Colors.GREEN}[+] Tài khoản: {user} | Lấy Auth Ticket thành công!{Colors.RESET}")

        # 1. Tiêm SQLite WebView & SharedPreferences kèm phân quyền chính xác
        if controller.backend.can_write_app_data:
            print(f"{Colors.CYAN}[*] Đang nạp Cookie & Lưu phiên đăng nhập vĩnh viễn...{Colors.RESET}")
            inject_direct_root_cookies(controller.backend, target.package, raw_cookie, user)

        # 2. Force Stop để nạp dữ liệu sạch
        if controller.backend.can_force_stop:
            controller.force_stop(target.package)
            time.sleep(0.6)

        # 3. Mở app vào Sảnh
        opened, detail = controller.start_lobby(
            target.package,
            ticket=ticket,
            freeform=config.freeform or config.auto_arrange,
            bounds=target.bounds,
        )

        if opened:
            print(f"{Colors.GREEN}[+] Đã đăng nhập & mở Sảnh Roblox {target.package} thành công!{Colors.RESET}")
        else:
            print(f"{Colors.RED}[-] Lỗi khởi chạy: {_compact(detail)}{Colors.RESET}")
        time.sleep(1.2)

    print(f"\n{Colors.GREEN}{Colors.BOLD}[✓] Hoàn tất đăng nhập Cookie vào sảnh Roblox!{Colors.RESET}")


def _config_menu(config: RejoinConfig, config_path: Path) -> None:
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
            sec = input("Nhập số giây timeout [120]: ").strip()
            config.health_check_timeout_seconds = int(sec or "120")
        save_config(config_path, config)


def interactive_menu(
    config_path: Path,
    requested_backend: str,
    adb_serial: Optional[str],
    logger: logging.Logger,
) -> int:
    config = _load_menu_config(config_path)
    backend = select_backend(requested_backend, adb_serial)
    controller = AndroidController(backend, logger, config.command_timeout_seconds)

    ensure_cookie_file(DEFAULT_COOKIE_SOURCE_PATH)

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

        try:
            choice = input(f"\n{Colors.MAGENTA}Execute -> {Colors.RESET}").strip()
            if choice == "0":
                return 0
            if choice == "1":
                dashboard = RealtimeDashboardEngine(config, controller, logger)
                with SingleInstance(DEFAULT_LOCK_PATH), WakeLock(config.wake_lock, logger):
                    dashboard.run()
            elif choice == "2":
                _configure_menu_links(config, config_path)
            elif choice == "3":
                _configure_menu_packages(config, config_path, controller)
            elif choice == "4":
                for t in config.targets:
                    controller.start_lobby(t.package)
            elif choice == "5":
                _menu_login_cookie(config, controller, logger)
            elif choice == "13":
                _config_menu(config, config_path)
        except Exception as exc:
            logger.error("%s", exc)

        input(f"\n{Colors.YELLOW}Nhấn Enter để quay lại menu...{Colors.RESET}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Roblox Auto Rejoin Engine")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--backend", default="auto", choices=("auto", "direct", "su", "adb", "soft"))
    parser.add_argument("--adb-serial", help="ADB serial")
    parser.add_argument("--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("menu")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        argv = ["menu"]

    args = build_parser().parse_args(argv)
    logger = setup_logger(DEFAULT_LOG_PATH, verbose=args.verbose)
    return interactive_menu(args.config, args.backend, args.adb_serial, logger)


if __name__ == "__main__":
    raise SystemExit(main())
