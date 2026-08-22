import os
import sys
import time
import subprocess
import json
import re
import threading
import math
import urllib.request
import shlex
from typing import Dict, List, Optional, Tuple

class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    CYAN = "\033[36m"
    MAGENTA = "\033[35m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"

class DeviceController:
    """Tầng giao tiếp HĐH Android cấp thấp. Chịu trách nhiệm thực thi an toàn mọi lệnh Root."""
    
    @staticmethod
    def exec_cmd(command: str) -> Tuple[bool, str]:
        try:
            script_path = "/sdcard/Download/sv_cmd.sh"
            # Inject môi trường biến PATH gốc của Android để lách lỗi giả lập (UgPhone/VPhone)
            bash_script = (
                "#!/system/bin/sh\n"
                "export PATH=/sbin:/system/sbin:/system/bin:/system/xbin:/data/data/com.termux/files/usr/bin:$PATH\n"
                f"{command}\n"
            )
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(bash_script)
            
            # Gửi lệnh với DEVNULL để chặn deadlock tiến trình I/O
            res = subprocess.run(
                ["sh", script_path], 
                capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=20
            )
            output = f"{res.stdout}\n{res.stderr}".strip()
            
            # Escalation to Root nếu bị SELinux chặn
            if res.returncode != 0 and ("Permission denied" in output or "inaccessible" in output):
                res = subprocess.run(
                    ["su", "-c", f"sh {script_path}"], 
                    capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=20
                )
                output = f"{res.stdout}\n{res.stderr}".strip()
                
            return (res.returncode == 0), output
        except subprocess.TimeoutExpired:
            return False, "Timeout"
        except Exception as e:
            return False, str(e)

    @classmethod
    def get_all_packages(cls) -> List[str]:
        packages = set()
        for cmd in ["pm list packages -u --user all", "pm list packages -f", "pm list packages"]:
            ok, out = cls.exec_cmd(cmd)
            if ok and out:
                for line in out.splitlines():
                    clean_line = line.strip()
                    if clean_line.startswith("package:"):
                        pkg = clean_line.replace("package:", "").split("=")[-1].strip()
                        if pkg: packages.add(pkg)

        # Quét Fallback cấp thấp vào phân vùng Data
        ok, data_out = cls.exec_cmd("ls -1 /data/data/")
        if ok and data_out:
            for item in data_out.splitlines():
                if "." in item.strip(): packages.add(item.strip())
        return sorted(list(packages))

    @classmethod
    def kill_package(cls, pkg: str) -> None:
        cls.exec_cmd(f"am force-stop {pkg}")

    @classmethod
    def open_lobby(cls, pkg: str) -> None:
        """ĐỢT 1: Tối ưu Warm-up (Khởi tạo sảnh). Ép chết tiến trình cũ và gọi Monkey."""
        cls.kill_package(pkg)
        time.sleep(1.2)
        ok, out = cls.exec_cmd(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1 > /dev/null 2>&1")
        # Fallback Native Intent
        if not ok or "Error" in out or "aborted" in out.lower():
            cls.exec_cmd(f"am start -p {pkg}")

    @classmethod
    def launch_place(cls, pkg: str, place_id: str, job_id: Optional[str] = None, 
                     link_code: Optional[str] = None, share_code: Optional[str] = None, 
                     freeform: bool = False, bounds: Optional[str] = None) -> bool:
        """ĐỢT 2: Tiêm DeepLink mượt mà không Restart App (Chống mất kết nối)"""
        
        # Build URI một cách an toàn
        base_uri = f"roblox://experiences/start?placeId={place_id}"
        if job_id: base_uri += f"&gameInstanceId={job_id}"
        elif link_code: base_uri += f"&linkCode={link_code}"
        elif share_code: base_uri += f"&code={share_code}"
            
        safe_uri = shlex.quote(base_uri) # Kỹ thuật PRO: Chống shell injection
        cmd_base = f"-a android.intent.action.VIEW -d {safe_uri}"
        
        cmd_primary = f"am start -p {pkg} {cmd_base}"
        cmd_fallback = f"am start -n {pkg}/com.roblox.client.ActivityProtocolLaunch {cmd_base}"

        if freeform:
            cls.exec_cmd("settings put global enable_freeform_support 1")
            cls.exec_cmd("settings put global force_resizable_activities 1")
            ff_flags = "--windowingMode 5"
            if bounds: ff_flags += f" --bounds {bounds}"
            
            # Cố gắng áp dụng Config Kích thước
            ok, out = cls.exec_cmd(f"am start {ff_flags} -p {pkg} {cmd_base}")
            if not ok or "Error" in out or "Exception" in out:
                # Retry: Hủy Config nếu OS không hỗ trợ
                ok2, out2 = cls.exec_cmd(cmd_primary)
                return True if ok2 else cls.exec_cmd(cmd_fallback)[0]
            return True
        else:
            # Chạy chuẩn Normal Mode
            ok, out = cls.exec_cmd(cmd_primary)
            if not ok or "Error" in out or "Exception" in out:
                return cls.exec_cmd(cmd_fallback)[0]
            return True

    @classmethod
    def set_android_id(cls, new_id: str) -> bool:
        return cls.exec_cmd(f"settings put secure android_id {new_id}")[0]

    @classmethod
    def inject_cookie_to_pkg(cls, pkg: str, raw_cookie: str) -> bool:
        c = raw_cookie.strip()
        if not c.startswith("_|WARNING:-DO-NOT-SHARE-THIS"):
            c = f"_|WARNING:-DO-NOT-SHARE-THIS.--Sharing-this-will-allow-someone-to-log-into-your-account-and-rob-your-robox.--|_{c}"

        cls.kill_package(pkg)
        time.sleep(0.5)
        xml_path = f"/data/data/{pkg}/shared_prefs/com.roblox.client_preferences.xml"
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
        return cls.exec_cmd(cmd)[0]

class RobloxRejoinEngine:
    """Core Logic xử lý tiến trình tự động hóa."""
    
    CONFIG_FILE = "/sdcard/Download/sieuvip_config.json"
    COOKIE_FILE = "/sdcard/Download/cookie.txt"

    def __init__(self) -> None:
        self.config: Dict = self._load_config()
        self.device = DeviceController()
        
        # Thread-Safety Variables (Biến an toàn đa luồng)
        self._lock = threading.Lock()
        self.status_map: Dict[str, str] = {}
        self.username_map: Dict[str, str] = {}
        self.ui_running = False
        self.global_status = "System Initializing..."
        
        self._last_cpu_idle = 0
        self._last_cpu_total = 0

    def _load_config(self) -> Dict:
        default_cfg = {
            "auto_resize": False,
            "auto_arrange": False,
            "packages": [],
            "server_links": {},
            "cookies": {}
        }
        if os.path.exists(self.CONFIG_FILE):
            try:
                with open(self.CONFIG_FILE, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    default_cfg.update(cfg)
                    return default_cfg
            except Exception: pass
        return default_cfg

    def _save_config(self) -> None:
        with open(self.CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=4)

    def set_status(self, pkg: str, msg: str) -> None:
        """Thread-safe status update"""
        with self._lock:
            self.status_map[pkg] = msg

    def set_global_status(self, msg: str) -> None:
        with self._lock:
            self.global_status = msg

    def parse_place_info(self, raw_input: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        place_match = re.search(r"games/(\d+)", raw_input)
        place_id = place_match.group(1) if place_match else None
        if not place_id and raw_input.isdigit(): place_id = raw_input

        job_match = re.search(r"gameInstanceId=([a-f0-9\-]+)", raw_input)
        job_id = job_match.group(1) if job_match else None
        
        link_code_match = re.search(r"privateServerLinkCode=([a-zA-Z0-9\-_]+)", raw_input)
        link_code = link_code_match.group(1) if link_code_match else None
        
        share_code_match = re.search(r"share\?code=([a-zA-Z0-9\-_]+)", raw_input)
        share_code = share_code_match.group(1) if share_code_match else None

        return place_id, job_id, link_code, share_code

    def fetch_username(self, cookie: str) -> str:
        try:
            req = urllib.request.Request("https://users.roblox.com/v1/users/authenticated")
            req.add_header("Cookie", f".ROBLOSECURITY={cookie}")
            resp = urllib.request.urlopen(req, timeout=3)
            data = json.loads(resp.read().decode())
            name = data.get("name", "Unknown")
            return f"****{name[-4:]}" if len(name) > 4 else f"****{name}"
        except Exception:
            return "Unknown"

    def get_system_stats(self) -> Tuple[str, str]:
        # Fast & Native Linux Sys Info Parser
        ram_usage, cpu_usage = "N/A", "N/A"
        try:
            with open('/proc/meminfo', 'r') as f: lines = f.readlines()
            t = f = b = c = 0
            for line in lines:
                if 'MemTotal' in line: t = int(line.split()[1])
                elif 'MemFree' in line: f = int(line.split()[1])
                elif 'Buffers' in line: b = int(line.split()[1])
                elif 'Cached' in line: c = int(line.split()[1])
            if t > 0: ram_usage = f"{((t - f - b - c) / t) * 100:.1f}%"
        except: pass

        try:
            with open('/proc/stat', 'r') as f: parts = [int(x) for x in f.readline().split()[1:]]
            idle, total = parts[3], sum(parts)
            d_idle, d_total = idle - self._last_cpu_idle, total - self._last_cpu_total
            self._last_cpu_idle, self._last_cpu_total = idle, total
            if d_total > 0: cpu_usage = f"{(100.0 * (1.0 - d_idle / d_total)):.1f}%"
        except: pass
        return cpu_usage, ram_usage

    def get_screen_size(self) -> Tuple[int, int]:
        ok, out = self.device.exec_cmd("wm size")
        w, h = 720, 1280
        if ok and out:
            match = re.search(r"(\d+)x(\d+)", out)
            if match:
                w, h = int(match.group(1)), int(match.group(2))
        return w, h

    def live_dashboard_thread(self):
        """High-Performance Anti-Flicker Dashboard"""
        os.system("cls" if os.name == "nt" else "clear") # Initial clear
        while self.ui_running:
            cpu, ram = self.get_system_stats()
            
            # Kỹ thuật di chuyển trỏ chuột về Home (0,0) chống nháy nhòe màn hình
            sys.stdout.write("\033[H")
            
            # Render Buffer
            buffer = []
            buffer.append(f"\n{' '*20}{Colors.CYAN}CPU: {cpu} | RAM: {ram}{Colors.RESET}       \n")
            
            w_pkg, w_usr, w_stt = 22, 16, 32
            buffer.append(f"┌{'─' * w_pkg}┬{'─' * w_usr}┬{'─' * w_stt}┐")
            buffer.append(f"│ {Colors.MAGENTA}{'Package':<{w_pkg-1}}{Colors.RESET}│ {Colors.MAGENTA}{'Username':<{w_usr-1}}{Colors.RESET}│ {Colors.MAGENTA}{'Status':<{w_stt-1}}{Colors.RESET}│")
            buffer.append(f"├{'─' * w_pkg}┼{'─' * w_usr}┼{'─' * w_stt}┤")
            
            with self._lock:
                for pkg in self.config.get("packages", []):
                    usr = self.username_map.get(pkg, "Unknown")
                    stt = self.status_map.get(pkg, "Waiting...")
                    
                    # Cắt chuỗi an toàn nếu quá dài
                    stt_disp = stt[:w_stt-2] + ".." if len(stt) > w_stt-1 else stt
                    col_stt = Colors.RED if "Lỗi" in stt or "Failed" in stt else Colors.GREEN
                    buffer.append(f"│ {Colors.CYAN}{pkg:<{w_pkg-1}}{Colors.RESET}│ {Colors.GREEN}{usr:<{w_usr-1}}{Colors.RESET}│ {col_stt}{stt_disp:<{w_stt-1}}{Colors.RESET}│")
                
                cur_global = self.global_status

            buffer.append(f"└{'─' * w_pkg}┴{'─' * w_usr}┴{'─' * w_stt}┘")
            # In đè dài ra để lấp chữ cũ
            buffer.append(f"\n{Colors.YELLOW}[*] {cur_global:<60}{Colors.RESET}")
            buffer.append(f"{Colors.MAGENTA}Bấm Ctrl+C để dừng an toàn...{' '*20}{Colors.RESET}\n")
            
            sys.stdout.write("\n".join(buffer))
            sys.stdout.flush()
            time.sleep(1)

    def run_rejoin_sequence(self, with_bypass: bool = False) -> None:
        pkgs = self.config.get("packages", [])
        if not pkgs:
            print(f"{Colors.RED}[!] Chưa có Package. Vui lòng Setup mục 3 trước.{Colors.RESET}")
            time.sleep(1.5)
            return

        print(f"\n{Colors.CYAN}--- Cài đặt tiến trình (Time Interval) ---{Colors.RESET}")
        try:
            raw_time = input(f"{Colors.MAGENTA}Thời gian lặp lại (phút) [Nhập 0 để treo một lần]: {Colors.RESET}").strip()
            interval_seconds = int(float(raw_time) * 60) if raw_time else 0
        except ValueError:
            interval_seconds = 0

        self.set_global_status("Khởi tạo bộ dữ liệu Username...")
        with self._lock:
            self.status_map = {p: "Preparing..." for p in pkgs}
            self.username_map = {}
            for p in pkgs:
                cookie = self.config.get("cookies", {}).get(p)
                self.username_map[p] = self.fetch_username(cookie) if cookie else "Unknown"

        self.ui_running = True
        ui_thread = threading.Thread(target=self.live_dashboard_thread, daemon=True)
        ui_thread.start()

        try:
            while True:
                # --- ĐỢT 1: WARM-UP (KHỞI TẠO SẢNH) ---
                self.set_global_status("ĐỢT 1: Đóng và Khởi tạo sảnh game...")
                for pkg in pkgs:
                    self.set_status(pkg, "Đợt 1: Xóa nền & Mở sảnh...")
                    self.device.open_lobby(pkg)
                    time.sleep(3.5) # Độ trễ chuẩn để Android kịp render GUI
                    self.set_status(pkg, "Đợt 1: Ready ở nền")

                # Tính toán Kích thước (Dynamic Grid Engine)
                auto_rs = self.config.get("auto_resize", False)
                auto_ar = self.config.get("auto_arrange", False)
                freeform_enabled = auto_rs or auto_ar
                grid_bounds = []
                
                if auto_ar:
                    w, h = self.get_screen_size()
                    n = len(pkgs)
                    cols = math.ceil(math.sqrt(n))
                    rows = math.ceil(n / cols)
                    sw, sh = w // cols, h // rows
                    for i in range(n):
                        left, top = (i % cols) * sw, (i // cols) * sh
                        grid_bounds.append(f"{left},{top},{left+sw},{top+sh}")

                # --- ĐỢT 2: DIRECT JOIN (TIÊM LINK TRỰC TIẾP, KHÔNG ĐÓNG APP) ---
                self.set_global_status("ĐỢT 2: Gửi lệnh Join Game...")
                for i, pkg in enumerate(pkgs):
                    self.set_status(pkg, "Đợt 2: Injecting DeepLink...")
                    link = self.config.get("server_links", {}).get(pkg, "")
                    place_id, job_id, link_code, share_code = self.parse_place_info(link)
                    
                    if not place_id:
                        self.set_status(pkg, "Lỗi: Link Setup Sai")
                        continue

                    if with_bypass: self.device.set_android_id(os.urandom(8).hex())
                    
                    b_str = grid_bounds[i] if auto_ar else ("0,0,500,700" if auto_rs else None)
                    
                    # Nhận diện lệnh thật - Xử lý Retry
                    success = self.device.launch_place(pkg, place_id, job_id, link_code, share_code, freeform_enabled, b_str)
                    
                    if success:
                        self.set_status(pkg, "Joined Success")
                    else:
                        self.set_status(pkg, "Retry Launching...")
                        time.sleep(1.5)
                        retry_ok = self.device.launch_place(pkg, place_id, job_id, link_code, share_code, freeform=False)
                        self.set_status(pkg, "Joined (Fallback Mode)" if retry_ok else "Lỗi Mở Game")
                    
                    time.sleep(4) # Chờ Server Roblox nhận diện Token

                # --- ĐẾM NGƯỢC ---
                if interval_seconds <= 0:
                    self.set_global_status("✓ Hoàn tất. Hệ thống đang treo giữ các Game...")
                    while True: time.sleep(1)
                else:
                    for remaining in range(interval_seconds, 0, -1):
                        mins, secs = divmod(remaining, 60)
                        self.set_global_status(f"Chu kỳ lặp tiếp theo diễn ra sau: {mins}p {secs}s")
                        time.sleep(1)

        except KeyboardInterrupt:
            # Xử lý Graceful Shutdown
            pass
        finally:
            self.ui_running = False
            os.system("cls" if os.name == "nt" else "clear")
            print(f"{Colors.GREEN}[+] Đã thoát tác vụ an toàn.{Colors.RESET}")
            time.sleep(1)

    def filter_and_select_packages(self) -> None:
        print(f"\n{Colors.CYAN}[*] Bắt đầu rà quét hệ thống...{Colors.RESET}")
        all_pkgs = self.device.get_all_packages()
        if not all_pkgs: return
        keyword = input(f"\n{Colors.MAGENTA}Nhập từ khóa App (vd: free, roblox): {Colors.RESET}").strip().lower()
        if not keyword: return
        matched = [p for p in all_pkgs if keyword in p.lower()]
        if not matched: return
        
        for i, p in enumerate(matched, start=1): print(f"  {i}. {p}")
        link = input(f"\n{Colors.MAGENTA}Nhập Link Server (Hỗ trợ Share/VIP/PlaceID): {Colors.RESET}").strip()
        self.config["packages"] = matched
        for p in matched: self.config.setdefault("server_links", {})[p] = link
        self._save_config()
        print(f"{Colors.GREEN}[+] Đã lưu cấu hình.{Colors.RESET}")
        time.sleep(1.5)

    def auto_assign_all(self) -> None:
        link = input(f"\n{Colors.MAGENTA}Nhập 1 Link áp dụng cho toàn bộ Roblox trên máy: {Colors.RESET}").strip()
        all_pkgs = self.device.get_all_packages()
        matched = [p for p in all_pkgs if "roblox" in p.lower() or "clone" in p.lower()]
        if not matched: return
        self.config["packages"] = matched
        for p in matched: self.config.setdefault("server_links", {})[p] = link
        self._save_config()
        print(f"{Colors.GREEN}[+] Đã gán cho {len(matched)} ứng dụng.{Colors.RESET}")
        time.sleep(1.5)

    def login_via_cookie_menu(self) -> None:
        cookies = self._read_cookie_file()
        if not cookies:
            print(f"{Colors.RED}[!] Lỗi: file cookie.txt trống.{Colors.RESET}")
            time.sleep(1.5)
            return
            
        choice = input(f"\n{Colors.CYAN}[1] Gán tất cả tự động\n[2] Chọn app thủ công\n{Colors.MAGENTA}Chọn (1/2): {Colors.RESET}").strip()
        pkgs = self.config.get("packages", [])
        
        if choice == "1":
            for idx in range(min(len(pkgs), len(cookies))):
                self.device.inject_cookie_to_pkg(pkgs[idx], cookies[idx])
                self.config.setdefault("cookies", {})[pkgs[idx]] = cookies[idx]
        elif choice == "2":
            selected = []
            c_idx = 0
            while c_idx < len(cookies):
                for i, p in enumerate(pkgs, start=1):
                    print(f" [{i}] {p} " + (f"{Colors.GREEN}(Đã chọn){Colors.RESET}" if p in selected else ""))
                pick = input(f"{Colors.YELLOW}[0] Lưu lại\n{Colors.MAGENTA}Chọn app: {Colors.RESET}").strip()
                if pick == "0": break
                if pick.isdigit() and 1 <= int(pick) <= len(pkgs):
                    if pkgs[int(pick)-1] not in selected:
                        selected.append(pkgs[int(pick)-1])
                        c_idx += 1
            for i, p in enumerate(selected):
                self.device.inject_cookie_to_pkg(p, cookies[i])
                self.config.setdefault("cookies", {})[p] = cookies[i]
        self._save_config()
        print(f"{Colors.GREEN}[+] Hoàn tất Login Cookie.{Colors.RESET}")
        time.sleep(1)

    def handle_config_menu(self) -> None:
        while True:
            os.system("cls" if os.name == "nt" else "clear")
            c_rs = self.config.get("auto_resize", False)
            c_ar = self.config.get("auto_arrange", False)
            
            print(f"\n{Colors.CYAN}--- Cấu hình Nâng cao (Windowing) ---{Colors.RESET}")
            print(f" [1] Bật cửa sổ nhỏ (Freeform) : {Colors.GREEN if c_rs else Colors.RED}{c_rs}{Colors.RESET}")
            print(f" [2] Bật tự chia đều Màn hình : {Colors.GREEN if c_ar else Colors.RED}{c_ar}{Colors.RESET}")
            print(f" [0] Lưu và Quay lại")
            
            pick = input(f"\n{Colors.MAGENTA}Chọn thao tác: {Colors.RESET}").strip()
            if pick == "1": self.config["auto_resize"] = not c_rs
            elif pick == "2": self.config["auto_arrange"] = not c_ar
            elif pick == "0": break
            self._save_config()

class TUIApp:
    def __init__(self) -> None:
        self.engine = RobloxRejoinEngine()
        self.width = 66

    def render(self) -> None:
        os.system("cls" if os.name == "nt" else "clear")
        print(f"{' '*18}⚡ {Colors.CYAN}{Colors.BOLD}SieuVipPro Dashboard{Colors.RESET}\n")

        menu_data = [
            ("Automation Engine", [(1, "Start Auto Rejoin"), (2, "Start Auto Rejoin (Bypass ID)")]),
            ("Game & Packages", [(3, "Select Packages & Set Link"), (4, "List Current Packages"), (5, "Auto-Select ALL Roblox Apps")]),
            ("Account Management", [(7, "Login via Cookie"), (8, "Clear App Data (Logout)"), (9, "Sync Login Cookies")]),
            ("System Tools", [(11, "Set Random Android ID"), (12, "Download Target APK")]),
            ("", [(13, "Advanced Window Config"), (0, "Exit System")])
        ]

        w = self.width
        print(f"┌{'─' * 6}┬{'─' * (w - 7)}┐")
        for idx, (section, items) in enumerate(menu_data):
            if section: print(f"│{' '*6}│ {Colors.BLUE}── {section} ──{Colors.RESET}{' ' * (w - 11 - len(section))}│")
            for num, label in items:
                col = Colors.RED if num == 0 else (Colors.GREEN if num == 13 else Colors.CYAN)
                print(f"│ {Colors.MAGENTA}{num:>4}{Colors.RESET} │ {col}{label:<{w - 10}}{Colors.RESET}│")
            if idx < len(menu_data) - 1: print(f"├{'─' * 6}┼{'─' * (w - 7)}┤")
        print(f"└{'─' * 6}┴{'─' * (w - 7)}┘")

    def run(self) -> None:
        actions = {
            1: lambda: self.engine.run_rejoin_sequence(with_bypass=False),
            2: lambda: self.engine.run_rejoin_sequence(with_bypass=True),
            3: self.engine.filter_and_select_packages,
            5: self.engine.auto_assign_all,
            7: self.engine.login_via_cookie_menu,
            13: self.engine.handle_config_menu,
            0: lambda: sys.exit(0)
        }
        while True:
            self.render()
            try:
                raw = input(f"\n{Colors.MAGENTA}Execute -> {Colors.RESET}").strip()
                if raw and raw.isdigit() and int(raw) in actions: 
                    actions[int(raw)]()
            except (ValueError, KeyboardInterrupt, EOFError):
                sys.exit(0)

if __name__ == "__main__":
    TUIApp().run()
