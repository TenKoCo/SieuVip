#!/data/data/com.termux/files/usr/bin/python
"""SieuVip Roblox rejoin engine designed for Termux on Android."""

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
import urllib.error
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
HEALTH_POLL_SECONDS = 5.0
PACKAGE_RE = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+$")
BOUNDS_RE = re.compile(r"^\d+,\d+,\d+,\d+$")
SYSTEM_PATH = (
    "/product/bin:/apex/com.android.runtime/bin:/apex/com.android.art/bin:"
    "/system_ext/bin:/system/bin:/system/xbin:/odm/bin:/vendor/bin:/vendor/xbin:"
    "/data/data/com.termux/files/usr/bin"
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


class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    CYAN = "\033[36m"
    MAGENTA = "\033[35m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    BLUE = "\033[34m"


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
    health_check_method: str = "online"
    health_check_timeout_seconds: int = 180

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
            raw.get("health_check_method", "online")
        ).strip().lower()
        if health_check_method not in {"online", "heartbeat"}:
            health_check_method = "online"

        return cls(
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
                raw.get("health_check_timeout_seconds", 180), 15, 3600
            ),
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
            f"Chưa có cấu hình tại {path}. Hãy chọn mục 3 để thêm Package."
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Không đọc được config {path}: {exc}") from exc
    return RejoinConfig.from_dict(raw)


def save_config(path: Path, config: RejoinConfig) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(config.to_dict(), file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except OSError as exc:
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

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%H:%M:%S"))
    logger.addHandler(console)

    try:
        rotating = RotatingFileHandler(
            log_path,
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
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
            raise AppError("Đã có một tiến trình auto rejoin khác đang chạy!") from exc
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
            self.acquired = (result.returncode == 0)
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
            return f"direct root uid={os.geteuid()}"
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

    if requested in {"auto", "su"}:
        su_path = _find_su()
        if su_path and _probe_su(su_path):
            return AndroidBackend("su", su_path=su_path)

    if requested in {"auto", "adb"}:
        adb_path = shutil.which("adb")
        if adb_path:
            serial = _select_adb_device(adb_path, adb_serial)
            if serial:
                return AndroidBackend("adb", adb_path=adb_path, adb_serial=serial)

    am_path = shutil.which("am")
    if requested in {"auto", "soft"} and (am_path or Path("/system/bin/am").exists()):
        return AndroidBackend("soft")
    raise BackendError("Không tìm thấy quyền Root (su) hoặc backend Android hợp lệ!")


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


class RobloxAuth:
    """Handles Roblox Authentication Ticket exchange from .ROBLOSECURITY cookies."""

    @staticmethod
    def get_csrf_token(cookie: str) -> Optional[str]:
        req = urllib.request.Request(
            "https://auth.roblox.com/v1/authentication-ticket/",
            data=b"{}",
            headers={
                "Cookie": f".ROBLOSECURITY={cookie}",
                "Content-Type": "application/json",
                "User-Agent": "Roblox/Android",
                "Referer": "https://www.roblox.com/",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.headers.get("x-csrf-token")
        except urllib.error.HTTPError as exc:
            return exc.headers.get("x-csrf-token")
        except Exception:
            return None

    @staticmethod
    def generate_auth_ticket(cookie: str) -> Tuple[Optional[str], str]:
        csrf = RobloxAuth.get_csrf_token(cookie)
        if not csrf:
            return None, "Không lấy được CSRF Token từ Roblox API"

        req = urllib.request.Request(
            "https://auth.roblox.com/v1/authentication-ticket/",
            data=b"{}",
            headers={
                "Cookie": f".ROBLOSECURITY={cookie}",
                "Content-Type": "application/json",
                "User-Agent": "Roblox/Android",
                "Referer": "https://www.roblox.com/",
                "x-csrf-token": csrf,
                "RBXAuthenticationNegotiation": "1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                ticket = resp.headers.get("rbx-authentication-ticket")
                if ticket:
                    return ticket.strip(), "OK"
                try:
                    payload = json.loads(resp.read().decode())
                    if "ticket" in payload:
                        return str(payload["ticket"]).strip(), "OK"
                except Exception:
                    pass
                return None, "Roblox không trả về Auth Ticket"
        except urllib.error.HTTPError as exc:
            return None, f"Lỗi sinh Ticket HTTP {exc.code}"
        except Exception as exc:
            return None, f"Lỗi kết nối sinh Ticket: {exc}"


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
            match = re.search(r"(?i)\b(?:gameinstanceid|jobid)=([a-z0-9-]+)", clean)
            game_instance_id = match.group(1) if match else None
        if not link_code:
            match = re.search(r"(?i)\b(?:privateserverlinkcode|linkcode)=([a-z0-9_-]+)", clean)
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
            raw_target = self.raw
            if ticket:
                sep = "&" if "?" in raw_target else "?"
                raw_target += f"{sep}ticket={urllib.parse.quote(ticket)}"
            candidates.append(raw_target)
        if not candidates and ticket:
            candidates.append(f"roblox://navigation/home?ticket={urllib.parse.quote(ticket)}")
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


class CookieStore:
    @classmethod
    def normalize_record(cls, raw_record: str) -> str:
        value = str(raw_record).strip()
        lowered = value.lower()
        is_raw_cookie = value.startswith("_|WARNING:")
        is_cookie_header = lowered.startswith(".roblosecurity=") or lowered.startswith("cookie:")
        if not is_raw_cookie and not is_cookie_header:
            fields = value.split(":", 2)
            if len(fields) == 3 and len(fields[2].strip()) >= 50:
                value = fields[2].strip()
        return cls.normalize(value)

    @staticmethod
    def normalize(raw_cookie: str) -> str:
        value = str(raw_cookie).strip().strip("'\"")
        value = re.sub(r"\\([_.|\-])", r"\1", value)
        header_match = re.search(r"(?i)(?:^|[;\s])\.ROBLOSECURITY\s*=\s*([^;\s]+)", value)
        if header_match:
            value = header_match.group(1).strip()
        if any(character in value for character in ("\x00", "\r", "\n")):
            raise ConfigError("Cookie chứa ký tự điều khiển không hợp lệ")
        if len(value) < 50:
            raise ConfigError("Cookie quá ngắn hoặc không đúng định dạng")
        return value

    @classmethod
    def load(cls, path: Path, packages: Sequence[str]) -> Dict[str, str]:
        if not path.exists():
            raise ConfigError(f"Không tìm thấy file: {path}. Hãy tạo file và dán cookie vào!")
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"Không đọc được file cookie {path}: {exc}") from exc

        mapping: Dict[str, str] = {}
        try:
            decoded = json.loads(content)
        except json.JSONDecodeError:
            decoded = None

        if isinstance(decoded, dict):
            raw_mapping = decoded.get("cookies", decoded)
            if isinstance(raw_mapping, dict):
                for package, cookie in raw_mapping.items():
                    package_text = str(package).strip()
                    if PACKAGE_RE.fullmatch(package_text) and isinstance(cookie, str):
                        mapping[package_text] = cls.normalize_record(cookie)
        else:
            lines = [
                line.strip()
                for line in content.splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
            for package, cookie in zip(packages, lines):
                mapping[package] = cls.normalize_record(cookie)

        if not mapping:
            raise ConfigError(f"Không có cookie hợp lệ trong {path}")
        return mapping


def validate_roblox_cookie(cookie: str, timeout: float = 10.0) -> Tuple[bool, str]:
    try:
        normalized = CookieStore.normalize(cookie)
    except ConfigError as exc:
        return False, str(exc)
    request = urllib.request.Request(
        "https://users.roblox.com/v1/users/authenticated",
        headers={
            "Cookie": f".ROBLOSECURITY={normalized}",
            "Accept": "application/json",
            "User-Agent": "SieuVipPro-Rejoin/1.0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            return False, "Cookie bị Roblox từ chối hoặc đã hết hạn"
        return False, f"Roblox trả về HTTP {exc.code} khi kiểm tra cookie"
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return False, "Không kết nối được Roblox để kiểm tra cookie"
    user_id = payload.get("id") if isinstance(payload, dict) else None
    username = payload.get("name") if isinstance(payload, dict) else None
    if not user_id or not username:
        return False, "Roblox không trả về tài khoản cho cookie này"
    masked = str(username)
    masked = ("*" * max(0, len(masked) - 3)) + masked[-3:]
    return True, f"Cookie hợp lệ cho tài khoản {masked}"


class CookieInstaller:
    """Injects session and uses Auth Ticket handshake."""

    def __init__(
        self,
        backend: AndroidBackend,
        logger: logging.Logger,
        command_timeout: float,
    ) -> None:
        self.backend = backend
        self.logger = logger
        self.command_timeout = command_timeout

    def apply(self, package: str, cookie: str) -> Tuple[bool, str]:
        if not self.backend.can_write_app_data:
            return False, "Inject cookie cần backend root/su"
        if not PACKAGE_RE.fullmatch(package):
            return False, "Package không hợp lệ"
        try:
            normalized = CookieStore.normalize(cookie)
        except ConfigError as exc:
            return False, str(exc)

        c = normalized
        if not c.startswith("_|WARNING:-DO-NOT-SHARE-THIS"):
            c = "_|WARNING:-DO-NOT-SHARE-THIS.--Sharing-this-will-allow-someone-to-log-into-your-account-and-rob-your-robox.--|_" + c

        script = f"""
pkg="{package}"
app_dir="/data/data/$pkg"
if [ ! -d "$app_dir" ] && [ -d "/data/user/0/$pkg" ]; then
    app_dir="/data/user/0/$pkg"
fi

am force-stop "$pkg" 2>/dev/null
killall -9 "$pkg" 2>/dev/null

owner=$(stat -c '%u:%g' "$app_dir" 2>/dev/null)
if [ -z "$owner" ]; then
    owner="1000:1000"
fi

rm -rf "$app_dir/app_webview" "$app_dir/cache" "$app_dir/code_cache" "$app_dir/databases" "$app_dir/files/GuestData" 2>/dev/null
mkdir -p "$app_dir/shared_prefs"

cat << 'EOF' > "$app_dir/shared_prefs/com.roblox.client_preferences.xml"
<?xml version='1.0' encoding='utf-8' standalone='yes' ?>
<map>
    <string name="RBXSession">{c}</string>
    <string name="RBXSessionToken">{c}</string>
</map>
EOF

chown -R "$owner" "$app_dir/shared_prefs"
chmod 771 "$app_dir/shared_prefs"
chmod 660 "$app_dir/shared_prefs/com.roblox.client_preferences.xml"

if [ -x /system/bin/restorecon ]; then
    /system/bin/restorecon -RF "$app_dir" >/dev/null 2>&1 || true
fi
"""
        write_result = self.backend.run(["sh", "-c", script], timeout=self.command_timeout)
        if not write_result.ok:
            return False, f"Lỗi inject: {_compact(write_result.output)}"
        return True, "Đã chuẩn bị data & SELinux thành công"


class AndroidController:
    PROTOCOL_ACTIVITY = "com.roblox.client.ActivityProtocolLaunch"
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
        help_text = help_result.output.lower()
        if "activity manager" in help_text and not any(
            m in help_text for m in ("securityexception", "permission denial", "not allowed")
        ):
            return True, help_result.output
        return False, current_user.output or help_result.output

    def force_stop(self, package: str) -> Tuple[bool, str]:
        result = self.backend.run(["am", "force-stop", package], timeout=self.command_timeout)
        return self.command_accepted(result), result.output

    def start_lobby(self, package: str, ticket: Optional[str] = None) -> Tuple[bool, str]:
        if ticket:
            spec = RobloxLaunchSpec.parse(DEFAULT_BLOX_FRUITS_PLACE_ID)
            return self.start_deep_link(package, spec, freeform=False, bounds=None, ticket=ticket)

        result = self.backend.run(
            [
                "am",
                "start",
                "-W",
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
        freeform: bool,
        bounds: Optional[str],
        ticket: Optional[str] = None,
    ) -> Tuple[bool, str]:
        if not spec.is_valid() and not ticket:
            return False, "Link/Place ID không hợp lệ"

        option_variants: List[List[str]] = []
        if freeform and bounds:
            option_variants.append(["--windowingMode", "5", "--bounds", bounds])
        if freeform:
            option_variants.append(["--windowingMode", "5"])
        option_variants.append([])

        errors: List[str] = []
        component = f"{package}/{self.PROTOCOL_ACTIVITY}"
        for url in spec.candidate_urls(ticket=ticket):
            for options in option_variants:
                intents = (
                    [
                        "am",
                        "start",
                        "-W",
                        *options,
                        "-a",
                        "android.intent.action.VIEW",
                        "-d",
                        url,
                        "-n",
                        component,
                    ],
                    [
                        "am",
                        "start",
                        "-W",
                        *options,
                        "-a",
                        "android.intent.action.VIEW",
                        "-d",
                        url,
                        "-p",
                        package,
                    ],
                )
                for argv in intents:
                    result = self.backend.run(argv, timeout=self.command_timeout)
                    if self.command_accepted(result):
                        return True, result.output or "Android đã nhận intent"
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
            return False, process_result.output or "Không thấy PID"

        activity_result = self.backend.run(
            ["dumpsys", "activity", "-p", package, "activities"], timeout=15
        )
        if not activity_result.ok:
            return True, process_result.output
        package_lower = package.lower()
        activity_markers = (
            "activityrecord{",
            "mresumedactivity",
            "topresumedactivity",
            "realactivity=",
            "origactivity=",
            "topactivity=",
            "task{",
        )
        task_running = any(
            package_lower in line.lower()
            and any(marker in line.lower() for marker in activity_markers)
            for line in activity_result.output.splitlines()
        )
        return (True, process_result.output) if task_running else (False, "Process treo/không có Activity")

    def get_screen_size(self) -> Tuple[int, int]:
        result = self.backend.run(["wm", "size"], timeout=10)
        matches = re.findall(r"(?i)(\d+)x(\d+)", result.output)
        return tuple(map(int, matches[-1])) if matches else (720, 1280)

    def randomize_android_id(self) -> Tuple[bool, str]:
        value = os.urandom(8).hex()
        result = self.backend.run(["settings", "put", "secure", "android_id", value], timeout=15)
        return self.command_accepted(result), result.output


class RejoinEngine:
    def __init__(
        self,
        config: RejoinConfig,
        controller: AndroidController,
        logger: logging.Logger,
        cookie_installer: Optional[CookieInstaller] = None,
        cookies: Optional[Dict[str, str]] = None,
    ) -> None:
        self.config = config
        self.controller = controller
        self.logger = logger
        self.cookie_installer = cookie_installer
        self.cookies = cookies or {}
        self.stop_requested = False
        self._ping_paths: Dict[str, str] = {}

    def request_stop(self, signum: int, frame: Any) -> None:
        del frame
        if not self.stop_requested:
            self.logger.info("Nhận signal %s; dừng an toàn...", signum)
        self.stop_requested = True

    def _inject_ping_script_silently(self) -> None:
        check_file = f"{random.randint(100000, 999999)}.main"
        lua_code = (
            "spawn(function()\n"
            "    while task.wait(30) do\n"
            "        pcall(function()\n"
            f'            writefile("{check_file}", tostring(os.time()))\n'
            "        end)\n"
            "    end\n"
            "end)"
        )
        temp_file = "/sdcard/Download/sv_tmp_ping.lua"
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                f.write(lua_code)
        except Exception:
            return

        target_dirs = [
            "/sdcard/Delta/autoexecute",
            "/sdcard/Fluxus/autoexecute",
            "/sdcard/Codex/autoexecute",
            "/sdcard/spdm/autoexecute",
            "/sdcard/Hydrogen/autoexecute",
            "/sdcard/Trigon/autoexecute",
        ]
        for target in target_dirs:
            check_cmd = f"[ -d {target} ] && echo EXISTS"
            res = self.controller.backend.run(["sh", "-c", check_cmd], timeout=5)
            if "EXISTS" in res.output:
                self.controller.backend.run(["sh", "-c", f"rm -f {target}/SieuVip_Ping_*.lua"], timeout=5)
                dest = f"{target}/SieuVip_Ping_{random.randint(10,99)}.lua"
                self.controller.backend.run(["sh", "-c", f"cp {temp_file} {dest} && chmod 777 {dest}"], timeout=5)

        self.controller.backend.run(["sh", "-c", f"rm -f {temp_file}"], timeout=5)

    def _read_local_heartbeat(self, package: str) -> Tuple[Optional[float], str]:
        path = self._ping_paths.get(package)
        if not path:
            search_paths = [
                f"/sdcard/Android/data/{package}",
                "/sdcard/Delta/workspace",
                "/sdcard/Fluxus/workspace",
                "/sdcard/Codex/workspace",
                "/sdcard/spdm/workspace",
                "/sdcard/Hydrogen/workspace",
                "/sdcard/Trigon/workspace",
            ]
            for sp in search_paths:
                find_cmd = f"find {sp} -name '*.main' -type f 2>/dev/null | head -n 1"
                res = self.controller.backend.run(["sh", "-c", find_cmd], timeout=8)
                if res.ok and res.stdout.strip():
                    path = res.stdout.strip()
                    self._ping_paths[package] = path
                    break

        if path:
            res = self.controller.backend.run(["sh", "-c", f"stat -c %Y {shlex.quote(path)}"], timeout=8)
            if res.ok and res.stdout.strip().isdigit():
                return float(res.stdout.strip()), "OK"
            return None, "File heartbeat không phản hồi"
        return None, "Chưa tìm thấy file .main"

    def run(self, once: bool = False) -> int:
        enabled = [target for target in self.config.targets if target.enabled]
        if not enabled:
            raise ConfigError("Không có package nào đang được chọn!")

        ok, detail = self.controller.preflight()
        if not ok:
            raise BackendError("Không chạy được Activity Manager: " + _compact(detail))

        if self.config.health_check_method == "heartbeat":
            self._inject_ping_script_silently()

        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)
        cycle = 0

        while not self.stop_requested:
            cycle += 1
            started = time.monotonic()
            self.logger.info("========== Chu kỳ %d ==========", cycle)

            if self.config.randomize_android_id_each_cycle:
                changed, _ = self.controller.randomize_android_id()
                if changed:
                    self.logger.info("Đã đổi Android ID ngẫu nhiên")

            bounds = self._resolve_bounds(enabled)
            succeeded = 0
            for index, target in enumerate(enabled):
                if self.stop_requested:
                    break
                if self._run_target(target, bounds[index], force_rejoin=(cycle == 1)):
                    succeeded += 1
                if index + 1 < len(enabled):
                    self._sleep(self.config.between_apps_seconds)

            self.logger.info(
                "Chu kỳ %d hoàn tất: %d/%d app ổn định (%.1fs)",
                cycle,
                succeeded,
                len(enabled),
                time.monotonic() - started,
            )
            if once or self.stop_requested:
                break

            deadline = time.monotonic() + (
                self.config.interval_seconds if self.config.interval_seconds > 0 else 86400 * 365
            )
            while time.monotonic() < deadline and not self.stop_requested:
                self._sleep(HEALTH_POLL_SECONDS)
                for index, target in enumerate(enabled):
                    healthy, detail = self._target_health_once(target)
                    if not healthy:
                        self.logger.warning(
                            "[%s] Phát hiện bất thường (%s) -> Tự động phục hồi!",
                            target.package,
                            detail,
                        )
                        self._run_target(target, bounds[index], force_rejoin=False)

        self.logger.info("Engine đã dừng.")
        return 0

    def _run_target(
        self,
        target: TargetConfig,
        bounds: Optional[str],
        *,
        force_rejoin: bool = False,
    ) -> bool:
        spec = RobloxLaunchSpec.parse(target.link)
        if not spec.is_valid():
            self.logger.error("[%s] Link không hợp lệ", target.package)
            return False

        if not force_rejoin:
            healthy, health_detail = self._target_health_once(target)
            if healthy:
                return True
            self.logger.info("[%s] Bắt đầu khởi động lại (%s)", target.package, health_detail)

        self.controller.force_stop(target.package)
        self._sleep(0.5)

        ticket = None
        cookie = self.cookies.get(target.package)
        if self.config.auto_login_cookies and cookie:
            if self.cookie_installer:
                self.cookie_installer.apply(target.package, cookie)
            t_val, msg = RobloxAuth.generate_auth_ticket(cookie)
            if t_val:
                ticket = t_val
                self.logger.info("[%s] Đã sinh Auth Ticket đăng nhập thành công", target.package)
            else:
                self.logger.warning("[%s] Không sinh được Ticket (%s)", target.package, msg)

        if force_rejoin:
            opened, _ = self.controller.start_lobby(target.package)
            if opened:
                self._sleep(self.config.warmup_seconds)
                self.controller.force_stop(target.package)
                self._sleep(0.5)

        attempts = self.config.retries + 1
        for attempt in range(1, attempts + 1):
            if self.stop_requested:
                return False
            if attempt > 1:
                self.controller.force_stop(target.package)
                self._sleep(0.5)

            join_started = time.time()
            accepted, detail = self.controller.start_deep_link(
                target.package,
                spec,
                freeform=self.config.freeform or self.config.auto_arrange,
                bounds=bounds,
                ticket=ticket,
            )
            if accepted:
                healthy, health_detail = self._wait_for_target_health(target, join_started)
                if healthy:
                    self.logger.info("[%s] Join thành công (Lần %d/%d)", target.package, attempt, attempts)
                    return True
                detail = health_detail

            self.logger.warning("[%s] Join lần %d thất bại: %s", target.package, attempt, _compact(detail))
            if attempt < attempts:
                self._sleep(self.config.retry_backoff_seconds * attempt)
        return False

    def _target_health_once(
        self,
        target: TargetConfig,
        not_before: Optional[float] = None,
    ) -> Tuple[bool, str]:
        if self.config.health_check_method == "online":
            running, detail = self.controller.is_process_running(target.package)
            return (True, "Running") if running else (False, detail)

        timestamp, detail = self._read_local_heartbeat(target.package)
        if timestamp is None:
            return False, detail
        now = time.time()
        age = max(0.0, now - timestamp)
        if age > self.config.health_check_timeout_seconds:
            old_path = self._ping_paths.get(target.package)
            if old_path:
                self.controller.backend.run(["sh", "-c", f"rm -f {shlex.quote(old_path)}"])
                del self._ping_paths[target.package]
            return False, f"Heartbeat đóng băng {age:.0f}s"
        return True, "Heartbeat active"

    def _wait_for_target_health(
        self,
        target: TargetConfig,
        join_started: float,
    ) -> Tuple[bool, str]:
        deadline = time.monotonic() + max(0, self.config.health_check_timeout_seconds)
        last_detail = "Chưa có tín hiệu"
        while not self.stop_requested:
            healthy, last_detail = self._target_health_once(target, not_before=join_started)
            if healthy:
                return True, last_detail
            if time.monotonic() >= deadline:
                break
            self._sleep(HEALTH_POLL_SECONDS)
        return False, last_detail

    def _resolve_bounds(self, targets: List[TargetConfig]) -> List[Optional[str]]:
        configured = [target.bounds for target in targets]
        if not self.config.auto_arrange and not self.config.freeform:
            return configured

        width, height = self.controller.get_screen_size()
        count = max(1, len(targets))
        columns = min(count, max(1, min(6, width // 220)))
        rows = math.ceil(len(targets) / columns)
        cell_width = width // columns
        cell_height = height // max(1, rows)
        margin = max(4, min(16, width // 200))
        responsive_width = max(1, cell_width - (margin * 2))
        preferred_height = max(320, int(responsive_width * 1.65))

        if not self.config.auto_arrange:
            window_height = min(height - (margin * 2), preferred_height)
            cascade = max(18, width // 50)
            sorted_bounds = []
            for index, target in enumerate(targets):
                if target.bounds:
                    sorted_bounds.append(target.bounds)
                    continue
                left = margin + min(max(0, width - responsive_width - margin), index * cascade)
                top = margin + min(max(0, height - window_height - margin), index * cascade)
                right = min(width, left + responsive_width)
                bottom = min(height, top + window_height)
                sorted_bounds.append(f"{left},{top},{right},{bottom}")
            return sorted_bounds

        result = []
        for index, target in enumerate(targets):
            if target.bounds:
                result.append(target.bounds)
                continue
            col, row = index % columns, index // columns
            left = (col * cell_width) + margin
            top = (row * cell_height) + margin
            right = ((col + 1) * cell_width if col < columns - 1 else width) - margin
            bottom = min(((row + 1) * cell_height if row < rows - 1 else height) - margin, top + preferred_height)
            result.append(f"{left},{top},{right},{bottom}")
        return result

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while not self.stop_requested:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.25, remaining))


def _compact(value: str, limit: int = 300) -> str:
    compact = " ".join(str(value).split())
    return compact if len(compact) <= limit else compact[:limit - 1] + "…"


def _load_menu_config(path: Path) -> RejoinConfig:
    return load_config(path) if path.exists() else RejoinConfig(targets=[])


def match_package_prefix(packages: Sequence[str], raw_prefix: str) -> List[str]:
    prefix = raw_prefix.strip().lower().rstrip(".")
    if not prefix:
        raise ConfigError("Chưa nhập tiền tố package")
    return [p for p in packages if p.lower() == prefix or p.lower().startswith(prefix + ".")]


def _configure_menu_packages(
    config: RejoinConfig, config_path: Path, controller: AndroidController
) -> None:
    packages, error = controller.list_packages()
    if not packages:
        raise BackendError("Không quét được package: " + _compact(error))
    prefix = input("\nNhập tên đầu package (ví dụ: com hoặc com.roblox): ").strip()
    selected = match_package_prefix(packages, prefix)
    if not selected:
        raise ConfigError(f"Không tìm thấy package phù hợp với: {prefix}")

    config.targets = [
        TargetConfig(package=p, link=DEFAULT_BLOX_FRUITS_PLACE_ID, enabled=True)
        for p in selected
    ]
    save_config(config_path, config)
    print(f"\n[+] Đã chọn {len(config.targets)} packages thành công.")


def _configure_menu_links(config: RejoinConfig, config_path: Path) -> None:
    if not config.targets:
        raise ConfigError("Chưa chọn package. Vui lòng vào mục 3 trước.")
    raw_link = input("\nNhập Game ID / Server VIP [Enter = Blox Fruits]: ").strip()
    link = raw_link or DEFAULT_BLOX_FRUITS_PLACE_ID
    for target in config.targets:
        target.link = link
    save_config(config_path, config)
    print("\n[+] Đã áp dụng Server Link cho tất cả package.")


def _login_cookie_menu(
    config: RejoinConfig,
    controller: AndroidController,
    backend: AndroidBackend,
    cookie_path: Path,
    logger: logging.Logger,
) -> int:
    if not config.targets:
        raise ConfigError("Chưa có package nào được chọn. Hãy chọn ở mục 3.")
    packages = [t.package for t in config.targets]
    cookies = CookieStore.load(cookie_path, packages)
    installer = CookieInstaller(backend, logger, config.command_timeout_seconds)
    succeeded = 0
    for target in config.targets:
        c = cookies.get(target.package)
        if not c:
            continue
        valid, val_msg = validate_roblox_cookie(c)
        if not valid:
            logger.error("[%s] Cookie lỗi: %s", target.package, val_msg)
            continue
        logger.info("[%s] %s", target.package, val_msg)

        ticket, t_msg = RobloxAuth.generate_auth_ticket(c)
        if not ticket:
            logger.error("[%s] Không sinh được Auth Ticket: %s", target.package, t_msg)
            continue

        installer.apply(target.package, c)
        logger.info("[%s] Đang mở app và truyền Auth Ticket...", target.package)
        spec = RobloxLaunchSpec.parse(target.link)
        ok, _ = controller.start_deep_link(target.package, spec, freeform=False, bounds=None, ticket=ticket)
        if ok:
            succeeded += 1
            time.sleep(2.0)
    logger.info("Đã hoàn tất đăng nhập cho %d/%d package", succeeded, len(config.targets))
    return 0


def _config_menu(config: RejoinConfig, config_path: Path) -> None:
    while True:
        print("\033[2J\033[H", end="")
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
            sec = input("Nhập số giây timeout [180]: ").strip()
            config.health_check_timeout_seconds = int(sec or "180")
        save_config(config_path, config)


def interactive_menu(
    config_path: Path,
    cookie_source: Path,
    requested_backend: str,
    adb_serial: Optional[str],
    logger: logging.Logger,
) -> int:
    config = _load_menu_config(config_path)
    backend = select_backend(requested_backend, adb_serial)
    controller = AndroidController(backend, logger, config.command_timeout_seconds)

    while True:
        print("\033[2J\033[H", end="")
        print(f"{' '*18}⚡ {Colors.CYAN}{Colors.BOLD}SieuVipPro Dashboard{Colors.RESET}\n")
        print("┌──────┬────────────────────────────────────────────────────────┐")
        print(f"│ {Colors.MAGENTA}   1{Colors.RESET}  │ {Colors.CYAN}Start Auto Rejoin Engine                               {Colors.RESET}│")
        print(f"│ {Colors.MAGENTA}   2{Colors.RESET}  │ {Colors.CYAN}Nhập Game ID / Link Server VIP                         {Colors.RESET}│")
        print(f"│ {Colors.MAGENTA}   3{Colors.RESET}  │ {Colors.CYAN}Chọn Package Roblox để chạy                            {Colors.RESET}│")
        print(f"│ {Colors.MAGENTA}   4{Colors.RESET}  │ {Colors.CYAN}Mở tất cả App lên nền (Warm-up)                        {Colors.RESET}│")
        print(f"│ {Colors.MAGENTA}   5{Colors.RESET}  │ {Colors.CYAN}Login Cookie qua Root (Auth Ticket Engine)             {Colors.RESET}│")
        print(f"│ {Colors.MAGENTA}  13{Colors.RESET}  │ {Colors.GREEN}Cấu hình Nâng cao (Grid / Heartbeat Watchdog)          {Colors.RESET}│")
        print(f"│ {Colors.MAGENTA}   0{Colors.RESET}  │ {Colors.RED}Thoát Hệ Thống                                         {Colors.RESET}│")
        print("└──────┴────────────────────────────────────────────────────────┘")

        try:
            choice = input(f"\n{Colors.MAGENTA}Execute -> {Colors.RESET}").strip()
            if choice == "0":
                return 0
            if choice == "1":
                engine = RejoinEngine(config, controller, logger)
                with SingleInstance(DEFAULT_LOCK_PATH), WakeLock(config.wake_lock, logger):
                    engine.run(once=False)
            elif choice == "2":
                _configure_menu_links(config, config_path)
            elif choice == "3":
                _configure_menu_packages(config, config_path, controller)
            elif choice == "4":
                for t in config.targets:
                    controller.start_lobby(t.package)
            elif choice == "5":
                _login_cookie_menu(config, controller, backend, cookie_source, logger)
            elif choice == "13":
                _config_menu(config, config_path)
        except Exception as exc:
            logger.error("%s", exc)

        input(f"\n{Colors.YELLOW}Nhấn Enter để quay lại menu...{Colors.RESET}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Roblox Auto Rejoin Engine")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--cookies", type=Path, default=DEFAULT_COOKIE_SOURCE_PATH)
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
    return interactive_menu(args.config, args.cookies, args.backend, args.adb_serial, logger)


if __name__ == "__main__":
    raise SystemExit(main())
