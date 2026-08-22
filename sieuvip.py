import os
import sys
import time
import subprocess
import json
import re
import threading
import math
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
    @staticmethod
    def exec_cmd(command: str) -> Tuple[bool, str]:
        try:
            res = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=15)
            if res.returncode == 0:
                return True, res.stdout.strip()
            return False, res.stderr.strip()
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

        ok, data_out = cls.exec_cmd("ls -1 /data/data/")
        if ok and data_out:
            for item in data_out.splitlines():
                if "." in item.strip(): packages.add(item.strip())
        return sorted(list(packages))

    @classmethod
    def kill_package(cls, pkg: str) -> None:
        cls.exec_cmd(f"am force-stop {pkg}")

    @classmethod
    def launch_place(cls, pkg: str, place_id: str, job_id: Optional[str] = None, link_code: Optional[str] = None, freeform: bool = False, bounds: Optional[str] = None) -> None:
        cls.kill_package(pkg)
        time.sleep(1)
        
        intent_url = f"roblox://experiences/start?placeId={place_id}"
        if job_id: intent_url += f"&gameInstanceId={job_id}"
        elif link_code: intent_url += f"&linkCode={link_code}"
            
        cmd_primary = f"am start"
        if freeform:
            cls.exec_cmd("settings put global enable_freeform_support 1")
            cmd_primary += " --windowingMode 5"
        if bounds:
            cmd_primary += f" --bounds {bounds}"
            
        cmd_primary += f" -n {pkg}/com.roblox.client.ActivityProtocolLaunch -a android.intent.action.VIEW -d '{intent_url}'"
        
        ok, out = cls.exec_cmd(cmd_primary)
        if not ok or "Error" in out or "Exception" in out:
            cmd_fallback = f"am start -p {pkg} -a android.intent.action.VIEW -d '{intent_url}'"
            cls.exec_cmd(cmd_fallback)

    @classmethod
    def set_android_id(cls, new_id: str) -> bool:
        ok, _ = cls.exec_cmd(f"settings put secure android_id {new_id}")
        return ok

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
        ok, _ = cls.exec_cmd(cmd)
        return ok

class RobloxRejoinEngine:
    CONFIG_FILE = "/sdcard/Download/sieuvip_config.json"
    COOKIE_FILE = "/sdcard/Download/cookie.txt"

    def __init__(self) -> None:
        self.config: Dict = self._load_config()
        self.device = DeviceController()
        
        # State variables for Live TUI Thread
        self.status_map: Dict[str, str] = {}
        self.username_map: Dict[str, str] = {}
        self.ui_running = False
        
        self._last_cpu_idle = 0
        self._last_cpu_total = 0

    def _load_config(self) -> Dict:
        default_cfg = {
            "check_ui_time": 180,
            "auto_block": True,
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
            except Exception:
                pass
        return default_cfg

    def _save_config(self) -> None:
        with open(self.CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=4)

    def _read_cookie_file(self) -> List[str]:
        if not os.path.exists(self.COOKIE_FILE): return []
        try:
            with open(self.COOKIE_FILE, "r", encoding="utf-8") as f:
                return [line.strip() for line in f.readlines() if line.strip() and not line.startswith("#")]
        except Exception:
            return []

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

    def parse_place_info(self, raw_input: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        place_match = re.search(r"games/(\d+)", raw_input)
        place_id = place_match.group(1) if place_match else None
        if not place_id and raw_input.isdigit(): place_id = raw_input

        job_match = re.search(r"gameInstanceId=([a-f0-9\-]+)", raw_input)
        job_id = job_match.group(1) if job_match else None
        
        link_code_match = re.search(r"privateServerLinkCode=([a-zA-Z0-9\-_]+)", raw_input)
        link_code = link_code_match.group(1) if link_code_match else None

        return place_id, job_id, link_code

    def get_system_stats(self) -> Tuple[str, str]:
        # RAM
        ram_usage = "N/A"
        try:
            with open('/proc/meminfo', 'r') as f:
                lines = f.readlines()
            t = f = b = c = 0
            for line in lines:
                if 'MemTotal' in line: t = int(line.split()[1])
                elif 'MemFree' in line: f = int(line.split()[1])
                elif 'Buffers' in line: b = int(line.split()[1])
                elif 'Cached' in line: c = int(line.split()[1])
            if t > 0:
                ram_usage = f"{((t - f - b - c) / t) * 100:.1f}%"
        except: pass

        # CPU
        cpu_usage = "N/A"
        try:
            with open('/proc/stat', 'r') as f:
                line = f.readline()
            parts = [int(x) for x in line.split()[1:]]
            idle = parts[3]
            total = sum(parts)
            d_idle = idle - self._last_cpu_idle
            d_total = total - self._last_cpu_total
            self._last_cpu_idle, self._last_cpu_total = idle, total
            if d_total > 0:
                cpu_usage = f"{(100.0 * (1.0 - d_idle / d_total)):.1f}%"
        except: pass
        
        return cpu_usage, ram_usage

    def get_screen_size(self) -> Tuple[int, int]:
        ok, out = self.device.exec_cmd("wm size")
        if ok and out:
            nums = re.findall(r'\d+', out.splitlines()[-1])
            if len(nums) >= 2:
                return int(nums[-2]), int(nums[-1])
        return 1080, 1920

    def live_dashboard_thread(self):
        while self.ui_running:
            cpu, ram = self.get_system_stats()
            os.system("cls" if os.name == "nt" else "clear")
            print(f"\n{' '*20}{Colors.CYAN}CPU: {cpu} | RAM: {ram}{Colors.RESET}\n")
            
            w_pkg, w_usr, w_stt = 22, 16, 30
            
            # Header
            print(f"┌{'─' * w_pkg}┬{'─' * w_usr}┬{'─' * w_stt}┐")
            print(f"│ {Colors.MAGENTA}{'Package':<{w_pkg-1}}{Colors.RESET}│ {Colors.MAGENTA}{'Username':<{w_usr-1}}{Colors.RESET}│ {Colors.MAGENTA}{'Status':<{w_stt-1}}{Colors.RESET}│")
            print(f"├{'─' * w_pkg}┼{'─' * w_usr}┼{'─' * w_stt}┤")
            
            # Rows
            for pkg in self.config.get("packages", []):
                usr = self.username_map.get(pkg, "Unknown")
                stt = self.status_map.get(pkg, "Waiting...")
                print(f"│ {Colors.CYAN}{pkg:<{w_pkg-1}}{Colors.RESET}│ {Colors.GREEN}{usr:<{w_usr-1}}{Colors.RESET}│ {Colors.GREEN}{stt:<{w_stt-1}}{Colors.RESET}│")
                
            print(f"└{'─' * w_pkg}┴{'─' * w_usr}┴{'─' * w_stt}┘")
            time.sleep(1)

    def run_rejoin_sequence(self, with_bypass: bool = False) -> None:
        pkgs = self.config.get("packages", [])
        if not pkgs:
            print(f"{Colors.RED}[!] Chưa có app. Hãy thiết lập mục 3 trước.{Colors.RESET}")
            time.sleep(1.5)
            return

        print(f"{Colors.YELLOW}[*] Đang khởi tạo luồng & trích xuất Username...{Colors.RESET}")
        
        # Prepare state map
        self.status_map = {p: "Đang nạp..." for p in pkgs}
        self.username_map = {}
        for p in pkgs:
            cookie = self.config.get("cookies", {}).get(p)
            self.username_map[p] = self.fetch_username(cookie) if cookie else "Unknown"

        # Khởi chạy Thread UI
        self.ui_running = True
        ui_thread = threading.Thread(target=self.live_dashboard_thread, daemon=True)
        ui_thread.start()

        # --- ĐỢT 1: Đóng và Mở App (Không Join) ---
        for pkg in pkgs:
            self.status_map[pkg] = "Đợt 1: Đóng app..."
            self.device.kill_package(pkg)
            time.sleep(1)
            self.status_map[pkg] = "Đợt 1: Mở sảnh..."
            self.device.exec_cmd(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1 > /dev/null 2>&1")
            time.sleep(2)

        # Cấu hình tính toán Bounds cho Đợt 2
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

        # --- ĐỢT 2: Đóng và Join Game (Kèm Config 13) ---
        for i, pkg in enumerate(pkgs):
            self.status_map[pkg] = "Đợt 2: Force Stop..."
            link = self.config.get("server_links", {}).get(pkg, "")
            place_id, job_id, link_code = self.parse_place_info(link)
            
            if not place_id:
                self.status_map[pkg] = "Lỗi: Không có Place ID"
                continue

            if with_bypass: self.device.set_android_id(os.urandom(8).hex())
            
            self.status_map[pkg] = "Đợt 2: Đang Join..."
            
            b_str = grid_bounds[i] if auto_ar else ("0,0,800,600" if auto_rs else None)
            
            self.device.launch_place(pkg, place_id, job_id, link_code, freeform=freeform_enabled, bounds=b_str)
            time.sleep(2.5)
            self.status_map[pkg] = "Joined"

        time.sleep(2) # Hold UI for 2 seconds to view results
        self.ui_running = False
        ui_thread.join()
        
        print(f"\n{Colors.GREEN}[+] Hoàn tất toàn bộ chu trình Rejoin!{Colors.RESET}")
        input(f"{Colors.MAGENTA}Bấm Enter để quay lại menu chính...{Colors.RESET}")

    def filter_and_select_packages(self) -> None:
        print(f"\n{Colors.CYAN}[*] Đang quét hệ thống...{Colors.RESET}")
        all_pkgs = self.device.get_all_packages()
        if not all_pkgs: return
        keyword = input(f"\n{Colors.MAGENTA}Nhập từ khóa package: {Colors.RESET}").strip().lower()
        if not keyword: return
        matched = [p for p in all_pkgs if keyword in p.lower()]
        if not matched: return
        
        for i, p in enumerate(matched, start=1): print(f"  {i}. {p}")
        link = input(f"\n{Colors.MAGENTA}Nhập Server Link / Place ID: {Colors.RESET}").strip()
        self.config["packages"] = matched
        for p in matched: self.config.setdefault("server_links", {})[p] = link
        self._save_config()
        time.sleep(1)

    def login_via_cookie_menu(self) -> None:
        cookies = self._read_cookie_file()
        if not cookies: return
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

    def handle_config_menu(self) -> None:
        while True:
            os.system("cls" if os.name == "nt" else "clear")
            c_rs = self.config.get("auto_resize", False)
            c_ar = self.config.get("auto_arrange", False)
            
            print(f"\n{Colors.CYAN}--- Configuration Menu ---{Colors.RESET}")
            print(f" [1] Auto làm nhỏ tab lại (Freeform) : {Colors.GREEN if c_rs else Colors.RED}{c_rs}{Colors.RESET}")
            print(f" [2] Auto sắp xếp các tab cho đều  : {Colors.GREEN if c_ar else Colors.RED}{c_ar}{Colors.RESET}")
            print(f" [0] Quay lại")
            
            pick = input(f"\n{Colors.MAGENTA}Chọn config (0-2): {Colors.RESET}").strip()
            if pick == "1": self.config["auto_resize"] = not c_rs
            elif pick == "2": self.config["auto_arrange"] = not c_ar
            elif pick == "0": break
            self._save_config()

class SieuVipProApp:
    def __init__(self) -> None:
        self.engine = RobloxRejoinEngine()
        self.width = 62

    def render(self) -> None:
        os.system("cls" if os.name == "nt" else "clear")
        print(f"{' '*16}⚡ {Colors.CYAN}{Colors.BOLD}SieuVipPro Menu{Colors.RESET}\n")

        menu_data = [
            ("Auto Rejoin", [(1, "Start auto rejoin"), (2, "Start auto rejoin with bypass")]),
            ("Server Setup", [(3, "Select packages & assign server link"), (4, "List selected packages"), (5, "Auto-select all Roblox packages with one link")]),
            ("Tabs", [(6, "Open all Roblox tabs")]),
            ("Account / Cookie", [(7, "Login via cookie"), (8, "Logout Roblox"), (9, "Fix login cookie"), (10, "Export cookies")]),
            ("System", [(11, "Set Android ID"), (12, "Download APK")]),
            ("", [(13, "Configuration Settings"), (0, "Exit")])
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
            7: self.engine.login_via_cookie_menu,
            13: self.engine.handle_config_menu,
            0: lambda: sys.exit(0)
        }
        while True:
            self.render()
            try:
                raw = input(f"\n{Colors.MAGENTA}Enter choice: {Colors.RESET}").strip()
                if raw and int(raw) in actions: actions[int(raw)]()
            except Exception:
                sys.exit(0)

if __name__ == "__main__":
    SieuVipProApp().run()
