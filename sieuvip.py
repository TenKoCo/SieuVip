#!/data/data/com.termux/files/usr/bin/python
from __future__ import annotations

import argparse
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import random
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import parse_qs, quote, urlparse
import xml.etree.ElementTree as ET


APP_DIR = Path.home() / ".roblox_rejoin_root"
CONFIG_FILE = APP_DIR / "config.json"
LOG_FILE = APP_DIR / "roblox_rejoin.log"
LOCK_FILE = APP_DIR / "worker.lock"
PID_FILE = APP_DIR / "worker.pid"
RUNTIME_FILE = APP_DIR / "runtime.json"

STOP_EVENT = threading.Event()

AUTOEXEC_DIR_CACHE: dict[str, list[str]] = {}
SCRIPT_HB_PATH_CACHE: dict[str, str] = {}
SCRIPT_HB_LAST_SCAN: dict[str, float] = {}

PACKAGE_RE = re.compile(r"^[A-Za-z0-9._]+$")
SERVER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")

logger = logging.getLogger("roblox-root-rejoin")

DEFAULT_CONFIG: dict[str, Any] = {
    "auto_rejoin": False,
    "selected_packages": [],
    "package_usernames": {},
    "package_uids": {},
    "auto_cross_block": False,
    "auto_sort_tab": False,
    "auto_sort_home_delay_seconds": 0.8,
    "return_termux_after_sort": True,
    "block_profile_load_seconds": 4,
    "block_ui_timeout_seconds": 10,
    "block_action_gap_seconds": 1.5,
    "script_heartbeat_enabled": True,
    "script_heartbeat_interval_seconds": 3,
    "script_heartbeat_timeout_seconds": 12,
    "script_heartbeat_start_timeout_seconds": 45,
    "script_heartbeat_required_for_ready": True,
    "target_scope": "all",
    "package_targets": {},
    "target": {
        "mode": "place",
        "place_id": "",
        "server_id": "",
        "access_code": "",
        "link_code": "",
        "raw_uri": "",
    },
    "stop_after_minutes": 0.0,
    "stop_deadline_epoch": 0.0,
    "last_stop_reason": "",
    "check_interval": 10,
    "heartbeat_failure_threshold": 3,
    "close_all_before_start": True,
    "close_all_wait_seconds": 2,
    "ready_stable_seconds": 8,
    "ready_timeout_seconds": 90,
    "ready_poll_seconds": 1,
    "launch_attempts": 3,
    "launch_gap": 2,
    "hard_rejoin_minutes": 0,
}


@dataclass
class PackageState:
    package: str
    username: str = "-"
    phase: str = "STOPPED"
    pid: int | None = None
    alive: bool = False
    consecutive_failures: int = 0
    restart_count: int = 0
    previous_pid: int | None = None
    last_seen: float = 0.0
    last_rejoin: float = 0.0
    script_heartbeat_alive: bool = False
    script_heartbeat_age: float | None = None
    script_heartbeat_path: str = ""
    script_user_id: str = ""
    script_place_id: str = ""
    script_job_id: str = ""
    minimized: bool = False
    detail: str = ""


def ensure_dir() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)


def setup_logging() -> None:
    ensure_dir()
    if logger.handlers:
        return

    logger.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )
    handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=2 * 1024 * 1024,
        backupCount=4,
        encoding="utf-8",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)


def atomic_json_write(path: Path, data: dict[str, Any]) -> None:
    ensure_dir()
    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=str(path.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_config() -> dict[str, Any]:
    ensure_dir()
    if not CONFIG_FILE.exists():
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        atomic_json_write(CONFIG_FILE, cfg)
        return cfg

    try:
        saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.exception("Không đọc được config")
        return json.loads(json.dumps(DEFAULT_CONFIG))

    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    for key, value in saved.items():
        if key == "target" and isinstance(value, dict):
            cfg["target"].update(value)
        else:
            cfg[key] = value

    if not isinstance(cfg.get("package_usernames"), dict):
        cfg["package_usernames"] = {}
    if not isinstance(cfg.get("package_uids"), dict):
        cfg["package_uids"] = {}
    return cfg


def save_config(config: dict[str, Any]) -> None:
    atomic_json_write(CONFIG_FILE, config)


def run_command(
    args: list[str],
    timeout: float = 10.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )


def root_run(
    args: list[str],
    timeout: float = 15.0,
) -> subprocess.CompletedProcess[str]:
    return run_command(
        ["su", "-c", shlex.join(args)],
        timeout=timeout,
    )


def require_root() -> None:
    try:
        result = run_command(["su", "-c", "id -u"], timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Không gọi được su: {exc}") from exc

    if result.returncode != 0 or result.stdout.strip() != "0":
        raise RuntimeError(
            "ROOT REQUIRED: Termux chưa được cấp quyền root UID 0."
        )


def android_user() -> int:
    try:
        result = root_run(["am", "get-current-user"], timeout=5)
        value = result.stdout.strip()
        if result.returncode == 0 and value.isdigit():
            return int(value)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return 0


def scan_packages() -> list[str]:
    result = root_run(
        [
            "pm",
            "list",
            "packages",
            "-3",
            "--user",
            str(android_user()),
        ],
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "pm list packages thất bại")

    packages: set[str] = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            package = line[8:].strip()
            if PACKAGE_RE.fullmatch(package):
                packages.add(package)
    return sorted(packages, key=str.lower)


def package_exists(package: str) -> bool:
    if not PACKAGE_RE.fullmatch(package):
        return False
    try:
        result = root_run(["pm", "path", package], timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and "package:" in result.stdout


def package_looks_like_roblox(package: str) -> bool:
    if "roblox" in package.lower():
        return True
    try:
        result = root_run(["dumpsys", "package", package], timeout=6)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    dump = result.stdout.lower()
    return any(
        marker in dump
        for marker in ("com.roblox.client", "robloxactivity", "roblox")
    )


def auto_detect_roblox(packages: list[str]) -> list[str]:
    found: list[str] = []
    total = len(packages)
    for index, package in enumerate(packages, start=1):
        print(
            f"\rĐang nhận diện Roblox {index}/{total}: {package[:35]:35}",
            end="",
            flush=True,
        )
        if package_looks_like_roblox(package):
            found.append(package)
    print()
    return found


def valid_place_id(value: str) -> bool:
    return value.isdigit() and int(value) > 0


def validate_raw_uri(uri: str) -> bool:
    uri = uri.strip()
    if uri.startswith("roblox://"):
        return True
    try:
        parsed = urlparse(uri)
    except ValueError:
        return False

    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    return (
        host == "roblox.com"
        or host.endswith(".roblox.com")
        or host == "ro.blox.com"
    )


BLOX_FRUITS_PLACE_ID = "2753915549"


def default_blox_fruits_target() -> dict[str, str]:
    return {
        "mode": "place",
        "place_id": BLOX_FRUITS_PLACE_ID,
        "server_id": "",
        "access_code": "",
        "link_code": "",
        "raw_uri": "",
    }


def target_to_uri(target: dict[str, Any]) -> str:
    mode = str(target.get("mode", "place"))

    if mode == "raw":
        uri = str(target.get("raw_uri", "")).strip()
        if not validate_raw_uri(uri):
            raise ValueError("Roblox URI không hợp lệ")
        return uri

    place_id = str(target.get("place_id", "")).strip()
    if not valid_place_id(place_id):
        raise ValueError("Place ID chưa hợp lệ")

    uri = f"roblox://placeId={place_id}"

    if mode == "server":
        server_id = str(target.get("server_id", "")).strip()
        if not SERVER_ID_RE.fullmatch(server_id):
            raise ValueError("Server JobId không hợp lệ")
        uri += "&gameInstanceId=" + quote(server_id, safe="-_")

    elif mode == "private":
        access_code = str(target.get("access_code", "")).strip()
        if not access_code:
            raise ValueError("Access Code đang trống")
        uri += "&accessCode=" + quote(access_code, safe="")

    elif mode == "link_code":
        link_code = str(target.get("link_code", "")).strip()
        if not link_code:
            raise ValueError("Link Code đang trống")
        uri += "&linkCode=" + quote(link_code, safe="")

    return uri


def get_target_for_package(
    config: dict[str, Any],
    package: str | None = None,
) -> dict[str, Any]:
    scope = str(config.get("target_scope", "all"))

    if scope == "per_package" and package:
        package_targets = config.get("package_targets", {})
        if isinstance(package_targets, dict):
            target = package_targets.get(package)
            if isinstance(target, dict):
                return target

        # Package mới chưa được cấu hình: fallback ẩn = Blox Fruits.
        return default_blox_fruits_target()

    target = config.get("target")
    if isinstance(target, dict):
        # Config cũ chưa có target hợp lệ cũng fallback về Blox Fruits.
        try:
            target_to_uri(target)
            return target
        except ValueError:
            pass

    return default_blox_fruits_target()


def build_target_uri(
    config: dict[str, Any],
    package: str | None = None,
) -> str:
    return target_to_uri(
        get_target_for_package(
            config,
            package,
        )
    )


def target_summary(config: dict[str, Any]) -> str:
    scope = str(config.get("target_scope", "all"))

    if scope == "per_package":
        selected = list(
            dict.fromkeys(
                config.get("selected_packages", [])
            )
        )
        package_targets = config.get("package_targets", {})
        configured = (
            sum(
                1
                for package in selected
                if isinstance(package_targets, dict)
                and isinstance(package_targets.get(package), dict)
            )
        )
        return (
            f"RIÊNG TỪNG PACKAGE "
            f"({configured}/{len(selected)} đã nhập; "
            f"còn lại mặc định Blox Fruits)"
        )

    try:
        uri = build_target_uri(config)
    except ValueError:
        uri = f"roblox://placeId={BLOX_FRUITS_PLACE_ID}"

    return f"TẤT CẢ PACKAGE → {uri}"



def get_process_snapshot() -> dict[str, list[int]]:
    try:
        result = root_run(
            ["/system/bin/ps", "-A", "-o", "PID,NAME"],
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.exception("Không lấy được process snapshot")
        return {}

    if result.returncode != 0:
        logger.error("ps lỗi: %s", result.stderr.strip())
        return {}

    processes: dict[str, list[int]] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or line.upper().startswith("PID"):
            continue

        parts = line.split(None, 1)
        if len(parts) != 2:
            continue

        pid_text, name = parts
        try:
            pid = int(pid_text)
        except ValueError:
            continue

        processes.setdefault(name.strip(), []).append(pid)
    return processes


def find_package_pid(
    package: str,
    processes: dict[str, list[int]],
) -> int | None:
    direct = processes.get(package)
    if direct:
        return direct[0]

    prefix = f"{package}:"
    for name, pids in processes.items():
        if name.startswith(prefix) and pids:
            return pids[0]
    return None


def get_package_pid_root(package: str) -> int | None:
    if not PACKAGE_RE.fullmatch(package):
        return None
    try:
        result = root_run(["pidof", package], timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    output = result.stdout.strip()
    if not output:
        return None

    try:
        return int(output.split()[0])
    except ValueError:
        return None


def force_stop_package(package: str) -> bool:
    if not PACKAGE_RE.fullmatch(package):
        return False
    try:
        result = root_run(
            [
                "am",
                "force-stop",
                "--user",
                str(android_user()),
                package,
            ],
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.exception("force-stop lỗi: %s", package)
        return False

    if result.returncode != 0:
        logger.warning(
            "force-stop %s thất bại: %s",
            package,
            result.stderr.strip(),
        )
        return False

    logger.info("CLOSED -> %s", package)
    return True


def send_join_intent(package: str, uri: str) -> bool:
    try:
        result = root_run(
            [
                "am",
                "start",
                "--user",
                str(android_user()),
                "-W",
                "-a",
                "android.intent.action.VIEW",
                "-c",
                "android.intent.category.BROWSABLE",
                "-d",
                uri,
                "-p",
                package,
            ],
            timeout=25,
        )
    except subprocess.TimeoutExpired:
        logger.warning("am start timeout: %s", package)
        return False
    except OSError:
        logger.exception("am start lỗi: %s", package)
        return False

    output = f"{result.stdout}\n{result.stderr}"
    lower = output.lower()
    failed = any(
        marker in lower
        for marker in (
            "error:",
            "exception",
            "securityexception",
            "unable to resolve",
        )
    )

    if result.returncode != 0 or failed:
        logger.warning(
            "Join intent %s thất bại: %s",
            package,
            output[-800:].strip(),
        )
        return False

    logger.info("JOIN INTENT -> %s", package)
    return True



def open_profile_in_package(package: str, target_uid: str) -> bool:
    """Mở profile Roblox bằng UID trong đúng package, không truy cập cookie/session."""
    if not PACKAGE_RE.fullmatch(package) or not valid_user_id(target_uid):
        return False

    profile_url = f"https://www.roblox.com/users/{target_uid}/profile"
    try:
        result = root_run(
            [
                "am",
                "start",
                "--user",
                str(android_user()),
                "-W",
                "-a",
                "android.intent.action.VIEW",
                "-c",
                "android.intent.category.BROWSABLE",
                "-d",
                profile_url,
                "-p",
                package,
            ],
            timeout=25,
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.exception("Không mở được profile UID=%s trong %s", target_uid, package)
        return False

    output = f"{result.stdout}\n{result.stderr}".lower()
    if result.returncode != 0 or any(
        token in output
        for token in ("error:", "exception", "unable to resolve", "securityexception")
    ):
        logger.warning("Open profile thất bại %s -> %s", package, target_uid)
        return False
    return True


def dump_ui_xml() -> str | None:
    """Dump accessibility tree. Không OCR và không đọc dữ liệu đăng nhập."""
    remote_path = "/data/local/tmp/roblox_rejoin_ui.xml"
    commands = [
        ["/system/bin/uiautomator", "dump", "--compressed", remote_path],
        ["uiautomator", "dump", "--compressed", remote_path],
    ]

    dumped = False
    for command in commands:
        try:
            result = root_run(command, timeout=12)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            dumped = True
            break

    if not dumped:
        return None

    try:
        result = root_run(["cat", remote_path], timeout=5)
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        try:
            root_run(["rm", "-f", remote_path], timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass


def bounds_center(bounds: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds.strip())
    if not match:
        return None
    x1, y1, x2, y2 = (int(value) for value in match.groups())
    if x2 <= x1 or y2 <= y1:
        return None
    return ((x1 + x2) // 2, (y1 + y2) // 2)


def find_ui_target(
    xml_text: str,
    *,
    exact_labels: tuple[str, ...] = (),
    contains_labels: tuple[str, ...] = (),
    resource_contains: tuple[str, ...] = (),
) -> tuple[int, int, str] | None:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    exact = {label.casefold() for label in exact_labels}
    contains = tuple(label.casefold() for label in contains_labels)
    resources = tuple(label.casefold() for label in resource_contains)

    candidates: list[tuple[int, int, str]] = []
    for node in root.iter("node"):
        text = str(node.attrib.get("text", "")).strip()
        desc = str(node.attrib.get("content-desc", "")).strip()
        resource_id = str(node.attrib.get("resource-id", "")).strip()
        bounds = str(node.attrib.get("bounds", "")).strip()

        values = [value.casefold() for value in (text, desc) if value]
        matched = any(value in exact for value in values)
        if not matched and contains:
            matched = any(token in value for value in values for token in contains)
        if not matched and resources:
            rid = resource_id.casefold()
            matched = any(token in rid for token in resources)
        if not matched:
            continue

        center = bounds_center(bounds)
        if center is None:
            continue
        label = text or desc or resource_id
        candidates.append((center[0], center[1], label))

    if not candidates:
        return None
    # UI menu/profile controls are usually nearer the upper part of the screen.
    return min(candidates, key=lambda item: item[1])


def tap_ui_target(target: tuple[int, int, str]) -> bool:
    x, y, _ = target
    try:
        result = root_run(["input", "tap", str(x), str(y)], timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def wait_and_tap_ui(
    *,
    timeout: float,
    exact_labels: tuple[str, ...] = (),
    contains_labels: tuple[str, ...] = (),
    resource_contains: tuple[str, ...] = (),
) -> str | None:
    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline and not STOP_EVENT.is_set():
        xml_text = dump_ui_xml()
        if xml_text:
            target = find_ui_target(
                xml_text,
                exact_labels=exact_labels,
                contains_labels=contains_labels,
                resource_contains=resource_contains,
            )
            if target and tap_ui_target(target):
                return target[2]
        STOP_EVENT.wait(0.8)
    return None


def block_uid_via_app_ui(
    source_package: str,
    target_uid: str,
    config: dict[str, Any],
) -> tuple[bool, str]:
    """
    Block qua UI profile chính thức của Roblox.
    Không trích xuất hoặc tái sử dụng cookie/session của app.
    """
    if not open_profile_in_package(source_package, target_uid):
        return False, "không mở được profile"

    load_wait = max(
        1.0,
        min(20.0, float(config.get("block_profile_load_seconds", 4))),
    )
    if STOP_EVENT.wait(load_wait):
        return False, "đã dừng"

    ui_timeout = max(
        3.0,
        min(30.0, float(config.get("block_ui_timeout_seconds", 10))),
    )

    # Roblox Support mô tả: profile -> menu ba chấm -> Block User.
    more = wait_and_tap_ui(
        timeout=ui_timeout,
        exact_labels=(
            "More",
            "More options",
            "More Options",
            "Thêm",
            "Tùy chọn khác",
            "Tuỳ chọn khác",
        ),
        contains_labels=("more options", "tùy chọn", "tuỳ chọn"),
        resource_contains=("overflow", "more"),
    )
    if more is None:
        return False, "không nhận diện được nút ba chấm/More"

    if STOP_EVENT.wait(0.8):
        return False, "đã dừng"

    xml_text = dump_ui_xml()
    if not xml_text:
        return False, "không đọc được menu profile"

    already = find_ui_target(
        xml_text,
        exact_labels=(
            "Unblock User",
            "Unblock",
            "Bỏ chặn người dùng",
            "Bỏ chặn",
        ),
    )
    if already is not None:
        return True, "đã block từ trước"

    block_target = find_ui_target(
        xml_text,
        exact_labels=(
            "Block User",
            "Block",
            "Chặn người dùng",
            "Chặn",
        ),
    )
    if block_target is None or not tap_ui_target(block_target):
        return False, "không nhận diện được Block User"

    if STOP_EVENT.wait(0.8):
        return False, "đã dừng"

    # Hộp thoại xác nhận thường có nút Block/Chặn.
    confirm = wait_and_tap_ui(
        timeout=ui_timeout,
        exact_labels=("Block", "Block User", "Chặn", "Chặn người dùng"),
    )
    if confirm is None:
        return False, "không nhận diện được nút xác nhận Block"

    return True, "block thành công"


def run_cross_block(
    config: dict[str, Any],
    states: dict[str, PackageState] | None = None,
) -> tuple[int, int]:
    """Mỗi package/account block UID của tất cả account còn lại."""
    packages = list(dict.fromkeys(config.get("selected_packages", [])))
    uids = config.get("package_uids", {})

    identities: list[tuple[str, str]] = []
    for package in packages:
        uid = str(uids.get(package, "") or "").strip()
        if valid_user_id(uid):
            identities.append((package, uid))

    if len(identities) < 2:
        logger.warning("Auto block chéo cần ít nhất 2 package có UID hợp lệ")
        return 0, 0

    success_count = 0
    total_count = 0
    action_gap = max(
        0.5,
        min(10.0, float(config.get("block_action_gap_seconds", 1.5))),
    )

    for source_package, source_uid in identities:
        if STOP_EVENT.is_set():
            break

        if states and source_package in states:
            states[source_package].phase = "CHECKING"
            states[source_package].detail = "Đang auto block chéo account"
            write_runtime(states, "CROSS_BLOCK")

        for _, target_uid in identities:
            if STOP_EVENT.is_set():
                break
            if target_uid == source_uid:
                continue

            total_count += 1
            ok, message = block_uid_via_app_ui(
                source_package,
                target_uid,
                config,
            )
            if ok:
                success_count += 1
                logger.info(
                    "CROSS BLOCK OK source=%s target_uid=%s (%s)",
                    source_package,
                    target_uid,
                    message,
                )
            else:
                logger.warning(
                    "CROSS BLOCK FAIL source=%s target_uid=%s (%s)",
                    source_package,
                    target_uid,
                    message,
                )

            if STOP_EVENT.wait(action_gap):
                break

        force_stop_package(source_package)
        STOP_EVENT.wait(0.8)

    return success_count, total_count


def configure_cross_block(config: dict[str, Any]) -> None:
    packages = list(dict.fromkeys(config.get("selected_packages", [])))
    if len(packages) < 2:
        print("Cần ít nhất 2 package ở mục 3 để block chéo.")
        return

    currently_enabled = bool(config.get("auto_cross_block", False))
    if currently_enabled:
        config["auto_cross_block"] = False
        save_config(config)
        print("Auto block chéo account: OFF")
        return

    configure_usernames(config)
    config = load_config()
    uids = config.get("package_uids", {})
    missing = [
        package
        for package in packages
        if not valid_user_id(str(uids.get(package, "") or ""))
    ]
    if missing:
        print("Thiếu UID hợp lệ cho:")
        for package in missing:
            print(" -", package)
        print("Chưa bật Auto block chéo.")
        return

    config["auto_cross_block"] = True
    save_config(config)
    print("Auto block chéo account: ON")

    # Khi bật mục 6, chạy ngay nếu worker Auto Rejoin chưa chiếm các app.
    if worker_status() == "STOPPED":
        states: dict[str, PackageState] = {}
        sync_states(config, states)
        force_stop_all(config, states)
        completed, total = run_cross_block(config, states)
        force_stop_all(config, states)
        print(f"Auto block chéo hoàn tất: {completed}/{total} action.")
    else:
        print("Worker đang chạy; Auto block chéo sẽ áp dụng ở lần worker khởi động kế tiếp.")


def script_heartbeat_marker(package: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]", "_", package)
    return f"__termux_heartbeat_{safe}.txt"


def build_script_heartbeat_lua(
    package: str,
    interval_seconds: float,
) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]", "_", package)
    marker = script_heartbeat_marker(package)
    interval = max(1.0, min(30.0, float(interval_seconds)))

    return f'''-- Generated by Termux Roblox Rejoin
local HB_FILE = "{marker}"
local HB_INTERVAL = {interval:.2f}
local ENV = (getgenv and getgenv()) or _G
local FLAG = "__TERMUX_SCRIPT_HB_{safe}"

if ENV[FLAG] then
    return
end
ENV[FLAG] = true

local Players = game:GetService("Players")

local function heartbeat()
    if type(writefile) ~= "function" then
        return false
    end

    local uid = "0"
    pcall(function()
        local player = Players.LocalPlayer
        if player then
            uid = tostring(player.UserId)
        end
    end)

    local payload = table.concat({{
        tostring(os.time()),
        uid,
        tostring(game.PlaceId),
        tostring(game.JobId)
    }}, "|")

    local ok = pcall(function()
        writefile(HB_FILE, payload)
    end)
    return ok
end

task.spawn(function()
    while true do
        pcall(heartbeat)
        task.wait(HB_INTERVAL)
    end
end)
'''.strip()


def _existing_root_dirs(paths: list[str]) -> list[str]:
    existing: list[str] = []
    for path in paths:
        try:
            result = root_run(["test", "-d", path], timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            existing.append(path)
    return existing


def package_storage_roots(package: str) -> list[str]:
    roots = [
        f"/data/user/0/{package}/files",
        f"/data/data/{package}/files",
        f"/storage/emulated/0/Android/data/{package}/files",
        f"/storage/emulated/0/Android/media/{package}",
    ]
    return list(dict.fromkeys(_existing_root_dirs(roots)))


def find_autoexec_dirs(package: str) -> list[str]:
    cached = AUTOEXEC_DIR_CACHE.get(package)
    if cached:
        valid = _existing_root_dirs(cached)
        if valid:
            AUTOEXEC_DIR_CACHE[package] = valid
            return valid

    roots = package_storage_roots(package)
    found: list[str] = []
    executor_parents: list[str] = []

    for root in roots:
        try:
            result = root_run(
                ["find", root, "-maxdepth", "7", "-type", "d"],
                timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue

        if result.returncode != 0:
            continue

        for raw in result.stdout.splitlines():
            path = raw.strip()
            if not path:
                continue

            name = Path(path).name.lower().replace("_", "").replace("-", "")
            if name in {"autoexec", "autoexecute"}:
                found.append(path)

            if name in {
                "delta",
                "deltaexecutor",
                "arceus",
                "arceusx",
                "arceusxneo",
            }:
                executor_parents.append(path)

    if not found:
        for parent in executor_parents:
            destination = f"{parent.rstrip('/')}/autoexec"
            try:
                result = root_run(["mkdir", "-p", destination], timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if result.returncode == 0:
                found.append(destination)

    found = list(dict.fromkeys(found))
    AUTOEXEC_DIR_CACHE[package] = found
    return found


def install_script_heartbeat_autoexec(
    package: str,
    config: dict[str, Any],
) -> bool:
    if not config.get("script_heartbeat_enabled", True):
        return True

    directories = find_autoexec_dirs(package)
    if not directories:
        logger.error("Không tìm thấy autoexec/autoexecute cho %s", package)
        return False

    interval = float(config.get("script_heartbeat_interval_seconds", 3))
    source = build_script_heartbeat_lua(package, interval)

    ensure_dir()
    fd, temporary = tempfile.mkstemp(
        prefix="termux_hb_",
        suffix=".lua",
        dir=str(APP_DIR),
        text=True,
    )

    installed = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(source)
            file.flush()
            os.fsync(file.fileno())

        for directory in directories:
            destination = f"{directory.rstrip('/')}/__termux_script_heartbeat.lua"
            try:
                result = root_run(["cp", temporary, destination], timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                logger.exception("Copy heartbeat lỗi: %s", destination)
                continue

            if result.returncode != 0:
                logger.warning(
                    "Copy heartbeat thất bại %s: %s",
                    destination,
                    result.stderr.strip(),
                )
                continue

            root_run(["chmod", "0644", destination], timeout=5)
            # Best-effort SELinux context repair cho file nằm trong app-private data.
            try:
                root_run(["restorecon", destination], timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            logger.info("SCRIPT HEARTBEAT AUTOEXEC -> %s", destination)
            installed = True

    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass

    return installed


def install_all_script_heartbeats(
    config: dict[str, Any],
    states: dict[str, PackageState],
) -> bool:
    if not config.get("script_heartbeat_enabled", True):
        return True

    ok = True
    packages = list(dict.fromkeys(config.get("selected_packages", [])))
    for package in packages:
        state = states[package]
        state.phase = "HB_SETUP"
        state.detail = "Đang cài Script Heartbeat vào autoexec"
        write_runtime(states, "HEARTBEAT_SETUP")

        if install_script_heartbeat_autoexec(package, config):
            state.detail = "Đã cài Script Heartbeat"
        else:
            state.phase = "ERROR"
            state.detail = "Không tìm thấy/cài được autoexec"
            ok = False

        write_runtime(states, "HEARTBEAT_SETUP")

    return ok


def _heartbeat_search_roots(package: str) -> list[str]:
    roots = package_storage_roots(package)
    for autoexec in find_autoexec_dirs(package):
        path = Path(autoexec)
        for parent in [path.parent, path.parent.parent, path.parent.parent.parent]:
            parent_text = str(parent)
            if parent_text and parent_text != ".":
                roots.append(parent_text)

    common = [
        "/storage/emulated/0/Delta",
        "/storage/emulated/0/delta",
        "/storage/emulated/0/ArceusX",
        "/storage/emulated/0/Arceus",
    ]
    roots.extend(_existing_root_dirs(common))
    return list(dict.fromkeys(roots))


def find_script_heartbeat_path(package: str) -> str | None:
    marker = script_heartbeat_marker(package)
    cached = SCRIPT_HB_PATH_CACHE.get(package)

    # Cached path sẽ được kiểm chứng trực tiếp bằng lệnh cat ở reader;
    # tránh thêm một subprocess `test` ở mỗi heartbeat cycle.
    if cached:
        return cached

    now = time.monotonic()
    last_scan = SCRIPT_HB_LAST_SCAN.get(package, 0.0)
    if now - last_scan < 2.0:
        return None
    SCRIPT_HB_LAST_SCAN[package] = now

    for root in _heartbeat_search_roots(package):
        try:
            result = root_run(
                [
                    "find",
                    root,
                    "-maxdepth",
                    "8",
                    "-type",
                    "f",
                    "-name",
                    marker,
                    "-print",
                    "-quit",
                ],
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue

        if result.returncode == 0:
            paths = result.stdout.strip().splitlines()
            if paths:
                SCRIPT_HB_PATH_CACHE[package] = paths[0]
                return paths[0]

    return None


def read_script_heartbeat(package: str) -> dict[str, Any] | None:
    path = find_script_heartbeat_path(package)
    if not path:
        return None

    try:
        result = root_run(["cat", path], timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        SCRIPT_HB_PATH_CACHE.pop(package, None)
        return None

    raw = result.stdout.strip()
    parts = raw.split("|", 3)
    if len(parts) != 4:
        return None

    try:
        timestamp = float(parts[0])
    except ValueError:
        return None

    now = time.time()
    age = now - timestamp
    if age < -60:
        return None

    return {
        "timestamp": timestamp,
        "age": max(0.0, age),
        "user_id": parts[1],
        "place_id": parts[2],
        "job_id": parts[3],
        "path": path,
    }


def update_script_heartbeat_state(
    package: str,
    config: dict[str, Any],
    state: PackageState,
    minimum_timestamp: float = 0.0,
) -> bool:
    if not config.get("script_heartbeat_enabled", True):
        state.script_heartbeat_alive = True
        state.script_heartbeat_age = 0.0
        return True

    heartbeat = read_script_heartbeat(package)
    if heartbeat is None:
        state.script_heartbeat_alive = False
        state.script_heartbeat_age = None
        return False

    state.script_heartbeat_age = float(heartbeat["age"])
    state.script_heartbeat_path = str(heartbeat["path"])
    state.script_user_id = str(heartbeat["user_id"])
    state.script_place_id = str(heartbeat["place_id"])
    state.script_job_id = str(heartbeat["job_id"])

    timeout = max(
        2.0,
        min(
            120.0,
            float(config.get("script_heartbeat_timeout_seconds", 12)),
        ),
    )

    fresh = (
        float(heartbeat["timestamp"]) >= minimum_timestamp
        and float(heartbeat["age"]) <= timeout
    )
    state.script_heartbeat_alive = fresh
    return fresh


def wait_script_heartbeat_ready(
    package: str,
    config: dict[str, Any],
    state: PackageState,
    states: dict[str, PackageState],
    minimum_timestamp: float,
) -> bool:
    if not config.get("script_heartbeat_enabled", True):
        return True

    if not config.get("script_heartbeat_required_for_ready", True):
        return True

    timeout = max(
        5.0,
        min(
            300.0,
            float(config.get("script_heartbeat_start_timeout_seconds", 45)),
        ),
    )
    poll = max(
        0.5,
        min(3.0, float(config.get("ready_poll_seconds", 1))),
    )
    started = time.monotonic()

    while not STOP_EVENT.is_set():
        if time.monotonic() - started >= timeout:
            state.phase = "ERROR"
            state.detail = f"Timeout Script Heartbeat sau {timeout:.0f}s"
            state.script_heartbeat_alive = False
            write_runtime(states, "STARTING")
            return False

        state.phase = "WAITING_SCRIPT"
        if update_script_heartbeat_state(
            package,
            config,
            state,
            minimum_timestamp=minimum_timestamp,
        ):
            age = state.script_heartbeat_age or 0.0
            state.phase = "IN_GAME"
            state.detail = f"Đã vào game | Script ALIVE {age:.1f}s"
            state.consecutive_failures = 0
            state.last_seen = time.time()
            write_runtime(states, "RUNNING")
            return True

        elapsed = time.monotonic() - started
        state.detail = f"Chờ script heartbeat {elapsed:.0f}/{timeout:.0f}s"
        write_runtime(states, "STARTING")
        if STOP_EVENT.wait(poll):
            return False

    return False


def force_stop_all(
    config: dict[str, Any],
    states: dict[str, PackageState],
) -> None:
    packages = list(dict.fromkeys(config.get("selected_packages", [])))

    for package in packages:
        state = states[package]
        state.phase = "CLOSING"
        state.detail = "Đang đóng app trước khi bắt đầu"
        write_runtime(states, "STARTING")
        force_stop_package(package)
        state.pid = None
        state.alive = False
        state.phase = "STOPPED"
        state.detail = "Đã đóng"

    delay = max(
        0,
        min(30, int(config.get("close_all_wait_seconds", 2))),
    )
    if delay:
        STOP_EVENT.wait(delay)


def launch_with_retry(
    package: str,
    uri: str,
    config: dict[str, Any],
    state: PackageState,
    states: dict[str, PackageState],
    phase: str = "OPENING",
) -> bool:
    attempts = max(
        1,
        min(5, int(config.get("launch_attempts", 3))),
    )

    for attempt in range(1, attempts + 1):
        if STOP_EVENT.is_set():
            return False

        state.phase = phase
        state.detail = f"Đang mở app - lần {attempt}/{attempts}"
        write_runtime(states, "STARTING")

        if send_join_intent(package, uri):
            pid = get_package_pid_root(package)
            if pid is not None:
                if state.previous_pid and state.previous_pid != pid:
                    state.restart_count += 1
                state.previous_pid = pid
                state.pid = pid
                state.alive = True
                return True

            # am start có thể trả về trước khi process xuất hiện.
            wait_deadline = time.monotonic() + 6
            while time.monotonic() < wait_deadline and not STOP_EVENT.is_set():
                pid = get_package_pid_root(package)
                if pid is not None:
                    if state.previous_pid and state.previous_pid != pid:
                        state.restart_count += 1
                    state.previous_pid = pid
                    state.pid = pid
                    state.alive = True
                    return True
                STOP_EVENT.wait(0.5)

        if attempt < attempts:
            delay = min(
                10.0,
                (2 ** (attempt - 1)) + random.uniform(0.2, 1.0),
            )
            state.detail = f"Mở thất bại, thử lại sau {delay:.1f}s"
            write_runtime(states, "STARTING")
            if STOP_EVENT.wait(delay):
                return False

    state.phase = "ERROR"
    state.detail = "Không mở được app"
    state.alive = False
    state.pid = None
    write_runtime(states, "STARTING")
    return False


def wait_package_ready(
    package: str,
    config: dict[str, Any],
    state: PackageState,
    states: dict[str, PackageState],
    launch_timestamp: float | None = None,
) -> bool:
    stable_required = max(
        1.0,
        min(120.0, float(config.get("ready_stable_seconds", 8))),
    )
    timeout = max(
        stable_required,
        min(600.0, float(config.get("ready_timeout_seconds", 90))),
    )
    poll = max(
        0.5,
        min(5.0, float(config.get("ready_poll_seconds", 1))),
    )

    started = time.monotonic()
    started_epoch = time.time()
    minimum_hb_timestamp = launch_timestamp or started_epoch
    stable_started: float | None = None
    last_pid: int | None = None

    while not STOP_EVENT.is_set():
        now = time.monotonic()

        if now - started >= timeout:
            state.phase = "ERROR"
            state.detail = f"Timeout PID READY sau {timeout:.0f}s"
            state.alive = False
            write_runtime(states, "STARTING")
            return False

        pid = get_package_pid_root(package)

        if pid is None:
            stable_started = None
            last_pid = None
            state.pid = None
            state.alive = False
            state.phase = "WAITING_READY"
            state.detail = "Chờ process xuất hiện"
        else:
            if stable_started is None:
                stable_started = now
                last_pid = pid
            elif last_pid is not None and pid != last_pid:
                stable_started = now
                last_pid = pid
                state.restart_count += 1

            stable_time = now - stable_started
            state.pid = pid
            state.previous_pid = pid
            state.alive = True
            state.phase = "WAITING_READY"
            state.detail = (
                f"Đang vào game - PID ổn định "
                f"{min(stable_time, stable_required):.0f}/{stable_required:.0f}s"
            )

            if stable_time >= stable_required:
                write_runtime(states, "STARTING")
                return wait_script_heartbeat_ready(
                    package,
                    config,
                    state,
                    states,
                    minimum_timestamp=minimum_hb_timestamp,
                )

        write_runtime(states, "STARTING")
        if STOP_EVENT.wait(poll):
            return False

    return False


def minimize_current_app(
    package: str,
    config: dict[str, Any],
    state: PackageState,
    states: dict[str, PackageState],
) -> bool:
    """
    Auto sort tab = đưa app vừa READY về Home/background.

    Không kill process, không force-stop. Package vẫn nằm trong Android
    recent tasks. Lệnh HOME là cách tương thích rộng hơn freeform window.
    """
    if not config.get("auto_sort_tab", False):
        state.minimized = False
        return True

    state.detail = (
        f"{state.detail} | Đang thu nhỏ"
        if state.detail
        else "Đang thu nhỏ app"
    )
    write_runtime(states, "SORTING_TABS")

    try:
        result = root_run(
            [
                "input",
                "keyevent",
                "KEYCODE_HOME",
            ],
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(
            "AUTO SORT: không thu nhỏ được %s: %s",
            package,
            exc,
        )
        state.minimized = False
        return False

    if result.returncode != 0:
        logger.warning(
            "AUTO SORT: HOME thất bại %s: %s",
            package,
            result.stderr.strip(),
        )
        state.minimized = False
        return False

    delay = max(
        0.0,
        min(
            5.0,
            float(
                config.get(
                    "auto_sort_home_delay_seconds",
                    0.8,
                )
            ),
        ),
    )

    if delay and STOP_EVENT.wait(delay):
        return False

    state.minimized = True

    age = state.script_heartbeat_age
    if config.get("script_heartbeat_enabled", True):
        state.detail = (
            f"Đã vào game | Script ALIVE {(age or 0.0):.1f}s "
            f"| Đã thu nhỏ"
        )
    else:
        state.detail = "Đã vào game | Đã thu nhỏ"

    write_runtime(states, "SORTING_TABS")

    logger.info(
        "AUTO SORT: %s -> background/Home",
        package,
    )

    return True


def bring_termux_foreground() -> bool:
    """
    Best-effort đưa Termux trở lại foreground sau khi đã thu nhỏ toàn bộ
    Roblox apps để dashboard hiện lại.

    Không bắt buộc thành công; worker vẫn chạy nếu ROM chặn activity start.
    """
    commands = [
        [
            "am",
            "start",
            "--user",
            str(android_user()),
            "-n",
            "com.termux/.app.TermuxActivity",
        ],
        [
            "monkey",
            "-p",
            "com.termux",
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
        ],
    ]

    for command in commands:
        try:
            result = root_run(
                command,
                timeout=8,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue

        if result.returncode == 0:
            logger.info("AUTO SORT: đưa Termux về foreground")
            return True

    logger.warning(
        "AUTO SORT: không tự đưa được Termux về foreground"
    )
    return False



def sequential_start(
    config: dict[str, Any],
    states: dict[str, PackageState],
    force_close_first: bool,
) -> bool:
    packages = list(dict.fromkeys(config.get("selected_packages", [])))
    if not packages:
        return False

    if force_close_first:
        force_stop_all(config, states)

    for index, package in enumerate(packages, start=1):
        if STOP_EVENT.is_set():
            return False

        state = states[package]
        state.minimized = False
        state.phase = "OPENING"
        state.detail = f"Đang mở app {index}/{len(packages)}"
        write_runtime(states, "STARTING")

        if not package_exists(package):
            state.phase = "ERROR"
            state.detail = "Package không tồn tại"
            write_runtime(states, "STARTING")
            return False

        try:
            uri = build_target_uri(
                config,
                package,
            )
        except ValueError as exc:
            state.phase = "ERROR"
            state.detail = f"Target lỗi: {exc}"
            write_runtime(states, "STARTING")
            return False

        launch_timestamp = time.time()

        if not launch_with_retry(
            package,
            uri,
            config,
            state,
            states,
            phase="OPENING",
        ):
            return False

        if not wait_package_ready(
            package,
            config,
            state,
            states,
            launch_timestamp=launch_timestamp,
        ):
            return False

        if config.get("auto_sort_tab", False):
            minimize_current_app(
                package,
                config,
                state,
                states,
            )

        gap = max(0, min(30, int(config.get("launch_gap", 2))))
        if index < len(packages) and gap:
            if STOP_EVENT.wait(gap):
                return False

    if (
        config.get("auto_sort_tab", False)
        and config.get("return_termux_after_sort", True)
    ):
        bring_termux_foreground()

    return True


def sync_states(
    config: dict[str, Any],
    states: dict[str, PackageState],
) -> None:
    packages = list(dict.fromkeys(config.get("selected_packages", [])))
    usernames = config.get("package_usernames", {})
    wanted = set(packages)

    for package in list(states):
        if package not in wanted:
            del states[package]

    for package in packages:
        username = str(usernames.get(package, "-") or "-")
        if package not in states:
            states[package] = PackageState(
                package=package,
                username=username,
            )
        else:
            states[package].username = username


def write_runtime(
    states: dict[str, PackageState],
    worker_phase: str,
) -> None:
    payload = {
        "timestamp": time.time(),
        "worker_pid": os.getpid(),
        "worker_phase": worker_phase,
        "packages": {
            package: asdict(state)
            for package, state in states.items()
        },
    }
    try:
        atomic_json_write(RUNTIME_FILE, payload)
    except OSError:
        logger.exception("Không ghi được runtime state")


def worker_signal_handler(signum: int, _frame: Any) -> None:
    logger.info("Nhận signal %s -> graceful shutdown", signum)
    STOP_EVENT.set()



def configure_worker_stop_deadline(config: dict[str, Any]) -> None:
    """
    Worker timer dùng deadline epoch lưu trong config để worker crash/restart
    không reset thời gian mà người dùng đã đặt.
    """
    deadline = float(config.get("stop_deadline_epoch", 0.0) or 0.0)

    if deadline <= 0:
        return

    def timer_thread() -> None:
        remaining = deadline - time.time()

        if remaining > 0:
            if STOP_EVENT.wait(remaining):
                return

        try:
            latest = load_config()

            # Chỉ timer đúng phiên này mới được tắt worker.
            latest_deadline = float(
                latest.get("stop_deadline_epoch", 0.0) or 0.0
            )

            if (
                latest.get("auto_rejoin", False)
                and latest_deadline > 0
                and abs(latest_deadline - deadline) < 1.0
                and time.time() >= latest_deadline
            ):
                latest["auto_rejoin"] = False
                latest["last_stop_reason"] = "timer"
                save_config(latest)

                logger.info(
                    "Đã đạt thời gian dừng tool %.2f phút",
                    float(latest.get("stop_after_minutes", 0.0) or 0.0),
                )

                STOP_EVENT.set()

        except Exception:
            logger.exception("Stop timer gặp lỗi")

    threading.Thread(
        target=timer_thread,
        name="auto-stop-timer",
        daemon=True,
    ).start()



def worker_main() -> int:
    setup_logging()

    try:
        require_root()
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 1

    lock_handle = LOCK_FILE.open("a+", encoding="utf-8")
    try:
        fcntl.flock(
            lock_handle.fileno(),
            fcntl.LOCK_EX | fcntl.LOCK_NB,
        )
    except BlockingIOError:
        logger.error("Worker khác đang chạy")
        lock_handle.close()
        return 1

    signal.signal(signal.SIGINT, worker_signal_handler)
    signal.signal(signal.SIGTERM, worker_signal_handler)
    signal.signal(signal.SIGHUP, worker_signal_handler)

    atomic_json_write(
        PID_FILE,
        {"pid": os.getpid(), "started": time.time()},
    )

    states: dict[str, PackageState] = {}
    logger.info("ROOT worker started PID=%d", os.getpid())

    try:
        config = load_config()
        sync_states(config, states)
        write_runtime(states, "STARTING")
        configure_worker_stop_deadline(config)

        if not config.get("auto_rejoin", False):
            return 0

        # Khởi động tuần tự. Nếu lỗi thì self-heal bằng cách thử lại cả chuỗi.
        startup_failures = 0
        while not STOP_EVENT.is_set():
            config = load_config()
            sync_states(config, states)

            if not config.get("auto_rejoin", False):
                return 0

            heartbeat_installed = install_all_script_heartbeats(config, states)
            if not heartbeat_installed:
                startup_failures += 1
                delay = min(
                    60.0,
                    5.0 * (2 ** min(startup_failures - 1, 3))
                    + random.uniform(0.5, 2.0),
                )
                logger.error(
                    "Không cài đủ Script Heartbeat autoexec; retry sau %.1fs",
                    delay,
                )
                for state in states.values():
                    if state.phase == "ERROR":
                        state.detail += f" | retry {delay:.0f}s"
                write_runtime(states, "RECOVERING")
                if STOP_EVENT.wait(delay):
                    return 0
                continue

            if config.get("close_all_before_start", True):
                force_stop_all(config, states)

            if config.get("auto_cross_block", False) and startup_failures == 0:
                completed, total = run_cross_block(config, states)
                logger.info(
                    "Cross block startup hoàn tất: %d/%d action thành công",
                    completed,
                    total,
                )
                # Đóng các profile đang mở trước khi vào game.
                force_stop_all(config, states)

            ok = sequential_start(
                config,
                states,
                force_close_first=False,
            )
            if ok:
                break

            startup_failures += 1
            delay = min(
                60.0,
                5.0 * (2 ** min(startup_failures - 1, 3))
                + random.uniform(0.5, 2.0),
            )
            logger.warning(
                "Startup sequence lỗi; thử lại sau %.1fs",
                delay,
            )
            for state in states.values():
                if state.phase != "IN_GAME":
                    state.detail = f"Startup lỗi, thử lại sau {delay:.0f}s"
            write_runtime(states, "RECOVERING")
            if STOP_EVENT.wait(delay):
                return 0

        last_hard_rejoin = time.monotonic()

        while not STOP_EVENT.is_set():
            config = load_config()
            sync_states(config, states)

            if not config.get("auto_rejoin", False):
                break

            threshold = max(
                1,
                min(
                    10,
                    int(
                        config.get(
                            "heartbeat_failure_threshold",
                            3,
                        )
                    ),
                ),
            )

            # Ghi CHECKING trước scan để dashboard có trạng thái rõ ràng.
            for state in states.values():
                if state.phase not in {"OPENING", "WAITING_READY", "REJOINING"}:
                    state.phase = "CHECKING"
                    state.detail = "Kiểm tra trạng thái acc"
            write_runtime(states, "RUNNING")

            snapshot = get_process_snapshot()
            now = time.time()

            for package, state in states.items():
                pid = find_package_pid(package, snapshot)

                if pid is not None:
                    if (
                        state.previous_pid is not None
                        and state.previous_pid != pid
                    ):
                        state.restart_count += 1

                    state.previous_pid = pid
                    state.pid = pid
                    state.alive = True
                    state.last_seen = now

                    script_alive = update_script_heartbeat_state(
                        package,
                        config,
                        state,
                    )

                    if script_alive:
                        state.consecutive_failures = 0
                        age = state.script_heartbeat_age or 0.0
                        state.phase = "IN_GAME"
                        state.detail = (
                            f"Đã vào game | Script ALIVE {age:.1f}s"
                            + (" | Đã thu nhỏ" if state.minimized else "")
                        )
                        continue

                    if not config.get("script_heartbeat_enabled", True):
                        state.consecutive_failures = 0
                        state.phase = "IN_GAME"
                        state.detail = (
                            "Đã vào game"
                            + (" | Đã thu nhỏ" if state.minimized else "")
                        )
                        continue

                    state.minimized = False
                    state.phase = "REJOINING"
                    if state.script_heartbeat_age is None:
                        state.detail = "Script heartbeat LOST - đang rejoin"
                    else:
                        state.detail = (
                            f"Script heartbeat LOST "
                            f"({state.script_heartbeat_age:.1f}s) - đang rejoin"
                        )
                    write_runtime(states, "RUNNING")
                    force_stop_package(package)
                    if STOP_EVENT.wait(1):
                        break

                else:
                    state.pid = None
                    state.alive = False
                    state.script_heartbeat_alive = False
                    state.consecutive_failures += 1

                    if state.consecutive_failures < threshold:
                        state.phase = "CHECKING"
                        state.detail = (
                            f"Kiểm tra trạng thái acc "
                            f"{state.consecutive_failures}/{threshold}"
                        )
                        continue

                    state.minimized = False
                    state.phase = "REJOINING"
                    state.detail = "Mất process heartbeat - đang rejoin"
                    write_runtime(states, "RUNNING")

                try:
                    uri = build_target_uri(
                        config,
                        package,
                    )
                except ValueError as exc:
                    state.phase = "ERROR"
                    state.detail = str(exc)
                    continue

                launch_timestamp = time.time()
                if launch_with_retry(
                    package,
                    uri,
                    config,
                    state,
                    states,
                    phase="REJOINING",
                ) and wait_package_ready(
                    package,
                    config,
                    state,
                    states,
                    launch_timestamp=launch_timestamp,
                ):
                    state.last_rejoin = time.time()
                    state.consecutive_failures = 0
                    age = state.script_heartbeat_age
                    if config.get("script_heartbeat_enabled", True):
                        state.detail = (
                            f"Đã vào game | Script ALIVE {(age or 0.0):.1f}s"
                        )
                    else:
                        state.detail = "Đã vào game"
                    state.phase = "IN_GAME"

                    if config.get("auto_sort_tab", False):
                        minimize_current_app(
                            package,
                            config,
                            state,
                            states,
                        )
                        if config.get("return_termux_after_sort", True):
                            bring_termux_foreground()
                else:
                    state.phase = "ERROR"
                    state.detail = "Rejoin/Script heartbeat thất bại"

            write_runtime(states, "RUNNING")

            hard_minutes = max(
                0,
                int(config.get("hard_rejoin_minutes", 0)),
            )
            if hard_minutes > 0:
                elapsed = time.monotonic() - last_hard_rejoin
                if elapsed >= hard_minutes * 60:
                    logger.warning("Periodic hard rejoin")
                    if sequential_start(
                        config,
                        states,
                        force_close_first=True,
                    ):
                        last_hard_rejoin = time.monotonic()

            interval = max(
                5,
                min(
                    300,
                    int(config.get("check_interval", 10)),
                ),
            )
            STOP_EVENT.wait(interval)

    except Exception:
        logger.exception("Worker crash ngoài dự kiến")
        return 1

    finally:
        for state in states.values():
            state.phase = "STOPPED"
            state.detail = "Worker đã dừng"
        write_runtime(states, "STOPPED")

        try:
            PID_FILE.unlink(missing_ok=True)
        except OSError:
            pass

        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        finally:
            lock_handle.close()

        logger.info("Worker stopped gracefully")

    return 0


def read_worker_pid() -> int | None:
    try:
        data = json.loads(PID_FILE.read_text(encoding="utf-8"))
        pid = int(data["pid"])
        return pid if pid > 1 else None
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None


def valid_worker_pid(pid: int) -> bool:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False

    cmdline = raw.replace(b"\x00", b" ").decode(errors="replace")
    return "--worker" in cmdline and Path(__file__).name in cmdline


def worker_status() -> str:
    pid = read_worker_pid()
    if pid is not None and valid_worker_pid(pid):
        return f"RUNNING PID={pid}"
    return "STOPPED"


def start_worker() -> bool:
    pid = read_worker_pid()
    if pid is not None and valid_worker_pid(pid):
        return True

    try:
        subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--worker",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        print(f"Không start được worker: {exc}")
        return False

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        pid = read_worker_pid()
        if pid is not None and valid_worker_pid(pid):
            return True
        time.sleep(0.1)

    print(f"Worker không start được. Log: {LOG_FILE}")
    return False


def stop_worker() -> bool:
    config = load_config()
    config["auto_rejoin"] = False
    if config.get("last_stop_reason") != "timer":
        config["last_stop_reason"] = "manual"
    save_config(config)

    pid = read_worker_pid()
    if pid is None:
        return True

    if not valid_worker_pid(pid):
        PID_FILE.unlink(missing_ok=True)
        return False

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True

    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if not valid_worker_pid(pid):
            return True
        time.sleep(0.2)

    try:
        os.kill(pid, signal.SIGKILL)
        return True
    except ProcessLookupError:
        return True


def parse_selection(value: str, maximum: int) -> list[int]:
    selected: set[int] = set()

    for item in value.split(","):
        item = item.strip()
        if not item:
            continue

        if "-" in item:
            left, right = item.split("-", 1)
            if not left.isdigit() or not right.isdigit():
                raise ValueError("Range không hợp lệ")

            start = int(left)
            end = int(right)
            if start > end:
                start, end = end, start

            for number in range(start, end + 1):
                if 1 <= number <= maximum:
                    selected.add(number)
        else:
            if not item.isdigit():
                raise ValueError("Số không hợp lệ")
            number = int(item)
            if not 1 <= number <= maximum:
                raise ValueError(f"{number} ngoài phạm vi")
            selected.add(number)

    return sorted(selected)


def parse_target_input(value: str) -> tuple[dict[str, str], str]:
    """
    Tự nhận dạng đúng 2 loại input ở mục 2:
      1) Game Place ID
      2) Server VIP/private-server link Roblox
    """
    raw = value.strip()

    if not raw:
        return (
            default_blox_fruits_target(),
            "Blox Fruits mặc định",
        )

    # Chỉ toàn số -> Game Place ID.
    if valid_place_id(raw):
        return (
            {
                "mode": "place",
                "place_id": raw,
                "server_id": "",
                "access_code": "",
                "link_code": "",
                "raw_uri": "",
            },
            "Game Place ID",
        )

    # Roblox custom scheme.
    if raw.lower().startswith("roblox://"):
        lowered = raw.lower()

        vip_markers = (
            "linkcode=",
            "accesscode=",
            "share_links",
            "type=server",
            "privateserver",
        )

        if not any(marker in lowered for marker in vip_markers):
            raise ValueError(
                "Link roblox:// này không được nhận diện là Server VIP link"
            )

        return (
            {
                "mode": "raw",
                "place_id": "",
                "server_id": "",
                "access_code": "",
                "link_code": "",
                "raw_uri": raw,
            },
            "Server VIP Link",
        )

    try:
        parsed = urlparse(raw)
    except ValueError as exc:
        raise ValueError(
            "Input không phải Game Place ID hoặc Server VIP link"
        ) from exc

    if parsed.scheme not in {"http", "https"}:
        raise ValueError(
            "Chỉ chấp nhận Game Place ID hoặc link http/https Roblox"
        )

    host = (parsed.hostname or "").lower()

    allowed_host = (
        host == "roblox.com"
        or host.endswith(".roblox.com")
        or host == "ro.blox.com"
    )

    if not allowed_host:
        raise ValueError("Server VIP link phải thuộc Roblox")

    query = parse_qs(parsed.query, keep_blank_values=True)

    # Classic private server:
    # https://www.roblox.com/games/<placeId>/...?privateServerLinkCode=<code>
    private_codes = (
        query.get("privateServerLinkCode")
        or query.get("privateserverlinkcode")
        or []
    )

    if private_codes:
        link_code = str(private_codes[0]).strip()
        match = re.search(
            r"/games/(\d+)(?:/|$)",
            parsed.path,
            re.IGNORECASE,
        )

        if match and link_code:
            return (
                {
                    "mode": "link_code",
                    "place_id": match.group(1),
                    "server_id": "",
                    "access_code": "",
                    "link_code": link_code,
                    "raw_uri": "",
                },
                "Server VIP Link",
            )

        return (
            {
                "mode": "raw",
                "place_id": "",
                "server_id": "",
                "access_code": "",
                "link_code": "",
                "raw_uri": raw,
            },
            "Server VIP Link",
        )

    # Share server:
    # https://www.roblox.com/share?code=...&type=Server
    share_code = str((query.get("code") or [""])[0]).strip()
    share_type = str((query.get("type") or [""])[0]).strip().lower()

    if share_code and share_type == "server":
        return (
            {
                "mode": "raw",
                "place_id": "",
                "server_id": "",
                "access_code": "",
                "link_code": "",
                "raw_uri": raw,
            },
            "Server VIP Link",
        )

    # Roblox short link: giữ nguyên, Android/Roblox tự resolve.
    if host == "ro.blox.com":
        return (
            {
                "mode": "raw",
                "place_id": "",
                "server_id": "",
                "access_code": "",
                "link_code": "",
                "raw_uri": raw,
            },
            "Server VIP/Roblox Short Link",
        )

    raise ValueError(
        "Không nhận diện được Server VIP link. "
        "Hãy nhập Game Place ID hoặc link VIP/private server Roblox."
    )


def configure_target(config: dict[str, Any]) -> None:
    print(
        """
========================================
        CHỌN GAME / SERVER VIP
========================================
1. Nhập cho từng package
2. Nhập cho tất cả các package
0. Quay lại
"""
    )

    choice = input("Chọn: ").strip()

    if choice == "0":
        return

    if choice == "1":
        packages = list(
            dict.fromkeys(
                config.get("selected_packages", [])
            )
        )

        if not packages:
            print(
                "Chưa có package. Hãy chọn package ở mục 3 trước "
                "khi dùng chế độ nhập riêng từng package."
            )
            return

        package_targets = config.setdefault(
            "package_targets",
            {},
        )

        if not isinstance(package_targets, dict):
            package_targets = {}
            config["package_targets"] = package_targets

        print(
            "\nNhập Game Place ID hoặc ServerVip Link cho từng package."
        )
        print(
            "Để trống sẽ tự dùng Blox Fruits mặc định."
        )

        for index, package in enumerate(packages, start=1):
            print(
                f"\n[{index}/{len(packages)}] {package}"
            )

            raw = input(
                "Nhập Game Place ID hoặc ServerVip Link: "
            ).strip()

            try:
                target, detected_type = parse_target_input(raw)
            except ValueError as exc:
                print(f"Không hợp lệ: {exc}")
                return

            package_targets[package] = target

            try:
                uri = target_to_uri(target)
            except ValueError as exc:
                print(f"Target lỗi: {exc}")
                return

            print(
                f"→ {detected_type}: {uri}"
            )

        config["target_scope"] = "per_package"
        save_config(config)

        print(
            "\nĐã lưu target riêng cho từng package."
        )
        return

    if choice == "2":
        print(
            "\nNhập Game Place ID hoặc ServerVip Link."
        )
        print(
            "Để trống sẽ tự dùng Blox Fruits mặc định."
        )

        raw = input(
            "Nhập Game Place ID hoặc ServerVip Link: "
        ).strip()

        try:
            target, detected_type = parse_target_input(raw)
        except ValueError as exc:
            print(f"Không hợp lệ: {exc}")
            return

        config["target"] = target
        config["target_scope"] = "all"
        save_config(config)

        print(
            f"Đã nhận diện: {detected_type}"
        )
        print(
            "Target:",
            target_to_uri(target),
        )
        return

    print("Lựa chọn không hợp lệ")



def configure_packages(config: dict[str, Any]) -> None:
    print("\nROOT: đang quét package...")
    try:
        packages = scan_packages()
    except RuntimeError as exc:
        print(exc)
        return

    current = set(config.get("selected_packages", []))

    print("\n========================================")
    print("              PACKAGE")
    print("========================================")

    for index, package in enumerate(packages, start=1):
        marker = "X" if package in current else " "
        print(f"{index:3}. [{marker}] {package}")

    print(
        """
A. Tự chọn package theo số
B. Tự nhận diện Roblox
0. Quay lại
"""
    )

    choice = input("Chọn: ").strip().upper()

    if choice == "A":
        raw = input("VD: 1,3,5-8\n> ").strip()
        try:
            numbers = parse_selection(raw, len(packages))
        except ValueError as exc:
            print(exc)
            return
        selected = [packages[number - 1] for number in numbers]

    elif choice == "B":
        selected = auto_detect_roblox(packages)
    else:
        return

    config["selected_packages"] = selected

    usernames = config.setdefault("package_usernames", {})
    uids = config.setdefault("package_uids", {})
    for package in list(usernames):
        if package not in selected:
            usernames.pop(package, None)
    for package in list(uids):
        if package not in selected:
            uids.pop(package, None)

    save_config(config)

    print("\nĐã chọn:")
    for package in selected:
        print(" ✓", package)


def valid_user_id(value: str) -> bool:
    return value.isdigit() and int(value) > 0


def configure_usernames(config: dict[str, Any]) -> None:
    packages = config.get("selected_packages", [])
    usernames = config.setdefault("package_usernames", {})
    uids = config.setdefault("package_uids", {})

    print("\nUsername/UID chỉ dùng làm mapping local; tool không đọc mật khẩu/cookie.")
    for package in packages:
        current_name = str(usernames.get(package, "") or "")
        current_uid = str(uids.get(package, "") or "")

        value = input(
            f"{package}\nUsername [{current_name or '-'}]: "
        ).strip()
        if value:
            usernames[package] = value
        elif package not in usernames:
            usernames[package] = "-"

        uid_value = input(
            f"UID [{current_uid or '-'}]: "
        ).strip()
        if uid_value:
            if not valid_user_id(uid_value):
                print("UID không hợp lệ; giữ nguyên UID cũ.")
            else:
                uids[package] = uid_value

    save_config(config)


def configure_auto_rejoin(config: dict[str, Any]) -> bool:
    packages = config.get("selected_packages", [])
    if not packages:
        print("Chưa chọn package ở mục 3.")
        return False

    for package in packages:
        try:
            build_target_uri(
                config,
                package,
            )
        except ValueError as exc:
            print(
                f"Target của {package} không hợp lệ: {exc}"
            )
            return False

    configure_usernames(config)

    print(
        """
========================================
      AUTO REJOIN + HEARTBEAT
========================================
"""
    )

    stop_minutes_raw = input(
        "Thời gian dừng tool (phút, 0 = bỏ qua): "
    ).strip()

    if not stop_minutes_raw:
        stop_minutes = float(
            config.get(
                "stop_after_minutes",
                0.0,
            )
            or 0.0
        )
    else:
        try:
            stop_minutes = float(stop_minutes_raw)
        except ValueError:
            print("Thời gian dừng tool không hợp lệ")
            return False

    if stop_minutes < 0:
        print("Thời gian dừng tool không được âm")
        return False

    config["stop_after_minutes"] = stop_minutes

    if stop_minutes > 0:
        config["stop_deadline_epoch"] = (
            time.time()
            + stop_minutes * 60.0
        )
    else:
        config["stop_deadline_epoch"] = 0.0

    config["last_stop_reason"] = ""

    interval = input(
        f"Heartbeat interval [{config.get('check_interval', 10)}s]: "
    ).strip()
    if interval:
        if not interval.isdigit():
            print("Interval không hợp lệ")
            return False
        config["check_interval"] = max(5, min(300, int(interval)))

    threshold = input(
        "Heartbeat fail để rejoin "
        f"[{config.get('heartbeat_failure_threshold', 3)}]: "
    ).strip()
    if threshold:
        if not threshold.isdigit():
            print("Threshold không hợp lệ")
            return False
        config["heartbeat_failure_threshold"] = max(
            1,
            min(10, int(threshold)),
        )

    stable = input(
        "App chạy ổn định bao nhiêu giây mới mở app kế "
        f"[{config.get('ready_stable_seconds', 8)}]: "
    ).strip()
    if stable:
        try:
            config["ready_stable_seconds"] = max(
                1,
                min(120, float(stable)),
            )
        except ValueError:
            print("READY time không hợp lệ")
            return False

    timeout = input(
        "Timeout chờ READY "
        f"[{config.get('ready_timeout_seconds', 90)}s]: "
    ).strip()
    if timeout:
        if not timeout.isdigit():
            print("Timeout không hợp lệ")
            return False
        config["ready_timeout_seconds"] = max(
            10,
            min(600, int(timeout)),
        )

    script_interval = input(
        "Script heartbeat mỗi bao nhiêu giây "
        f"[{config.get('script_heartbeat_interval_seconds', 3)}]: "
    ).strip()
    if script_interval:
        try:
            config["script_heartbeat_interval_seconds"] = max(
                1.0,
                min(30.0, float(script_interval)),
            )
        except ValueError:
            print("Script heartbeat interval không hợp lệ")
            return False

    script_timeout = input(
        "Bao lâu không có Script heartbeat thì rejoin "
        f"[{config.get('script_heartbeat_timeout_seconds', 12)}s]: "
    ).strip()
    if script_timeout:
        try:
            config["script_heartbeat_timeout_seconds"] = max(
                2.0,
                min(120.0, float(script_timeout)),
            )
        except ValueError:
            print("Script heartbeat timeout không hợp lệ")
            return False

    script_start_timeout = input(
        "Timeout chờ script autoexec chạy "
        f"[{config.get('script_heartbeat_start_timeout_seconds', 45)}s]: "
    ).strip()
    if script_start_timeout:
        try:
            config["script_heartbeat_start_timeout_seconds"] = max(
                5.0,
                min(300.0, float(script_start_timeout)),
            )
        except ValueError:
            print("Script start timeout không hợp lệ")
            return False

    hard = input(
        "Hard Rejoin định kỳ phút "
        f"[{config.get('hard_rejoin_minutes', 0)} = tắt]: "
    ).strip()
    if hard:
        if not hard.isdigit():
            print("Hard Rejoin không hợp lệ")
            return False
        config["hard_rejoin_minutes"] = int(hard)

    config["auto_rejoin"] = True
    save_config(config)

    if start_worker():
        return True

    config["auto_rejoin"] = False
    save_config(config)
    return False


class CpuSampler:
    def __init__(self) -> None:
        self.previous_total: int | None = None
        self.previous_idle: int | None = None

    def sample(self) -> float | None:
        try:
            first = Path("/proc/stat").read_text(
                encoding="utf-8"
            ).splitlines()[0]
        except OSError:
            return None

        parts = first.split()
        if not parts or parts[0] != "cpu":
            return None

        try:
            values = [int(value) for value in parts[1:]]
        except ValueError:
            return None

        if len(values) < 4:
            return None

        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)

        if self.previous_total is None or self.previous_idle is None:
            self.previous_total = total
            self.previous_idle = idle
            return None

        total_delta = total - self.previous_total
        idle_delta = idle - self.previous_idle

        self.previous_total = total
        self.previous_idle = idle

        if total_delta <= 0:
            return None

        return max(
            0.0,
            min(
                100.0,
                100.0 * (total_delta - idle_delta) / total_delta,
            ),
        )


def read_ram_usage() -> tuple[float, float, float] | None:
    try:
        lines = Path("/proc/meminfo").read_text(
            encoding="utf-8"
        ).splitlines()
    except OSError:
        return None

    values: dict[str, int] = {}
    for line in lines:
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        number = rest.strip().split()[0]
        if number.isdigit():
            values[key] = int(number)

    total_kb = values.get("MemTotal")
    available_kb = values.get("MemAvailable")
    if not total_kb or available_kb is None:
        return None

    used_kb = total_kb - available_kb
    used_gb = used_kb / 1024 / 1024
    total_gb = total_kb / 1024 / 1024
    percent = used_kb * 100.0 / total_kb
    return used_gb, total_gb, percent


def read_runtime() -> dict[str, Any]:
    try:
        return json.loads(RUNTIME_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "worker_phase": "STARTING",
            "packages": {},
        }


def display_status(state: dict[str, Any]) -> str:
    phase = str(state.get("phase", "UNKNOWN"))
    failures = int(state.get("consecutive_failures", 0) or 0)

    if phase in {"CLOSING", "OPENING", "WAITING_READY", "WAITING_SCRIPT", "HB_SETUP", "REJOINING"}:
        return str(state.get("detail") or "Đang vào game")

    if phase == "IN_GAME":
        return str(state.get("detail") or "Đã vào game")

    if phase == "CHECKING":
        if failures > 0:
            return str(state.get("detail") or "Kiểm tra trạng thái acc")
        return "Kiểm tra trạng thái acc"

    if phase == "ERROR":
        return str(state.get("detail") or "Lỗi")

    if phase == "STOPPED":
        return "Đã đóng"

    return str(state.get("detail") or "Đang kiểm tra")


def truncate(text: str, width: int) -> str:
    text = str(text)
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"


def render_dashboard(cpu: float | None) -> str:
    config = load_config()
    runtime = read_runtime()

    ram = read_ram_usage()
    cpu_text = "--" if cpu is None else f"{cpu:5.1f}%"

    if ram is None:
        ram_text = "--"
    else:
        used, total, percent = ram
        ram_text = f"{used:.2f}/{total:.2f} GB ({percent:.1f}%)"

    states = runtime.get("packages", {})

    lines = [
        "\033[2J\033[H",
        "==============================================================",
        "              ROBLOX ROOT REJOIN DASHBOARD",
        "==============================================================",
        f" CPU: {cpu_text}   |   RAM: {ram_text}",
        "--------------------------------------------------------------",
        f"{'PACKAGE':<28} | {'USERNAME':<16} | TRẠNG THÁI",
        "--------------------------------------------------------------",
    ]

    for package in config.get("selected_packages", []):
        state = states.get(package, {})
        username = (
            state.get("username")
            or config.get("package_usernames", {}).get(package)
            or "-"
        )
        status = display_status(state)
        lines.append(
            f"{truncate(package, 28):<28} | "
            f"{truncate(username, 16):<16} | "
            f"{status}"
        )

    if not config.get("selected_packages"):
        lines.append("(chưa chọn package)")

    lines.append("==============================================================")
    return "\n".join(lines)


def dashboard_loop() -> None:
    sampler = CpuSampler()
    sampler.sample()

    try:
        while True:
            cycle_started = time.monotonic()

            # Nếu worker crash nhưng Auto Rejoin vẫn ON, dashboard tự kéo worker lên lại.
            config = load_config()
            current_worker_status = worker_status()

            if (
                not config.get("auto_rejoin", False)
                and current_worker_status == "STOPPED"
                and config.get("last_stop_reason") == "timer"
            ):
                sys.stdout.write("\033[2J\033[H")
                sys.stdout.flush()
                print(
                    "Đã đạt thời gian dừng tool. "
                    "Auto Rejoin đã tự dừng."
                )
                raise SystemExit(0)

            if (
                config.get("auto_rejoin", False)
                and current_worker_status == "STOPPED"
            ):
                start_worker()

            cpu = sampler.sample()
            sys.stdout.write(render_dashboard(cpu))
            sys.stdout.write("\n")
            sys.stdout.flush()

            elapsed = time.monotonic() - cycle_started
            if elapsed < 1.0:
                time.sleep(1.0 - elapsed)

    except KeyboardInterrupt:
        # Theo yêu cầu: Ctrl+C là đường thoát duy nhất khỏi dashboard
        # và đồng thời tắt Auto Rejoin/worker.
        stop_worker()
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
        print("Auto Rejoin đã dừng bằng Ctrl+C.")
        raise SystemExit(0)


def print_runtime_detail() -> None:
    runtime = read_runtime()
    print(json.dumps(runtime, ensure_ascii=False, indent=2))



def configure_auto_sort_tab(config: dict[str, Any]) -> None:
    enabled = bool(config.get("auto_sort_tab", False))
    config["auto_sort_tab"] = not enabled
    save_config(config)

    if config["auto_sort_tab"]:
        print(
            "Auto sort tab: ON\n"
            "Khi bật mục 1, mỗi app sau READY + Script Heartbeat "
            "sẽ được đưa về background/Home trước khi mở app kế tiếp."
        )
    else:
        print("Auto sort tab: OFF")



def interactive_menu() -> None:
    setup_logging()
    require_root()

    while True:
        config = load_config()
        selected = config.get("selected_packages", [])
        auto = "ON" if config.get("auto_rejoin", False) else "OFF"

        target = target_summary(config)

        print(
            f"""
========================================
       ROBLOX ROOT AUTO REJOIN
========================================
ROOT: UID 0

1. Auto Rejoin + Dashboard
   Auto: {auto}
   Worker: {worker_status()}

2. Chọn Game Place ID / ServerVip Link
   {target}

3. Chọn package
   Đã chọn: {len(selected)}

4. Mở tất cả theo thứ tự READY
5. Xem runtime/heartbeat JSON
6. Auto block chéo account: {"ON" if config.get("auto_cross_block", False) else "OFF"}
7. Auto sort tab: {"ON" if config.get("auto_sort_tab", False) else "OFF"}
0. Thoát
========================================
"""
        )

        choice = input("Chọn: ").strip()

        if choice == "1":
            if not config.get("auto_rejoin", False):
                if not configure_auto_rejoin(config):
                    continue
            elif worker_status() == "STOPPED":
                start_worker()

            dashboard_loop()

        elif choice == "2":
            configure_target(config)

        elif choice == "3":
            configure_packages(config)

        elif choice == "4":
            # Chạy một worker tạm trong foreground sẽ phức tạp lifecycle;
            # dùng cùng startup engine bằng cách bật Auto Rejoin rồi dashboard.
            print(
                "Mục 4 dùng chung cơ chế mục 1. "
                "Hãy bật mục 1 để đóng hết -> mở tuần tự -> READY."
            )

        elif choice == "5":
            print_runtime_detail()

        elif choice == "6":
            configure_cross_block(config)

        elif choice == "7":
            configure_auto_sort_tab(config)

        elif choice == "0":
            return

        else:
            print("Lựa chọn không hợp lệ")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Roblox Termux ROOT Auto Rejoin + Dashboard"
    )
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--dashboard", action="store_true")
    args = parser.parse_args()

    setup_logging()

    try:
        require_root()
    except RuntimeError as exc:
        print(exc)
        return 1

    if args.worker:
        return worker_main()

    if args.status:
        print(worker_status())
        print_runtime_detail()
        return 0

    if args.stop:
        return 0 if stop_worker() else 1

    if args.dashboard:
        dashboard_loop()
        return 0

    try:
        interactive_menu()
    except KeyboardInterrupt:
        stop_worker()
        print("\nTool đã dừng bằng Ctrl+C.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
