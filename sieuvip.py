import html
import json
import math
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple


class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    CYAN = "\033[36m"
    MAGENTA = "\033[35m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    BLUE = "\033[34m"


class DeviceController:
    PROTOCOL_ACTIVITY = "com.roblox.client.ActivityProtocolLaunch"

    @staticmethod
    def exec_cmd(command: str) -> Tuple[bool, str]:
        try:
            res = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=25,
            )
            output = "\n".join(
                part.strip() for part in (res.stdout, res.stderr) if part and part.strip()
            )
            return res.returncode == 0, output
        except Exception as exc:
            return False, str(exc)

    @staticmethod
    def _command_succeeded(ok: bool, output: str) -> bool:
        if not ok:
            return False
        lowered = output.lower()
        failure_markers = (
            "error:",
            "exception",
            "securityexception",
            "permission denial",
            "unable to resolve intent",
            "does not exist",
            "no activities found to run",
            "monkey aborted",
        )
        return not any(marker in lowered for marker in failure_markers)

    @classmethod
    def get_all_packages(cls) -> List[str]:
        packages = set()
        for cmd in (
            "pm list packages -u --user all",
            "pm list packages -f",
            "pm list packages",
        ):
            ok, out = cls.exec_cmd(cmd)
            if ok and out:
                for line in out.splitlines():
                    clean_line = line.strip()
                    if clean_line.startswith("package:"):
                        pkg = clean_line.replace("package:", "", 1).split("=")[-1].strip()
                        if pkg:
                            packages.add(pkg)

        ok, data_out = cls.exec_cmd("ls -1 /data/data/")
        if ok and data_out:
            for item in data_out.splitlines():
                item = item.strip()
                if "." in item:
                    packages.add(item)
        return sorted(packages)

    @classmethod
    def kill_package(cls, pkg: str) -> bool:
        ok, out = cls.exec_cmd(f"am force-stop {shlex.quote(pkg)}")
        return cls._command_succeeded(ok, out)

    @classmethod
    def is_package_running(cls, pkg: str) -> bool:
        ok, out = cls.exec_cmd(f"pidof {shlex.quote(pkg)}")
        return ok and bool(out.strip())

    @classmethod
    def _wait_until_running(cls, pkg: str, timeout: float = 4.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cls.is_package_running(pkg):
                return True
            time.sleep(0.5)
        return False

    @classmethod
    def open_lobby(cls, pkg: str) -> Tuple[bool, str]:
        """Force-stop rồi mở launcher thật của app để tạo trạng thái khởi động sạch."""
        cls.kill_package(pkg)
        time.sleep(0.8)

        quoted_pkg = shlex.quote(pkg)
        commands = (
            f"monkey -p {quoted_pkg} -c android.intent.category.LAUNCHER 1",
            (
                "am start -W -a android.intent.action.MAIN "
                f"-c android.intent.category.LAUNCHER -p {quoted_pkg}"
            ),
        )
        errors = []
        for command in commands:
            ok, out = cls.exec_cmd(command)
            if cls._command_succeeded(ok, out):
                running = cls._wait_until_running(pkg)
                if running:
                    return True, "Launcher đã mở và process đang chạy"
                # Một số ROM chặn pidof đối với app khác dù intent đã được nhận.
                return True, "Launcher đã nhận lệnh; không đọc được PID"
            errors.append(out or "Lệnh mở launcher thất bại")
        return False, " | ".join(errors[-2:])

    @classmethod
    def _start_url(
        cls,
        pkg: str,
        url: str,
        freeform: bool = False,
        bounds: Optional[str] = None,
    ) -> Tuple[bool, str]:
        quoted_pkg = shlex.quote(pkg)
        quoted_component = shlex.quote(f"{pkg}/{cls.PROTOCOL_ACTIVITY}")
        quoted_url = shlex.quote(url)

        # --bounds không tồn tại trên một số Android/ROM. Thử bản đầy đủ trước,
        # sau đó tự hạ xuống chỉ windowingMode rồi full screen.
        option_variants: List[str] = []
        if freeform and bounds:
            option_variants.append(
                f"--windowingMode 5 --bounds {shlex.quote(bounds)}"
            )
        if freeform:
            option_variants.append("--windowingMode 5")
        option_variants.append("")

        errors = []
        for options in option_variants:
            prefix = f"am start -W {options}".strip()
            commands = (
                (
                    f"{prefix} -a android.intent.action.VIEW -d {quoted_url} "
                    f"-n {quoted_component}"
                ),
                (
                    f"{prefix} -a android.intent.action.VIEW -d {quoted_url} "
                    f"-p {quoted_pkg}"
                ),
            )
            for command in commands:
                ok, out = cls.exec_cmd(command)
                if cls._command_succeeded(ok, out):
                    running = cls._wait_until_running(pkg, timeout=2.5)
                    if running:
                        return True, "Intent join đã nhận; process đang chạy"
                    # am start -W thành công vẫn là bằng chứng tốt hơn pidof trên ROM bị hạn chế.
                    return True, "Intent join đã được Android chấp nhận; chưa xác nhận được PID"
                errors.append(out or "am start không trả về chi tiết")

        return False, " | ".join(errors[-3:])

    @staticmethod
    def _is_launchable_url(raw_link: str) -> bool:
        parsed = urllib.parse.urlparse(raw_link)
        if parsed.scheme.lower() in ("roblox", "roblox-player"):
            return True
        if parsed.scheme.lower() not in ("http", "https"):
            return False
        host = (parsed.hostname or "").lower()
        return host == "roblox.com" or host.endswith(".roblox.com") or host == "ro.blox.com"

    @classmethod
    def launch_place(
        cls,
        pkg: str,
        raw_link: str,
        place_id: Optional[str],
        job_id: Optional[str] = None,
        link_code: Optional[str] = None,
        freeform: bool = False,
        bounds: Optional[str] = None,
        force_stop: bool = True,
    ) -> Tuple[bool, str]:
        """Gửi deep link và trả lại kết quả thật, không tự gán nhãn Joined."""
        if force_stop:
            cls.kill_package(pkg)
            time.sleep(0.8)

        urls: List[str] = []
        if place_id:
            query = {"placeId": place_id}
            if job_id:
                query["gameInstanceId"] = job_id
            elif link_code:
                query["linkCode"] = link_code
            encoded_query = urllib.parse.urlencode(query)
            # Format direct-to-app được Roblox công bố chính thức.
            urls.append("roblox://" + encoded_query)
            # Giữ fallback cho một số bản client/clone đang nhận route này.
            urls.append(
                "roblox://experiences/start?" + encoded_query
            )

        clean_link = html.unescape(raw_link.strip().strip("'\""))
        if cls._is_launchable_url(clean_link) and clean_link not in urls:
            # Cần thiết cho link share mới: URL có code nhưng không chứa placeId.
            urls.append(clean_link)

        if not urls:
            return False, "Không tìm được Place ID hoặc Roblox URL hợp lệ"

        errors = []
        for url in urls:
            ok, detail = cls._start_url(pkg, url, freeform=freeform, bounds=bounds)
            if ok:
                return True, detail
            errors.append(detail)
        return False, " | ".join(errors[-2:])

    @classmethod
    def set_android_id(cls, new_id: str) -> bool:
        ok, out = cls.exec_cmd(
            f"settings put secure android_id {shlex.quote(new_id)}"
        )
        return cls._command_succeeded(ok, out)

    @classmethod
    def inject_cookie_to_pkg(cls, pkg: str, raw_cookie: str) -> bool:
        c = raw_cookie.strip()
        if not c.startswith("_|WARNING:-DO-NOT-SHARE-THIS"):
            c = (
                "_|WARNING:-DO-NOT-SHARE-THIS.--Sharing-this-will-allow-someone-"
                "to-log-into-your-account-and-rob-your-robox.--|_" + c
            )

        cls.kill_package(pkg)
        time.sleep(0.5)
        xml_path = f"/data/data/{pkg}/shared_prefs/com.roblox.client_preferences.xml"
        # Giữ tương thích với source cũ. Chức năng cookie cần quyền root ghi /data/data.
        cmd = (
            f"mkdir -p /data/data/{pkg}/shared_prefs && "
            f"if [ ! -f '{xml_path}' ]; then "
            f"echo '<?xml version=\"1.0\" encoding=\"utf-8\" standalone=\"yes\" ?><map><string name=\"RBXSession\">{c}</string></map>' > '{xml_path}'; "
            f"else "
            f"if grep -q '\"RBXSession\"' '{xml_path}'; then "
            f"sed -i 's|<string name=\"RBXSession\">.*</string>|<string name=\"RBXSession\">{c}</string>|g' '{xml_path}'; "
            f"else "
            f"sed -i 's|</map>|<string name=\"RBXSession\">{c}</string></map>|g' '{xml_path}'; "
            f"fi; "
            f"fi && chmod 660 '{xml_path}' && chown $(stat -c '%u:%g' /data/data/{pkg}) '{xml_path}'"
        )
        ok, out = cls.exec_cmd(cmd)
        return cls._command_succeeded(ok, out)


class RobloxRejoinEngine:
    CONFIG_FILE = "/sdcard/Download/sieuvip_config.json"
    COOKIE_FILE = "/sdcard/Download/cookie.txt"

    def __init__(self) -> None:
        self.config: Dict = self._load_config()
        self.device = DeviceController()
        self.status_map: Dict[str, str] = {}
        self.username_map: Dict[str, str] = {}
        self.ui_running = False
        self.global_status = "Đang khởi tạo..."
        self._last_cpu_idle = 0
        self._last_cpu_total = 0

    def _load_config(self) -> Dict:
        default_cfg = {
            "auto_resize": False,
            "auto_arrange": False,
            "packages": [],
            "server_links": {},
            "cookies": {},
        }
        if os.path.exists(self.CONFIG_FILE):
            try:
                with open(self.CONFIG_FILE, "r", encoding="utf-8") as file:
                    cfg = json.load(file)
                if isinstance(cfg, dict):
                    default_cfg.update(cfg)
            except (OSError, ValueError, TypeError):
                pass
        return default_cfg

    def _save_config(self) -> None:
        with open(self.CONFIG_FILE, "w", encoding="utf-8") as file:
            json.dump(self.config, file, indent=4)

    def _read_cookie_file(self) -> List[str]:
        if not os.path.exists(self.COOKIE_FILE):
            return []
        try:
            with open(self.COOKIE_FILE, "r", encoding="utf-8") as file:
                return [
                    line.strip()
                    for line in file
                    if line.strip() and not line.lstrip().startswith("#")
                ]
        except OSError:
            return []

    def fetch_username(self, cookie: str) -> str:
        try:
            req = urllib.request.Request(
                "https://users.roblox.com/v1/users/authenticated"
            )
            req.add_header("Cookie", f".ROBLOSECURITY={cookie}")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode())
            name = data.get("name", "Unknown")
            return f"****{name[-4:]}" if len(name) > 4 else f"****{name}"
        except Exception:
            return "Unknown"

    def parse_place_info(
        self, raw_input: str
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """Parse ID/link cũ, link deep-link và query không phân biệt hoa thường."""
        clean = html.unescape(raw_input.strip().strip("'\""))
        # Link đôi khi được copy ở dạng URL-encoded.
        for _ in range(2):
            decoded = urllib.parse.unquote(clean)
            if decoded == clean:
                break
            clean = decoded

        if clean.isdigit():
            return clean, None, None

        parsed = urllib.parse.urlparse(clean)
        query = {
            key.lower(): values
            for key, values in urllib.parse.parse_qs(
                parsed.query, keep_blank_values=False
            ).items()
        }

        def first_query(*keys: str) -> Optional[str]:
            for key in keys:
                values = query.get(key.lower())
                if values and values[0]:
                    return values[0]
            return None

        place_id = first_query("placeId", "placeID")
        job_id = first_query("gameInstanceId", "jobId")
        link_code = first_query("privateServerLinkCode", "linkCode")

        if not place_id:
            match = re.search(r"(?i)/(?:games|experiences)/(\d+)", clean)
            if match:
                place_id = match.group(1)
        if not place_id:
            match = re.search(r"(?i)\bplaceid=(\d+)", clean)
            if match:
                place_id = match.group(1)
        if not job_id:
            match = re.search(
                r"(?i)\b(?:gameinstanceid|jobid)=([a-z0-9-]+)", clean
            )
            if match:
                job_id = match.group(1)
        if not link_code:
            match = re.search(
                r"(?i)\b(?:privateserverlinkcode|linkcode)=([a-z0-9_-]+)",
                clean,
            )
            if match:
                link_code = match.group(1)

        return place_id, job_id, link_code

    def get_system_stats(self) -> Tuple[str, str]:
        ram_usage = "N/A"
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as file:
                lines = file.readlines()
            total = free = buffers = cached = 0
            for line in lines:
                if line.startswith("MemTotal"):
                    total = int(line.split()[1])
                elif line.startswith("MemFree"):
                    free = int(line.split()[1])
                elif line.startswith("Buffers"):
                    buffers = int(line.split()[1])
                elif line.startswith("Cached"):
                    cached = int(line.split()[1])
            if total > 0:
                ram_usage = (
                    f"{((total - free - buffers - cached) / total) * 100:.1f}%"
                )
        except (OSError, ValueError, IndexError):
            pass

        cpu_usage = "N/A"
        try:
            with open("/proc/stat", "r", encoding="utf-8") as file:
                line = file.readline()
            parts = [int(value) for value in line.split()[1:]]
            idle = parts[3]
            total = sum(parts)
            delta_idle = idle - self._last_cpu_idle
            delta_total = total - self._last_cpu_total
            self._last_cpu_idle, self._last_cpu_total = idle, total
            if delta_total > 0:
                cpu_usage = f"{100.0 * (1.0 - delta_idle / delta_total):.1f}%"
        except (OSError, ValueError, IndexError):
            pass
        return cpu_usage, ram_usage

    def get_screen_size(self) -> Tuple[int, int]:
        ok, out = self.device.exec_cmd("wm size")
        width, height = 720, 1280
        if ok and out:
            # Ưu tiên Override size nếu wm size trả về cả Physical và Override.
            matches = re.findall(r"(?i)(\d+)x(\d+)", out)
            if matches:
                width, height = map(int, matches[-1])
        return width, height

    @staticmethod
    def _fit(value: str, width: int) -> str:
        if len(value) <= width:
            return value
        return value[: max(0, width - 1)] + "…"

    def live_dashboard_thread(self) -> None:
        while self.ui_running:
            cpu, ram = self.get_system_stats()
            os.system("cls" if os.name == "nt" else "clear")
            print(f"\n{' ' * 20}{Colors.CYAN}CPU: {cpu} | RAM: {ram}{Colors.RESET}\n")

            w_pkg, w_usr, w_status = 22, 16, 38
            print(f"┌{'─' * w_pkg}┬{'─' * w_usr}┬{'─' * w_status}┐")
            print(
                f"│ {Colors.MAGENTA}{'Package':<{w_pkg - 1}}{Colors.RESET}"
                f"│ {Colors.MAGENTA}{'Username':<{w_usr - 1}}{Colors.RESET}"
                f"│ {Colors.MAGENTA}{'Status':<{w_status - 1}}{Colors.RESET}│"
            )
            print(f"├{'─' * w_pkg}┼{'─' * w_usr}┼{'─' * w_status}┤")

            for pkg in self.config.get("packages", []):
                shown_pkg = self._fit(pkg, w_pkg - 2)
                username = self._fit(self.username_map.get(pkg, "Unknown"), w_usr - 2)
                status = self._fit(self.status_map.get(pkg, "Waiting..."), w_status - 2)
                print(
                    f"│ {Colors.CYAN}{shown_pkg:<{w_pkg - 1}}{Colors.RESET}"
                    f"│ {Colors.GREEN}{username:<{w_usr - 1}}{Colors.RESET}"
                    f"│ {Colors.GREEN}{status:<{w_status - 1}}{Colors.RESET}│"
                )

            print(f"└{'─' * w_pkg}┴{'─' * w_usr}┴{'─' * w_status}┘")
            print(f"\n{Colors.YELLOW}[*] {self.global_status}{Colors.RESET}")
            print(
                f"{Colors.MAGENTA}Bấm Ctrl+C để dừng và quay lại menu chính...{Colors.RESET}"
            )
            time.sleep(1)

    def _calculate_grid_bounds(self, pkgs: List[str]) -> List[str]:
        if not self.config.get("auto_arrange", False):
            return []
        width, height = self.get_screen_size()
        count = len(pkgs)
        columns = math.ceil(math.sqrt(count))
        rows = math.ceil(count / columns)
        cell_width, cell_height = width // columns, height // rows
        bounds = []
        for index in range(count):
            left = (index % columns) * cell_width
            top = (index // columns) * cell_height
            right = width if index % columns == columns - 1 else left + cell_width
            bottom = height if index // columns == rows - 1 else top + cell_height
            bounds.append(f"{left},{top},{right},{bottom}")
        return bounds

    def run_rejoin_sequence(self, with_bypass: bool = False) -> None:
        pkgs = list(dict.fromkeys(self.config.get("packages", [])))
        if not pkgs:
            print(f"{Colors.RED}[!] Chưa có app. Hãy thiết lập mục 3 trước.{Colors.RESET}")
            time.sleep(1.5)
            return

        print(f"\n{Colors.CYAN}--- Cài đặt thời gian chạy ---{Colors.RESET}")
        try:
            raw_time = input(
                f"{Colors.MAGENTA}Nhập thời gian chờ để lặp lại (phút) "
                f"[0 = chạy một lần và giữ app]: {Colors.RESET}"
            ).strip()
            interval_minutes = float(raw_time) if raw_time else 0.0
            if interval_minutes < 0:
                raise ValueError
            interval_seconds = int(interval_minutes * 60)
        except ValueError:
            print(f"{Colors.RED}[!] Thời gian không hợp lệ; dùng chế độ chạy một lần.{Colors.RESET}")
            interval_seconds = 0
            time.sleep(1.2)

        print(f"{Colors.YELLOW}[*] Đang trích xuất username...{Colors.RESET}")
        self.status_map = {pkg: "Chuẩn bị..." for pkg in pkgs}
        self.username_map = {}
        for pkg in pkgs:
            cookie = self.config.get("cookies", {}).get(pkg)
            self.username_map[pkg] = self.fetch_username(cookie) if cookie else "Unknown"

        auto_resize = self.config.get("auto_resize", False)
        auto_arrange = self.config.get("auto_arrange", False)
        freeform_enabled = auto_resize or auto_arrange
        grid_bounds = self._calculate_grid_bounds(pkgs)

        self.ui_running = True
        ui_thread = threading.Thread(target=self.live_dashboard_thread, daemon=True)
        ui_thread.start()
        cycle = 0

        try:
            while True:
                cycle += 1
                warmup_ok: Dict[str, bool] = {}

                # Đợt 1 chỉ force-stop một lần rồi mở launcher thật. Không dùng
                # ActivityProtocolLaunch rỗng vì activity này cần deep-link data.
                self.global_status = f"Chu kỳ {cycle} - Đợt 1: khởi động sảnh..."
                for pkg in pkgs:
                    self.status_map[pkg] = "Đợt 1: mở launcher..."
                    ok, detail = self.device.open_lobby(pkg)
                    warmup_ok[pkg] = ok
                    self.status_map[pkg] = (
                        "Đợt 1: sảnh sẵn sàng"
                        if ok
                        else f"Đợt 1 lỗi: {detail}"
                    )

                # Đợt 2 KHÔNG force-stop lại nếu warm-up thành công. Đây là lỗi
                # logic chính trong source cũ. Nếu lần đầu lỗi, retry mới force-stop.
                self.global_status = f"Chu kỳ {cycle} - Đợt 2: gửi lệnh join..."
                success_count = 0
                for index, pkg in enumerate(pkgs):
                    raw_link = self.config.get("server_links", {}).get(pkg, "").strip()
                    place_id, job_id, link_code = self.parse_place_info(raw_link)
                    if not place_id and not self.device._is_launchable_url(
                        html.unescape(raw_link.strip().strip("'\""))
                    ):
                        self.status_map[pkg] = "Lỗi: Link/Place ID không hợp lệ"
                        continue

                    if with_bypass and not self.device.set_android_id(os.urandom(8).hex()):
                        self.status_map[pkg] = "Cảnh báo: đổi Android ID thất bại"

                    bounds = (
                        grid_bounds[index]
                        if auto_arrange
                        else ("0,0,600,800" if auto_resize else None)
                    )
                    self.status_map[pkg] = "Đợt 2: đang gửi deep link..."
                    ok, detail = self.device.launch_place(
                        pkg=pkg,
                        raw_link=raw_link,
                        place_id=place_id,
                        job_id=job_id,
                        link_code=link_code,
                        freeform=freeform_enabled,
                        bounds=bounds,
                        force_stop=not warmup_ok.get(pkg, False),
                    )

                    if not ok:
                        self.status_map[pkg] = "Retry: force-stop + join thẳng..."
                        ok, detail = self.device.launch_place(
                            pkg=pkg,
                            raw_link=raw_link,
                            place_id=place_id,
                            job_id=job_id,
                            link_code=link_code,
                            freeform=freeform_enabled,
                            bounds=bounds,
                            force_stop=True,
                        )

                    if ok:
                        success_count += 1
                        self.status_map[pkg] = "Đã gửi join; app đang chạy"
                    else:
                        self.status_map[pkg] = f"Join thất bại: {detail}"
                    time.sleep(1.0)

                if interval_seconds <= 0:
                    self.global_status = (
                        f"Hoàn tất: {success_count}/{len(pkgs)} app nhận lệnh join. "
                        "Không tự đóng/rejoin lại."
                    )
                    while True:
                        time.sleep(1)

                deadline = time.monotonic() + interval_seconds
                while True:
                    remaining = max(0, math.ceil(deadline - time.monotonic()))
                    if remaining <= 0:
                        break
                    minutes, seconds = divmod(remaining, 60)
                    self.global_status = (
                        f"Chu kỳ {cycle} xong ({success_count}/{len(pkgs)}). "
                        f"Rejoin tiếp sau {minutes:02d}:{seconds:02d}"
                    )
                    time.sleep(min(1.0, remaining))

        except KeyboardInterrupt:
            self.global_status = "Đã dừng auto rejoin."
        finally:
            self.ui_running = False
            ui_thread.join(timeout=2)

    def filter_and_select_packages(self) -> None:
        print(f"\n{Colors.CYAN}[*] Đang quét hệ thống...{Colors.RESET}")
        all_pkgs = self.device.get_all_packages()
        if not all_pkgs:
            print(f"{Colors.RED}[!] Không quét được package.{Colors.RESET}")
            time.sleep(1.5)
            return
        keyword = input(f"\n{Colors.MAGENTA}Nhập từ khóa package: {Colors.RESET}").strip().lower()
        if not keyword:
            return
        matched = [pkg for pkg in all_pkgs if keyword in pkg.lower()]
        if not matched:
            print(f"{Colors.RED}[!] Không có package phù hợp.{Colors.RESET}")
            time.sleep(1.5)
            return

        for index, pkg in enumerate(matched, start=1):
            print(f"  {index}. {pkg}")
        link = input(
            f"\n{Colors.MAGENTA}Nhập Server Link / Place ID: {Colors.RESET}"
        ).strip()
        self.config["packages"] = matched
        for pkg in matched:
            self.config.setdefault("server_links", {})[pkg] = link
        self._save_config()
        time.sleep(1)

    def auto_assign_all(self) -> None:
        link = input(
            f"\n{Colors.MAGENTA}Nhập 1 Link áp dụng cho tất cả Roblox packages: {Colors.RESET}"
        ).strip()
        all_pkgs = self.device.get_all_packages()
        matched = [
            pkg
            for pkg in all_pkgs
            if "roblox" in pkg.lower() or "clone" in pkg.lower()
        ]
        if not matched:
            return
        self.config["packages"] = matched
        for pkg in matched:
            self.config.setdefault("server_links", {})[pkg] = link
        self._save_config()
        time.sleep(1)

    def login_via_cookie_menu(self) -> None:
        cookies = self._read_cookie_file()
        if not cookies:
            return
        choice = input(
            f"\n{Colors.CYAN}[1] Gán tất cả tự động\n[2] Chọn app thủ công\n"
            f"{Colors.MAGENTA}Chọn (1/2): {Colors.RESET}"
        ).strip()
        pkgs = self.config.get("packages", [])

        if choice == "1":
            for index in range(min(len(pkgs), len(cookies))):
                self.device.inject_cookie_to_pkg(pkgs[index], cookies[index])
                self.config.setdefault("cookies", {})[pkgs[index]] = cookies[index]
        elif choice == "2":
            selected = []
            cookie_index = 0
            while cookie_index < len(cookies):
                for index, pkg in enumerate(pkgs, start=1):
                    selected_label = (
                        f"{Colors.GREEN}(Đã chọn){Colors.RESET}"
                        if pkg in selected
                        else ""
                    )
                    print(f" [{index}] {pkg} {selected_label}")
                pick = input(
                    f"{Colors.YELLOW}[0] Lưu lại\n{Colors.MAGENTA}Chọn app: {Colors.RESET}"
                ).strip()
                if pick == "0":
                    break
                if pick.isdigit() and 1 <= int(pick) <= len(pkgs):
                    pkg = pkgs[int(pick) - 1]
                    if pkg not in selected:
                        selected.append(pkg)
                        cookie_index += 1
            for index, pkg in enumerate(selected):
                self.device.inject_cookie_to_pkg(pkg, cookies[index])
                self.config.setdefault("cookies", {})[pkg] = cookies[index]
        self._save_config()

    def handle_config_menu(self) -> None:
        while True:
            os.system("cls" if os.name == "nt" else "clear")
            auto_resize = self.config.get("auto_resize", False)
            auto_arrange = self.config.get("auto_arrange", False)
            print(f"\n{Colors.CYAN}--- Configuration Menu ---{Colors.RESET}")
            print(
                f" [1] Auto làm nhỏ tab lại (Freeform) : "
                f"{Colors.GREEN if auto_resize else Colors.RED}{auto_resize}{Colors.RESET}"
            )
            print(
                f" [2] Auto sắp xếp các tab cho đều  : "
                f"{Colors.GREEN if auto_arrange else Colors.RED}{auto_arrange}{Colors.RESET}"
            )
            print(" [0] Quay lại")
            pick = input(
                f"\n{Colors.MAGENTA}Chọn config (0-2): {Colors.RESET}"
            ).strip()
            if pick == "1":
                self.config["auto_resize"] = not auto_resize
            elif pick == "2":
                self.config["auto_arrange"] = not auto_arrange
            elif pick == "0":
                break
            self._save_config()


class SieuVipProApp:
    def __init__(self) -> None:
        self.engine = RobloxRejoinEngine()
        self.width = 62

    def render(self) -> None:
        os.system("cls" if os.name == "nt" else "clear")
        print(f"{' ' * 16}⚡ {Colors.CYAN}{Colors.BOLD}SieuVipPro Menu{Colors.RESET}\n")
        menu_data = [
            ("Auto Rejoin", [(1, "Start auto rejoin"), (2, "Start auto rejoin with bypass")]),
            (
                "Server Setup",
                [
                    (3, "Select packages & assign server link"),
                    (4, "List selected packages"),
                    (5, "Auto-select all Roblox packages"),
                ],
            ),
            ("Tabs", [(6, "Open all Roblox tabs")]),
            (
                "Account / Cookie",
                [
                    (7, "Login via cookie"),
                    (8, "Logout Roblox"),
                    (9, "Fix login cookie"),
                    (10, "Export cookies"),
                ],
            ),
            ("System", [(11, "Set Android ID"), (12, "Download APK")]),
            ("", [(13, "Configuration Settings"), (0, "Exit")]),
        ]

        width = self.width
        print(f"┌{'─' * 6}┬{'─' * (width - 7)}┐")
        for index, (section, items) in enumerate(menu_data):
            if section:
                print(
                    f"│{' ' * 6}│ {Colors.BLUE}── {section} ──{Colors.RESET}"
                    f"{' ' * (width - 11 - len(section))}│"
                )
            for number, label in items:
                color = Colors.RED if number == 0 else (
                    Colors.GREEN if number == 13 else Colors.CYAN
                )
                print(
                    f"│ {Colors.MAGENTA}{number:>4}{Colors.RESET} │ "
                    f"{color}{label:<{width - 10}}{Colors.RESET}│"
                )
            if index < len(menu_data) - 1:
                print(f"├{'─' * 6}┼{'─' * (width - 7)}┤")
        print(f"└{'─' * 6}┴{'─' * (width - 7)}┘")

    def run(self) -> None:
        actions = {
            1: lambda: self.engine.run_rejoin_sequence(with_bypass=False),
            2: lambda: self.engine.run_rejoin_sequence(with_bypass=True),
            3: self.engine.filter_and_select_packages,
            5: self.engine.auto_assign_all,
            7: self.engine.login_via_cookie_menu,
            13: self.engine.handle_config_menu,
            0: lambda: sys.exit(0),
        }
        while True:
            self.render()
            try:
                raw = input(f"\n{Colors.MAGENTA}Enter choice: {Colors.RESET}").strip()
                if not raw:
                    continue
                choice = int(raw)
                action = actions.get(choice)
                if action:
                    action()
                else:
                    print(
                        f"{Colors.YELLOW}[!] Mục {choice} chưa được cài đặt trong source này."
                        f"{Colors.RESET}"
                    )
                    time.sleep(1.5)
            except ValueError:
                print(f"{Colors.RED}[!] Vui lòng nhập một số trong menu.{Colors.RESET}")
                time.sleep(1.2)
            except KeyboardInterrupt:
                print(f"\n{Colors.YELLOW}Đã thoát.{Colors.RESET}")
                return
            except Exception as exc:
                # Source cũ sys.exit(0) cho mọi lỗi nên app tắt im lặng, rất khó debug.
                print(f"{Colors.RED}[!] Lỗi: {exc}{Colors.RESET}")
                time.sleep(2)


if __name__ == "__main__":
    SieuVipProApp().run()
