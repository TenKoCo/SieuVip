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
import xml.etree.ElementTree as ElementTree
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import fcntl  # Chỉ có trên Linux/Android; luôn tồn tại trong Termux.
except ImportError:  # Cho phép chạy unit test/parse config trên Windows.
    fcntl = None  # type: ignore[assignment]


APP_NAME = "sieuvip-rejoin"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / APP_NAME / "config.json"
DEFAULT_COOKIE_PATH = Path.home() / ".config" / APP_NAME / "cookies.json"
DEFAULT_LOG_PATH = Path.home() / ".local" / "state" / APP_NAME / "rejoin.log"
DEFAULT_LOCK_PATH = Path.home() / ".local" / "state" / APP_NAME / "rejoin.lock"
DEFAULT_COOKIE_SOURCE_PATH = Path("/sdcard/Download/cookie.txt")
DEFAULT_BLOX_FRUITS_PLACE_ID = "2753915549"
HEALTH_POLL_SECONDS = 5.0
PACKAGE_RE = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+$")
BOUNDS_RE = re.compile(r"^\d+,\d+,\d+,\d+$")
SYSTEM_PATH = (
    "/product/bin:/apex/com.android.runtime/bin:/apex/com.android.art/bin:"
    "/system_ext/bin:/system/bin:/system/xbin:/odm/bin:/vendor/bin:/vendor/xbin"
)
SYSTEM_COMMANDS = {
    "am",
    "cmd",
    "getprop",
    "id",
    "monkey",
    "pidof",
    "pm",
    "settings",
    "wm",
}


class AppError(RuntimeError):
    pass


class ConfigError(AppError):
    pass


class BackendError(AppError):
    pass


def validate_heartbeat_url(value: str) -> str:
    """Validate an HTTPS heartbeat URL, optionally templated by package."""
    clean = str(value).strip()
    if not clean:
        raise ConfigError("Bạn chưa nhập URL heartbeat")
    unknown_fields = re.findall(r"\{([^{}]+)\}", clean)
    if any(field != "package" for field in unknown_fields):
        raise ConfigError("Heartbeat URL chỉ hỗ trợ biến {package}")
    rendered = clean.replace("{package}", "com.roblox.client")
    parsed = urllib.parse.urlparse(rendered)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ConfigError("Heartbeat URL phải là địa chỉ HTTPS hợp lệ")
    if parsed.username or parsed.password:
        raise ConfigError("Không đặt tài khoản/mật khẩu trong heartbeat URL")
    return clean


def heartbeat_url_for_package(template: str, package: str) -> str:
    validated = validate_heartbeat_url(template)
    encoded = urllib.parse.quote(package, safe="")
    return validated.replace("{package}", encoded)


def fetch_heartbeat_timestamp(
    template: str,
    package: str,
    timeout: float = 8.0,
) -> Tuple[Optional[float], str]:
    """Read a Unix timestamp from a small, authorized HTTPS endpoint."""
    try:
        url = heartbeat_url_for_package(template, package)
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json, text/plain",
                "User-Agent": "SieuVipRejoin/1.0",
            },
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=max(1.0, timeout)) as response:
            payload = response.read(65537)
        if len(payload) > 65536:
            return None, "Phản hồi heartbeat vượt quá 64 KiB"
        text = payload.decode("utf-8", errors="strict").strip()
        try:
            parsed: Any = json.loads(text)
        except json.JSONDecodeError:
            parsed = text
        if isinstance(parsed, dict):
            timestamp_value = next(
                (
                    parsed[key]
                    for key in ("timestamp", "last_seen", "lastSeen")
                    if key in parsed
                ),
                None,
            )
        else:
            timestamp_value = parsed
        timestamp = float(timestamp_value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000.0
        if not math.isfinite(timestamp) or timestamp <= 0:
            raise ValueError("timestamp ngoài phạm vi")
        return timestamp, "OK"
    except (ConfigError, UnicodeError, TypeError, ValueError) as exc:
        return None, f"Heartbeat không hợp lệ: {exc}"
    except urllib.error.HTTPError as exc:
        return None, f"Heartbeat HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, f"Không đọc được heartbeat: {_compact(str(exc), 160)}"


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
    heartbeat_url: Optional[str] = None

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
            # Tương thích config của source cũ.
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
        # Tự chuyển config cũ sang method đang được hỗ trợ.
        if health_check_method == "intent":
            health_check_method = "online"
        if health_check_method not in {"online", "heartbeat"}:
            raise ConfigError(
                "health_check_method phải là online hoặc heartbeat"
            )
        heartbeat_value = raw.get("heartbeat_url")
        heartbeat_url = str(heartbeat_value).strip() if heartbeat_value else None
        if heartbeat_url:
            heartbeat_url = validate_heartbeat_url(heartbeat_url)
        if health_check_method == "heartbeat" and not heartbeat_url:
            raise ConfigError(
                "Check Executor dùng heartbeat nhưng config chưa có heartbeat_url"
            )

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
                raw.get("health_check_timeout_seconds", 180), 15, 3600
            ),
            heartbeat_url=heartbeat_url,
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
            "heartbeat_url": self.heartbeat_url,
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
            f"Không thấy config {path}. Hãy chạy lệnh init trước."
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
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(APP_NAME)
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%H:%M:%S"))
    logger.addHandler(console)

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
    return logger


class SingleInstance:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.file: Optional[Any] = None

    def __enter__(self) -> "SingleInstance":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+", encoding="utf-8")
        if fcntl is None:
            return self
        try:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AppError("Đã có một tiến trình auto rejoin khác đang chạy") from exc
        self.file.seek(0)
        self.file.truncate()
        self.file.write(str(os.getpid()))
        self.file.flush()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.file is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            finally:
                self.file.close()


class WakeLock:
    def __init__(self, enabled: bool, logger: logging.Logger) -> None:
        self.enabled = enabled
        self.logger = logger
        self.acquired = False

    def __enter__(self) -> "WakeLock":
        command = shutil.which("termux-wake-lock")
        if not self.enabled:
            return self
        if not command:
            self.logger.warning(
                "Không thấy termux-wake-lock; nên cài package termux-api và tắt tối ưu pin cho Termux"
            )
            return self
        try:
            result = subprocess.run(
                [command], capture_output=True, text=True, timeout=8, check=False
            )
            self.acquired = result.returncode == 0
            if self.acquired:
                self.logger.info("Đã giữ wake-lock cho Termux")
            else:
                self.logger.warning("Không lấy được wake-lock: %s", _compact(result.stderr))
        except (OSError, subprocess.SubprocessError) as exc:
            self.logger.warning("Không lấy được wake-lock: %s", exc)
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if not self.acquired:
            return
        command = shutil.which("termux-wake-unlock")
        if command:
            subprocess.run(
                [command], capture_output=True, text=True, timeout=8, check=False
            )
            self.logger.info("Đã nhả wake-lock")


class AndroidBackend:
    """Runs Android system commands through direct, su, adb, or Termux am."""

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
        return "Termux soft mode (không force-stop)"

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
            stdout = decoded_parts[0]
            stderr = decoded_parts[1]
            return CommandResult(
                argv=logical_argv,
                returncode=124,
                output="\n".join(part for part in decoded_parts if part)
                or f"Timeout sau {timeout:.1f}s",
                elapsed=time.monotonic() - started,
                timed_out=True,
                stdout=stdout,
                stderr=stderr,
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
            # Truyền một remote command duy nhất để adb không làm mất quoting.
            command.extend(["shell", remote])
            return command, os.environ.copy()

        if self.kind == "soft":
            # Prefer Termux's am socket/library wrapper instead of /system/bin/am.
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
        if requested == "adb":
            raise BackendError(
                "Không có adb device. Hãy adb pair/connect trước hoặc truyền --adb-serial."
            )

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

    def candidate_urls(self) -> List[str]:
        candidates: List[str] = []
        if self.place_id:
            params: Dict[str, str] = {"placeId": self.place_id}
            if self.game_instance_id:
                params["gameInstanceId"] = self.game_instance_id
            elif self.access_code:
                params["accessCode"] = self.access_code
            elif self.link_code:
                params["linkCode"] = self.link_code
            encoded = urllib.parse.urlencode(params)
            candidates.append("roblox://" + encoded)
            candidates.append("roblox://experiences/start?" + encoded)
        if _is_roblox_url(self.raw):
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


class CookieStore:
    """Loads cookies without ever printing or logging their values."""

    @classmethod
    def normalize_record(cls, raw_record: str) -> str:
        """Accept either a raw cookie or ``account:password:cookie``."""
        value = str(raw_record).strip()
        lowered = value.lower()
        is_raw_cookie = value.startswith("_|WARNING:")
        is_cookie_header = lowered.startswith(".roblosecurity=") or lowered.startswith(
            "cookie:"
        )
        if not is_raw_cookie and not is_cookie_header:
            fields = value.split(":", 2)
            if len(fields) == 3 and len(fields[2].strip()) >= 50:
                # Không giữ lại account/password; chỉ cookie đi tiếp vào kho private.
                value = fields[2].strip()
        return cls.normalize(value)

    @staticmethod
    def normalize(raw_cookie: str) -> str:
        value = str(raw_cookie).strip().strip("'\"")
        # Cookie đôi khi bị thêm dấu '\\' khi sao chép từ Markdown/chat.
        # Chỉ bỏ escape trước các ký tự hợp lệ của token Roblox.
        value = re.sub(r"\\([_.|\-])", r"\1", value)
        header_match = re.search(
            r"(?i)(?:^|[;\s])\.ROBLOSECURITY\s*=\s*([^;\s]+)", value
        )
        if header_match:
            value = header_match.group(1).strip()
        if any(character in value for character in ("\x00", "\r", "\n")):
            raise ConfigError("Cookie chứa ký tự điều khiển không hợp lệ")
        if "\\" in value:
            raise ConfigError(
                "Cookie còn chứa dấu \\ không hợp lệ; hãy chép lại cookie gốc"
            )
        if len(value) < 50:
            raise ConfigError("Cookie quá ngắn hoặc không đúng định dạng")
        return value

    @classmethod
    def load(cls, path: Path, packages: Sequence[str]) -> Dict[str, str]:
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
            if not isinstance(raw_mapping, dict):
                raise ConfigError("Trường cookies phải là JSON object")
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
            raise ConfigError(f"Không tìm thấy cookie hợp lệ trong {path}")
        return mapping

    @staticmethod
    def save_private(path: Path, mapping: Dict[str, str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        payload = {"cookies": mapping}
        try:
            with temporary.open("w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise ConfigError(f"Không lưu được kho cookie {path}: {exc}") from exc


def validate_roblox_cookie(cookie: str, timeout: float = 10.0) -> Tuple[bool, str]:
    """Validate a cookie with Roblox without exposing it in logs or errors."""
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


def harden_cookie_file(path: Path, logger: logging.Logger) -> None:
    try:
        os.chmod(path, 0o600)
        mode = path.stat().st_mode & 0o777
    except OSError as exc:
        logger.warning("Không đặt được quyền 600 cho file cookie: %s", exc)
        return
    if mode & 0o077:
        logger.warning(
            "File cookie vẫn có quyền %03o; hãy chuyển nó vào vùng private của Termux",
            mode,
        )


class CookieInstaller:
    """Atomically writes RBXSession preference; this cannot prove app login."""

    PREF_NAME = "com.roblox.client_preferences.xml"
    SESSION_KEY = "RBXSession"

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
            return False, "Inject cookie cần backend root/su, ADB shell không đủ quyền"
        if not PACKAGE_RE.fullmatch(package):
            return False, "Package không hợp lệ"
        try:
            normalized = CookieStore.normalize(cookie)
        except ConfigError as exc:
            return False, str(exc)

        app_dir = f"/data/user/0/{package}"
        prefs_dir = f"{app_dir}/shared_prefs"
        prefs_path = f"{prefs_dir}/{self.PREF_NAME}"
        read_script = (
            f"if [ -f {shlex.quote(prefs_path)} ]; then "
            f"/system/bin/cat {shlex.quote(prefs_path)}; fi"
        )
        read_result = self.backend.run(
            ["sh", "-c", read_script], timeout=self.command_timeout
        )
        if not read_result.ok:
            # Không dùng combined stdout ở đây vì nó có thể chứa RBXSession cũ.
            safe_error = _compact(read_result.stderr) or f"rc={read_result.returncode}"
            return False, "Không đọc được SharedPreferences: " + safe_error

        try:
            root = self._updated_xml(read_result.stdout, normalized)
        except ElementTree.ParseError as exc:
            return False, f"SharedPreferences XML đang hỏng, không ghi đè: {exc}"

        if root is None:
            return True, "Cookie đã có sẵn trong XML (chưa xác minh đăng nhập app)"

        xml_body = ElementTree.tostring(root, encoding="unicode", short_empty_elements=True)
        xml_payload = (
            '<?xml version="1.0" encoding="utf-8" standalone="yes" ?>\n'
            + xml_body
            + "\n"
        )
        quoted_app = shlex.quote(app_dir)
        quoted_dir = shlex.quote(prefs_dir)
        quoted_prefs = shlex.quote(prefs_path)
        temporary_path = prefs_path + ".cookie.tmp"
        backup_path = prefs_path + ".pre_cookie_backup"
        quoted_temporary = shlex.quote(temporary_path)
        quoted_backup = shlex.quote(backup_path)
        write_script = (
            "set -eu; "
            f"test -d {quoted_app}; "
            f"mkdir -p {quoted_dir}; "
            f"if [ -f {quoted_prefs} ] && [ ! -f {quoted_backup} ]; then "
            f"cp -p {quoted_prefs} {quoted_backup}; fi; "
            f"trap 'rm -f {quoted_temporary}' EXIT; "
            f"cat > {quoted_temporary}; "
            f"owner=$(/system/bin/stat -c '%u:%g' {quoted_app}); "
            f"chown \"$owner\" {quoted_temporary}; "
            f"chmod 660 {quoted_temporary}; "
            f"mv -f {quoted_temporary} {quoted_prefs}; "
            f"if [ -x /system/bin/restorecon ]; then "
            f"/system/bin/restorecon -F {quoted_prefs} >/dev/null 2>&1 || true; fi; "
            "trap - EXIT"
        )
        write_result = self.backend.run(
            ["sh", "-c", write_script],
            timeout=self.command_timeout,
            input_text=xml_payload,
        )
        if not write_result.ok:
            return False, "Không ghi được SharedPreferences: " + _compact(
                write_result.stderr or write_result.output
            )

        verify_result = self.backend.run(
            ["sh", "-c", read_script], timeout=self.command_timeout
        )
        if not verify_result.ok:
            return False, "Đã ghi nhưng không đọc lại được để xác minh"
        try:
            verify_root = ElementTree.fromstring(verify_result.stdout)
        except ElementTree.ParseError:
            return False, "XML sau khi ghi không hợp lệ"
        current = verify_root.find(f"string[@name='{self.SESSION_KEY}']")
        if current is None or current.text != normalized:
            return False, "Không xác minh được RBXSession sau khi ghi"
        return True, "Đã ghi RBXSession vào XML (chưa xác minh đăng nhập app)"

    def _updated_xml(
        self, existing_xml: str, cookie: str
    ) -> Optional[ElementTree.Element]:
        if existing_xml.strip():
            root = ElementTree.fromstring(existing_xml)
            if root.tag != "map":
                raise ElementTree.ParseError("root element không phải <map>")
        else:
            root = ElementTree.Element("map")
        session = root.find(f"string[@name='{self.SESSION_KEY}']")
        if session is not None and session.text == cookie:
            return None
        if session is None:
            session = ElementTree.SubElement(
                root, "string", {"name": self.SESSION_KEY}
            )
        session.text = cookie
        return root


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
        # Một số ROM trả exit code khác 0 cho `am help` dù Activity Manager hoạt
        # động bình thường. Ưu tiên một lệnh chỉ đọc có kết quả xác định.
        current_user = self.backend.run(
            ["am", "get-current-user"], timeout=12
        )
        if self.command_accepted(current_user) and re.search(
            r"(?m)^\s*\d+\s*$", current_user.stdout or current_user.output
        ):
            return True, current_user.output

        help_result = self.backend.run(["am", "help"], timeout=12)
        help_text = help_result.output.lower()
        help_is_valid = (
            "activity manager" in help_text
            and "start-activity" in help_text
            and not any(
                marker in help_text
                for marker in (
                    "securityexception",
                    "permission denial",
                    "not allowed",
                    "not found",
                )
            )
        )
        if help_is_valid:
            return True, help_result.output
        detail = current_user.output or help_result.output
        return False, detail

    def force_stop(self, package: str) -> Tuple[bool, str]:
        if not self.backend.can_force_stop:
            return False, "backend soft không có quyền force-stop"
        result = self.backend.run(
            ["am", "force-stop", package], timeout=self.command_timeout
        )
        return self.command_accepted(result), result.output

    def start_lobby(self, package: str) -> Tuple[bool, str]:
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
        component = f"{package}/{self.PROTOCOL_ACTIVITY}"
        for url in spec.candidate_urls():
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
                    self.logger.debug(
                        "am result rc=%s elapsed=%.2fs output=%s",
                        result.returncode,
                        result.elapsed,
                        _compact(result.output),
                    )
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

    def package_exists(self, package: str) -> Tuple[Optional[bool], str]:
        result = self.backend.run(["pm", "path", package], timeout=15)
        if self.command_accepted(result):
            return result.output.startswith("package:"), result.output
        if not self.backend.can_inspect_all_packages:
            return None, result.output
        return False, result.output

    def is_process_running(self, package: str) -> Tuple[bool, str]:
        result = self.backend.run(["pidof", package], timeout=10)
        running = self.command_accepted(result) and bool(result.stdout.strip())
        return running, result.output

    def get_screen_size(self) -> Tuple[int, int]:
        result = self.backend.run(["wm", "size"], timeout=10)
        matches = re.findall(r"(?i)(\d+)x(\d+)", result.output)
        if matches:
            return tuple(map(int, matches[-1]))  # type: ignore[return-value]
        return 720, 1280

    def randomize_android_id(self) -> Tuple[bool, str]:
        if not self.backend.can_write_secure_settings:
            return False, "backend hiện tại không cho phép ghi secure settings"
        value = os.urandom(8).hex()
        result = self.backend.run(
            ["settings", "put", "secure", "android_id", value], timeout=15
        )
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

    def request_stop(self, signum: int, frame: Any) -> None:
        del frame
        if not self.stop_requested:
            self.logger.info("Nhận signal %s; sẽ dừng an toàn...", signum)
        self.stop_requested = True

    def run(self, once: bool = False) -> int:
        enabled = [target for target in self.config.targets if target.enabled]
        if not enabled:
            raise ConfigError("Không có target nào đang enabled")

        ok, detail = self.controller.preflight()
        if not ok:
            raise BackendError(
                "Backend không chạy được Android Activity Manager: " + _compact(detail)
            )
        if not self.controller.backend.can_force_stop:
            self.logger.warning(
                "Đang chạy soft mode: chỉ gửi deep link, không force-stop. "
                "Muốn ổn định hãy dùng --backend su hoặc --backend adb."
            )
        if self.config.auto_login_cookies:
            if not self.controller.backend.can_write_app_data:
                raise BackendError("Auto-login cookie cần backend root/su")
            if self.cookie_installer is None:
                raise ConfigError("Auto-login đã bật nhưng chưa nạp kho cookie")

        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)
        cycle = 0
        while not self.stop_requested:
            cycle += 1
            started = time.monotonic()
            monitor_mode = True
            cycle_logger = self.logger.debug if monitor_mode else self.logger.info
            cycle_logger("========== Bắt đầu chu kỳ %d =========", cycle)

            if self.config.randomize_android_id_each_cycle:
                changed, change_detail = self.controller.randomize_android_id()
                if changed:
                    self.logger.info("Đã đổi Android ID cho chu kỳ %d", cycle)
                else:
                    self.logger.warning(
                        "Không đổi được Android ID: %s", _compact(change_detail)
                    )

            bounds = self._resolve_bounds(enabled)
            succeeded = 0
            for index, target in enumerate(enabled):
                if self.stop_requested:
                    break
                if self._run_target(
                    target,
                    bounds[index],
                    force_rejoin=(cycle == 1),
                ):
                    succeeded += 1
                if index + 1 < len(enabled):
                    self._sleep(self.config.between_apps_seconds)

            elapsed = time.monotonic() - started
            cycle_logger(
                "Chu kỳ %d hoàn tất: %d/%d package ổn định, thời gian %.1fs",
                cycle,
                succeeded,
                len(enabled),
                elapsed,
            )
            if once or self.config.interval_seconds <= 0 or self.stop_requested:
                break
            wait_seconds = self.config.interval_seconds
            if monitor_mode:
                wait_seconds = min(wait_seconds, HEALTH_POLL_SECONDS)
                self._sleep(wait_seconds)
            else:
                self._wait_for_next_cycle(wait_seconds)
        self.logger.info("Auto rejoin đã dừng")
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
            self.logger.error("[%s] Link/Place ID không hợp lệ", target.package)
            return False

        exists, package_detail = self.controller.package_exists(target.package)
        if exists is False:
            self.logger.error(
                "[%s] Package không tồn tại: %s",
                target.package,
                _compact(package_detail),
            )
            return False

        if not force_rejoin:
            healthy, health_detail = self._target_health_once(target)
            if healthy:
                self.logger.debug(
                    "[%s] %s còn hợp lệ; giữ nguyên phiên",
                    target.package,
                    self._health_method_label(),
                )
                return True
            self.logger.info(
                "[%s] %s không hợp lệ; bắt đầu rejoin (%s)",
                target.package,
                self._health_method_label(),
                _compact(health_detail),
            )
        else:
            self.logger.info(
                "[%s] Lượt đầu: bắt buộc reset và rejoin",
                target.package,
            )

        self.logger.info("[%s] Chuẩn bị rejoin", target.package)
        if self.controller.backend.can_force_stop:
            stopped, stop_detail = self.controller.force_stop(target.package)
            if not stopped:
                self.logger.warning(
                    "[%s] force-stop thất bại: %s",
                    target.package,
                    _compact(stop_detail),
                )
            self._sleep(0.6)

        if self.config.auto_login_cookies:
            cookie = self.cookies.get(target.package)
            if not cookie:
                self.logger.error(
                    "[%s] Không có cookie tương ứng trong kho cookie", target.package
                )
                return False
            assert self.cookie_installer is not None
            installed, install_detail = self.cookie_installer.apply(
                target.package, cookie
            )
            if not installed:
                self.logger.error(
                    "[%s] Auto-login cookie thất bại: %s",
                    target.package,
                    _compact(install_detail),
                )
                return False
            self.logger.info("[%s] Cookie đăng nhập đã sẵn sàng", target.package)

        lobby_ok, lobby_detail = self.controller.start_lobby(target.package)
        if lobby_ok:
            self.logger.debug("[%s] Launcher đã mở", target.package)
            self._sleep(self.config.warmup_seconds)
        else:
            self.logger.warning(
                "[%s] Không mở được launcher; thử join thẳng: %s",
                target.package,
                _compact(lobby_detail),
            )

        if lobby_ok:
            if self.controller.backend.can_force_stop:
                reset_ok, reset_detail = self.controller.force_stop(target.package)
                if reset_ok:
                    self.logger.info(
                        "[%s] Reset xong phiên launcher; chuẩn bị mở lại",
                        target.package,
                    )
                else:
                    self.logger.warning(
                        "[%s] Reset launcher thất bại: %s",
                        target.package,
                        _compact(reset_detail),
                    )
                self._sleep(0.6)
            else:
                self.logger.warning(
                    "[%s] Bỏ qua reset vì backend không có quyền force-stop",
                    target.package,
                )

        attempts = self.config.retries + 1
        for attempt in range(1, attempts + 1):
            if self.stop_requested:
                return False
            if attempt > 1 and self.controller.backend.can_force_stop:
                self.controller.force_stop(target.package)
                self._sleep(0.6)

            join_started = time.time()
            accepted, detail = self.controller.start_deep_link(
                target.package,
                spec,
                freeform=self.config.freeform or self.config.auto_arrange,
                bounds=bounds,
            )
            if accepted:
                healthy, health_detail = self._wait_for_target_health(
                    target, join_started
                )
                if healthy:
                    self.logger.info(
                        "[%s] Rejoin thành công theo %s (lần %d/%d)",
                        target.package,
                        self._health_method_label(),
                        attempt,
                        attempts,
                    )
                    return True
                detail = (
                    "Android đã nhận intent nhưng "
                    f"{self._health_method_label()} không xác nhận: {health_detail}"
                )

            self.logger.warning(
                "[%s] Join lần %d/%d thất bại: %s",
                target.package,
                attempt,
                attempts,
                _compact(detail),
            )
            if attempt < attempts:
                delay = self.config.retry_backoff_seconds * (2 ** (attempt - 1))
                self._sleep(delay)
        return False

    def _health_method_label(self) -> str:
        return {
            "online": "Check Online",
            # Giữ tên quen thuộc trong giao diện; cơ chế là HTTPS heartbeat.
            "heartbeat": "Check Executor (Heartbeat)",
        }.get(self.config.health_check_method, "Check Online")

    def _target_health_once(
        self,
        target: TargetConfig,
        not_before: Optional[float] = None,
    ) -> Tuple[bool, str]:
        method = self.config.health_check_method
        if method == "online":
            running, detail = self.controller.is_process_running(target.package)
            return (
                (True, "Tiến trình Android đang chạy")
                if running
                else (False, _compact(detail) or "Không thấy tiến trình Android")
            )
        if method != "heartbeat":
            return False, f"Method check không hợp lệ: {method}"
        if not self.config.heartbeat_url:
            return False, "Chưa cấu hình heartbeat_url"

        timestamp, detail = fetch_heartbeat_timestamp(
            self.config.heartbeat_url,
            target.package,
            timeout=min(8.0, max(1.0, self.config.command_timeout_seconds)),
        )
        if timestamp is None:
            return False, detail
        now = time.time()
        if timestamp > now + 60:
            return False, "Timestamp heartbeat nằm quá xa trong tương lai"
        age = max(0.0, now - timestamp)
        if age > self.config.health_check_timeout_seconds:
            return False, f"Heartbeat đã cũ {age:.0f}s"
        if not_before is not None and timestamp < not_before - 5:
            return False, "Heartbeat chưa cập nhật sau lần mở app này"
        return True, f"Heartbeat mới {age:.0f}s"

    def _wait_for_target_health(
        self,
        target: TargetConfig,
        join_started: float,
    ) -> Tuple[bool, str]:
        deadline = time.monotonic() + max(
            0, self.config.health_check_timeout_seconds
        )
        last_detail = "Chưa có tín hiệu"
        while not self.stop_requested:
            healthy, last_detail = self._target_health_once(
                target, not_before=join_started
            )
            if healthy:
                return True, last_detail
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._sleep(min(HEALTH_POLL_SECONDS, remaining))
        return False, last_detail

    def _resolve_bounds(self, targets: List[TargetConfig]) -> List[Optional[str]]:
        configured = [target.bounds for target in targets]
        if not self.config.auto_arrange:
            if self.config.freeform:
                return [value or "0,0,600,800" for value in configured]
            return configured

        width, height = self.controller.get_screen_size()
        columns = math.ceil(math.sqrt(len(targets)))
        rows = math.ceil(len(targets) / columns)
        cell_width = width // columns
        cell_height = height // rows
        result: List[Optional[str]] = []
        for index, target in enumerate(targets):
            if target.bounds:
                result.append(target.bounds)
                continue
            column = index % columns
            row = index // columns
            left, top = column * cell_width, row * cell_height
            right = width if column == columns - 1 else left + cell_width
            bottom = height if row == rows - 1 else top + cell_height
            result.append(f"{left},{top},{right},{bottom}")
        return result

    def _wait_for_next_cycle(self, seconds: int) -> None:
        deadline = time.monotonic() + seconds
        while not self.stop_requested:
            remaining = math.ceil(deadline - time.monotonic())
            if remaining <= 0:
                return
            if remaining == seconds or remaining <= 10 or remaining % 60 == 0:
                minutes, secs = divmod(remaining, 60)
                self.logger.info("Chu kỳ tiếp theo sau %02d:%02d", minutes, secs)
            self._sleep(min(1.0, remaining))

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while not self.stop_requested:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.25, remaining))


def _compact(value: str, limit: int = 300) -> str:
    compact = " ".join(str(value).split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


def _parse_selection(raw: str, matches: List[str]) -> List[str]:
    clean = raw.strip()
    if clean.lower() in {"all", "a", "*"}:
        return matches
    selected = []
    for item in clean.split(","):
        item = item.strip()
        if item.isdigit() and 1 <= int(item) <= len(matches):
            selected.append(matches[int(item) - 1])
        elif PACKAGE_RE.fullmatch(item):
            selected.append(item)
        elif item:
            raise ConfigError(f"Lựa chọn/package không hợp lệ: {item}")
    return list(dict.fromkeys(selected))


def init_config(
    path: Path,
    backend: AndroidBackend,
    logger: logging.Logger,
    force: bool,
) -> int:
    if path.exists() and not force:
        answer = input(f"Config {path} đã tồn tại. Ghi đè? [y/N]: ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Đã huỷ.")
            return 0

    controller = AndroidController(backend, logger, 25)
    packages, error = controller.list_packages()
    matches = [
        package
        for package in packages
        if "roblox" in package.lower() or "clone" in package.lower()
    ]
    if matches:
        print("\nPackage có thể là Roblox:")
        for index, package in enumerate(matches, start=1):
            print(f"  {index:>2}. {package}")
        raw_selection = input(
            "Chọn số cách nhau bằng dấu phẩy, 'all', hoặc nhập package đầy đủ: "
        )
        selected = _parse_selection(raw_selection, matches)
    else:
        if error:
            print("Không quét được package:", _compact(error))
        raw_selection = input("Nhập package, nhiều package cách nhau bằng dấu phẩy: ")
        selected = _parse_selection(raw_selection, [])
    if not selected:
        raise ConfigError("Bạn chưa chọn package nào")

    link = input("Server link / Place ID áp dụng cho các package: ").strip()
    spec = RobloxLaunchSpec.parse(link)
    if not spec.is_valid():
        raise ConfigError("Server link / Place ID không hợp lệ")

    interval_raw = input("Thời gian giữa hai chu kỳ, phút [15; 0 = chạy một lần]: ").strip()
    interval_seconds = int(float(interval_raw or "15") * 60)
    warmup_raw = input("Thời gian warm-up launcher, giây [2.5]: ").strip()
    warmup = float(warmup_raw or "2.5")
    auto_login_answer = input(
        "Tự inject cookie trước khi mở từng app? [y/N]: "
    ).strip().lower()
    auto_login = auto_login_answer in {"y", "yes"}

    config = RejoinConfig(
        targets=[TargetConfig(package=package, link=link) for package in selected],
        interval_seconds=max(0, interval_seconds),
        warmup_seconds=max(0, min(60, warmup)),
        auto_login_cookies=auto_login,
    )
    save_config(path, config)
    print(f"\nĐã lưu config: {path}")
    if auto_login:
        print(
            "Hãy import cookie bằng: "
            "python sieuvip.py import-cookies /đường/dẫn/cookie.txt"
        )
    print("Kiểm tra bằng lệnh: python sieuvip.py check")
    return 0


def run_check(
    config: RejoinConfig,
    controller: AndroidController,
    backend: AndroidBackend,
    cookie_path: Path,
    logger: logging.Logger,
) -> int:
    print(f"Backend: {backend.description}")
    print(f"Force-stop: {'có' if backend.can_force_stop else 'không'}")
    am_ok, am_detail = controller.preflight()
    print(f"Android Activity Manager: {'OK' if am_ok else 'LỖI'}")
    if not am_ok:
        print("  ", _compact(am_detail))

    all_ok = am_ok
    for target in config.targets:
        spec = RobloxLaunchSpec.parse(target.link)
        exists, detail = controller.package_exists(target.package)
        package_status = "OK" if exists is True else (
            "không xác minh được" if exists is None else "KHÔNG TỒN TẠI"
        )
        link_status = "OK" if spec.is_valid() else "LỖI"
        print(f"- {target.package}: package={package_status}, link={link_status}")
        if spec.is_valid():
            print(f"  deep link ưu tiên: {spec.candidate_urls()[0]}")
        if exists is False:
            print("  ", _compact(detail))
            all_ok = False
        all_ok = all_ok and spec.is_valid()

    if backend.kind == "soft":
        print(
            "CẢNH BÁO: soft mode không force-stop và có thể bị Android 14+ chặn. "
            "Dùng backend su hoặc adb để chạy ổn định."
        )
    if config.auto_login_cookies:
        print(f"Auto-login cookie: bật ({cookie_path})")
        if not backend.can_write_app_data:
            print("  LỖI: inject cookie cần backend root/su")
            all_ok = False
        else:
            try:
                packages = [target.package for target in config.targets if target.enabled]
                cookies = CookieStore.load(cookie_path, packages)
                harden_cookie_file(cookie_path, logger)
                for package in packages:
                    status = "OK" if package in cookies else "THIẾU"
                    print(f"  {package}: cookie={status}")
                    all_ok = all_ok and package in cookies
            except ConfigError as exc:
                print(f"  LỖI: {exc}")
                all_ok = False
    return 0 if all_ok else 2


def import_cookies(
    config: RejoinConfig,
    source: Path,
    destination: Path,
) -> int:
    packages = [target.package for target in config.targets if target.enabled]
    mapping = CookieStore.load(source, packages)
    CookieStore.save_private(destination, mapping)
    print(f"Đã import {len(mapping)} cookie vào: {destination}")
    print("Cookie được lưu với quyền 600; giá trị cookie không được in ra màn hình.")
    missing = [package for package in packages if package not in mapping]
    if missing:
        print("Package còn thiếu cookie:")
        for package in missing:
            print(f"  - {package}")
        return 2
    return 0


def login_cookies(
    config: RejoinConfig,
    controller: AndroidController,
    backend: AndroidBackend,
    cookie_path: Path,
    logger: logging.Logger,
) -> int:
    if not backend.can_write_app_data:
        raise BackendError("Đăng nhập cookie cần backend root/su")
    targets = [target for target in config.targets if target.enabled]
    if not targets:
        raise ConfigError("Chưa chọn package. Hãy dùng chức năng 3 trước.")
    packages = [target.package for target in targets]
    cookies = CookieStore.load(cookie_path, packages)
    harden_cookie_file(cookie_path, logger)
    installer = CookieInstaller(backend, logger, config.command_timeout_seconds)
    succeeded = 0
    for target in targets:
        cookie = cookies.get(target.package)
        if not cookie:
            logger.warning(
                "Đã hết cookie; dừng trước package %s", target.package
            )
            break
        valid, validation_detail = validate_roblox_cookie(cookie)
        if not valid:
            logger.error(
                "[%s] Không dùng cookie: %s",
                target.package,
                _compact(validation_detail),
            )
            continue
        logger.info("[%s] %s", target.package, validation_detail)
        controller.force_stop(target.package)
        ok, detail = installer.apply(target.package, cookie)
        if ok:
            succeeded += 1
            logger.info("[%s] %s", target.package, detail)
            opened, open_detail = controller.start_lobby(target.package)
            if not opened:
                logger.warning(
                    "[%s] Đã inject cookie nhưng không mở được app: %s",
                    target.package,
                    _compact(open_detail),
                )
            time.sleep(max(0.0, config.between_apps_seconds))
        else:
            logger.error(
                "[%s] Ghi cookie thất bại: %s", target.package, _compact(detail)
            )
    logger.info(
        "Đã xác minh cookie và ghi XML cho %d/%d package", succeeded, len(targets)
    )
    if succeeded:
        logger.warning(
            "Roblox Android có thể bỏ qua RBXSession trong XML; trạng thái trên "
            "không phải xác nhận app đã đăng nhập."
        )
    return 0 if succeeded == len(targets) else 2


def list_packages(controller: AndroidController, keyword: str) -> int:
    packages, error = controller.list_packages()
    if not packages:
        raise BackendError("Không liệt kê được package: " + _compact(error))
    keyword = keyword.lower()
    for package in packages:
        if not keyword or keyword in package.lower():
            print(package)
    return 0


def _load_menu_config(path: Path) -> RejoinConfig:
    if path.exists():
        return load_config(path)
    return RejoinConfig(targets=[])


def match_package_prefix(packages: Sequence[str], raw_prefix: str) -> List[str]:
    """Match an Android package prefix on component boundaries.

    For example, ``com`` matches ``com.roblox.client`` but not ``company.app``.
    A complete package name is also accepted.
    """
    prefix = raw_prefix.strip().lower().rstrip(".")
    if not prefix:
        raise ConfigError("Bạn chưa nhập tên đầu package")
    if not re.fullmatch(r"[a-z0-9_]+(?:\.[a-z0-9_]+)*", prefix):
        raise ConfigError(f"Tên đầu package không hợp lệ: {raw_prefix!r}")
    return [
        package
        for package in packages
        if package.lower() == prefix or package.lower().startswith(prefix + ".")
    ]


def _menu_targets(config: RejoinConfig) -> List[TargetConfig]:
    return [target for target in config.targets if target.enabled]


def _require_menu_targets(config: RejoinConfig) -> List[TargetConfig]:
    targets = _menu_targets(config)
    if not targets:
        raise ConfigError("Chưa chọn package. Hãy dùng chức năng 3 trước.")
    return targets


def _configure_menu_packages(
    config: RejoinConfig,
    config_path: Path,
    controller: AndroidController,
) -> None:
    packages, error = controller.list_packages()
    if not packages:
        raise BackendError("Không liệt kê được package: " + _compact(error))

    prefix = input(
        "Nhập tên đầu package để chạy (ví dụ com hoặc com.roblox): "
    )
    selected = match_package_prefix(packages, prefix)
    if not selected:
        raise ConfigError(f"Không tìm thấy package bắt đầu bằng {prefix.strip()!r}")

    old_targets = {target.package: target for target in config.targets}
    config.targets = []
    for package in selected:
        old = old_targets.get(package)
        link = old.link if old and RobloxLaunchSpec.parse(old.link).is_valid() else (
            DEFAULT_BLOX_FRUITS_PLACE_ID
        )
        config.targets.append(
            TargetConfig(
                package=package,
                link=link,
                enabled=True,
                bounds=old.bounds if old else None,
            )
        )
    # Cookie login is an explicit menu action, not a side effect of auto rejoin.
    config.auto_login_cookies = False
    save_config(config_path, config)

    print(f"\nĐã chọn {len(config.targets)} package theo đúng thứ tự sau:")
    for index, target in enumerate(config.targets, start=1):
        print(f"  {index}. {target.package}")


def _configure_menu_links(config: RejoinConfig, config_path: Path) -> None:
    targets = _require_menu_targets(config)
    print("\n1. Nhập cho tất cả các package")
    print("2. Nhập cho từng package")
    mode = input("Chọn cách nhập [1/2]: ").strip()
    if mode not in {"1", "2"}:
        raise ConfigError("Chỉ chấp nhận lựa chọn 1 hoặc 2")

    if mode == "1":
        raw_link = input(
            "Nhập Game ID hoặc ServerVip [Enter = Blox Fruits]: "
        ).strip()
        link = raw_link or DEFAULT_BLOX_FRUITS_PLACE_ID
        if not RobloxLaunchSpec.parse(link).is_valid():
            raise ConfigError("Game ID hoặc ServerVip không hợp lệ")
        for target in targets:
            target.link = link
    else:
        print("\nCác package đã chọn ở chức năng 3:")
        updates: List[Tuple[TargetConfig, str]] = []
        for index, target in enumerate(targets, start=1):
            raw_link = input(
                f"{index}. {target.package} [Enter = Blox Fruits]: "
            ).strip()
            link = raw_link or DEFAULT_BLOX_FRUITS_PLACE_ID
            if not RobloxLaunchSpec.parse(link).is_valid():
                raise ConfigError(
                    f"Game ID hoặc ServerVip của {target.package} không hợp lệ"
                )
            updates.append((target, link))
        for target, link in updates:
            target.link = link

    save_config(config_path, config)
    print("\nĐã lưu Game ID/ServerVip.")


def _open_selected_apps(
    config: RejoinConfig,
    controller: AndroidController,
    logger: logging.Logger,
) -> int:
    targets = _require_menu_targets(config)
    opened = 0
    for index, target in enumerate(targets):
        ok, detail = controller.start_lobby(target.package)
        if ok:
            opened += 1
            logger.info("[%s] Đã mở app", target.package)
        else:
            logger.error("[%s] Không mở được app: %s", target.package, _compact(detail))
        if index + 1 < len(targets):
            time.sleep(max(0.0, config.between_apps_seconds))
    logger.info("Đã mở %d/%d package; không gửi lệnh join", opened, len(targets))
    return 0 if opened == len(targets) else 2


def _run_menu_rejoin(
    config: RejoinConfig,
    controller: AndroidController,
    logger: logging.Logger,
) -> int:
    _require_menu_targets(config)
    run_config = dataclasses.replace(config, auto_login_cookies=False)
    engine = RejoinEngine(run_config, controller, logger)
    old_sigint = signal.getsignal(signal.SIGINT)
    old_sigterm = signal.getsignal(signal.SIGTERM)
    try:
        with SingleInstance(DEFAULT_LOCK_PATH), WakeLock(config.wake_lock, logger):
            return engine.run(once=False)
    finally:
        signal.signal(signal.SIGINT, old_sigint)
        signal.signal(signal.SIGTERM, old_sigterm)


def _health_method_menu_label(config: RejoinConfig) -> str:
    return {
        "online": "Check Online (tiến trình Android)",
        "heartbeat": "Check Executor (Heartbeat HTTPS)",
    }.get(config.health_check_method, config.health_check_method)


def _config_menu(config: RejoinConfig, config_path: Path) -> None:
    while True:
        print("\033[2J\033[H", end="")
        print("SieuVipPro Rejoin - Config\n")
        print(f"1. Auto sort tabs: {'ON' if config.freeform else 'OFF'}")
        print("2. Auto block tat ca acc: KHONG HO TRO")
        print(f"3. Auto sap xep tabs: {'ON' if config.auto_arrange else 'OFF'}")
        print(f"4. Check Executor / Check Online: {_health_method_menu_label(config)}")
        print(
            "5. Check time (Executor): "
            f"{config.health_check_timeout_seconds}s"
        )
        print("0. Quay lai")
        try:
            choice = input("\nChọn cấu hình: ").strip()
        except (EOFError, KeyboardInterrupt):
            return

        if choice == "0":
            return
        if choice == "1":
            config.freeform = not config.freeform
            save_config(config_path, config)
            print(
                "\nAuto sort tabs đã "
                + ("bật (mở dạng cửa sổ nhỏ)." if config.freeform else "tắt.")
            )
        elif choice == "2":
            print(
                "\nKhông bật Auto block: Roblox không cung cấp API Open Cloud "
                "chính thức để tự block chéo các tài khoản. Tool không dùng API "
                "cookie cũ/không được hỗ trợ để tránh khoá hoặc lộ tài khoản."
            )
        elif choice == "3":
            config.auto_arrange = not config.auto_arrange
            save_config(config_path, config)
            print(
                "\nAuto sắp xếp tabs đã "
                + ("bật." if config.auto_arrange else "tắt.")
            )
        elif choice == "4":
            print("\n1. Check Online (kiểm tra tiến trình package)")
            print("2. Check Executor (Heartbeat HTTPS hợp lệ)")
            method_choice = input("Chọn method [1/2]: ").strip()
            if method_choice == "1":
                config.health_check_method = "online"
            elif method_choice == "2":
                current = config.heartbeat_url or ""
                prompt = "Heartbeat HTTPS URL"
                if current:
                    prompt += f" [{current}]"
                raw_url = input(
                    prompt + " (có thể dùng {package}): "
                ).strip()
                config.heartbeat_url = validate_heartbeat_url(raw_url or current)
                config.health_check_method = "heartbeat"
            else:
                raise ConfigError("Chỉ chấp nhận method 1 hoặc 2")
            save_config(config_path, config)
            print(f"\nĐã đổi method thành {_health_method_menu_label(config)}.")
        elif choice == "5":
            raw_seconds = input(
                "Số giây không có heartbeat trước khi đóng app [180]: "
            ).strip()
            seconds = int(raw_seconds or "180")
            if not 15 <= seconds <= 3600:
                raise ConfigError("Check time phải từ 15 đến 3600 giây")
            config.health_check_timeout_seconds = seconds
            save_config(config_path, config)
            print(f"\nĐã đặt Check time = {seconds}s.")
        else:
            print("\nLựa chọn không hợp lệ.")

        try:
            input("\nNhấn Enter để tiếp tục...")
        except (EOFError, KeyboardInterrupt):
            return


def interactive_menu(
    config_path: Path,
    cookie_source: Path,
    requested_backend: str,
    adb_serial: Optional[str],
    logger: logging.Logger,
) -> int:
    """Run the Termux menu and acquire Android privileges lazily."""
    config = _load_menu_config(config_path)
    cached_backend: Optional[AndroidBackend] = None

    def get_android() -> Tuple[AndroidBackend, AndroidController]:
        nonlocal cached_backend
        if cached_backend is None:
            cached_backend = select_backend(requested_backend, adb_serial)
        controller = AndroidController(
            cached_backend, logger, config.command_timeout_seconds
        )
        return cached_backend, controller

    while True:
        print("\033[2J\033[H", end="")
        print("SieuVipPro Rejoin\n")
        print("1. Start auto rejoin")
        print("2. Nhap Game ID or ServerVip")
        print("3. Chon Package de chay")
        print("4. Open all roblox tab")
        print("5. Login via cookie")
        print("13. Config")
        print("0. Thoat")
        try:
            choice = input("\nChọn chức năng: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nĐã thoát.")
            return 0

        try:
            if choice == "0":
                return 0
            if choice == "1":
                _, controller = get_android()
                _run_menu_rejoin(config, controller, logger)
            elif choice == "2":
                _configure_menu_links(config, config_path)
            elif choice == "3":
                _, controller = get_android()
                _configure_menu_packages(config, config_path, controller)
            elif choice == "4":
                _, controller = get_android()
                _open_selected_apps(config, controller, logger)
            elif choice == "5":
                backend, controller = get_android()
                login_cookies(
                    config, controller, backend, cookie_source, logger
                )
            elif choice == "13":
                _config_menu(config, config_path)
            else:
                print("Lựa chọn không hợp lệ.")
        except (AppError, ValueError) as exc:
            logger.error("%s", exc)

        try:
            input("\nNhấn Enter để về menu...")
        except (EOFError, KeyboardInterrupt):
            return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Roblox auto rejoin ổn định cho Termux"
    )
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG_PATH, help="đường dẫn config JSON"
    )
    parser.add_argument(
        "--cookies",
        type=Path,
        default=DEFAULT_COOKIE_PATH,
        help="kho cookie JSON riêng tư",
    )
    parser.add_argument(
        "--backend",
        choices=("auto", "direct", "su", "adb", "soft"),
        default="auto",
        help="backend chạy lệnh Android (mặc định: auto)",
    )
    parser.add_argument("--adb-serial", help="serial adb khi có nhiều device")
    parser.add_argument("--verbose", action="store_true", help="in log debug")
    subparsers = parser.add_subparsers(dest="command", required=True)

    menu_parser = subparsers.add_parser("menu", help="mở menu SieuVipPro")
    menu_parser.add_argument(
        "--cookie-source",
        type=Path,
        default=DEFAULT_COOKIE_SOURCE_PATH,
        help="cookie.txt dùng cho chức năng 5",
    )

    init_parser = subparsers.add_parser("init", help="tạo config bằng menu")
    init_parser.add_argument("--force", action="store_true", help="ghi đè config")

    subparsers.add_parser("check", help="kiểm tra backend, package và link")

    import_parser = subparsers.add_parser(
        "import-cookies", help="import cookie.txt/JSON vào kho riêng tư"
    )
    import_parser.add_argument("source", type=Path, help="file cookie nguồn")

    subparsers.add_parser("login", help="inject cookie vào các package một lần")

    run_parser = subparsers.add_parser("run", help="chạy auto rejoin")
    run_parser.add_argument("--once", action="store_true", help="chỉ chạy một chu kỳ")
    run_parser.add_argument(
        "--no-wake-lock", action="store_true", help="không lấy Termux wake-lock"
    )

    list_parser = subparsers.add_parser("list-packages", help="liệt kê package")
    list_parser.add_argument("keyword", nargs="?", default="")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    log_path = DEFAULT_LOG_PATH
    logger = setup_logger(log_path, verbose=args.verbose)
    try:
        # Menu phải xuất hiện trước mọi lần kiểm tra backend/root. Quyền Android
        # chỉ được dùng sau khi người dùng chọn một chức năng cần tới nó.
        if args.command == "menu":
            return interactive_menu(
                args.config,
                args.cookie_source,
                args.backend,
                args.adb_serial,
                logger,
            )

        if args.command == "import-cookies":
            config = load_config(args.config)
            return import_cookies(config, args.source, args.cookies)

        backend = select_backend(args.backend, args.adb_serial)
        logger.info("Backend: %s", backend.description)

        if args.command == "init":
            return init_config(args.config, backend, logger, args.force)

        config = load_config(args.config)
        controller = AndroidController(
            backend, logger, config.command_timeout_seconds
        )
        if args.command == "check":
            return run_check(config, controller, backend, args.cookies, logger)
        if args.command == "login":
            return login_cookies(
                config, controller, backend, args.cookies, logger
            )
        if args.command == "list-packages":
            return list_packages(controller, args.keyword)
        if args.command == "run":
            cookie_installer = None
            cookies: Dict[str, str] = {}
            if config.auto_login_cookies:
                packages = [
                    target.package for target in config.targets if target.enabled
                ]
                cookies = CookieStore.load(args.cookies, packages)
                harden_cookie_file(args.cookies, logger)
                cookie_installer = CookieInstaller(
                    backend, logger, config.command_timeout_seconds
                )
            engine = RejoinEngine(
                config,
                controller,
                logger,
                cookie_installer=cookie_installer,
                cookies=cookies,
            )
            use_wake_lock = config.wake_lock and not args.no_wake_lock
            with SingleInstance(DEFAULT_LOCK_PATH), WakeLock(use_wake_lock, logger):
                return engine.run(once=args.once)
        raise AppError(f"Command không xác định: {args.command}")
    except (AppError, ValueError) as exc:
        logger.error("%s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
