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
            script_path = "/sdcard/Download/sv_cmd.sh"
            with open(script_path, "w", encoding="utf-8") as f:
                f.write("#!/system/bin/sh\n")
                f.write("export PATH=/sbin:/system/sbin:/system/bin:/system/xbin:/data/data/com.termux/files/usr/bin:$PATH\n")
                f.write(command + "\n")
            
            res = subprocess.run(["sh", script_path], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=15)
            output = (res.stdout + "\n" + res.stderr).strip()
            
            if res.returncode != 0 and "Permission denied" in output:
                res = subprocess.run(["su", "-c", f"sh {script_path}"], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=15)
                output = (res.stdout + "\n" + res.stderr).strip()
                
            return (res.returncode == 0), output
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
    def open_lobby(cls, pkg: str) -> None:
        """ĐỢT 1: Đóng app và Mở sảnh (Warm-up)"""
        cls.kill_package(pkg)
        time.sleep(1.5)
        ok, out = cls.exec_cmd(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1")
        if not ok or "Error" in out or "Exception" in out or "aborted" in out.lower():
            cls.exec_cmd(f"am start -p {pkg}")

    @classmethod
    def launch_place(cls, pkg: str, place_id: str, job_id: Optional[str] = None, link_code: Optional[str] = None, share_code: Optional[str] = None, freeform: bool = False, bounds: Optional[str] = None) -> bool:
        """ĐỢT 2: SỬA LỖI - Không Force Stop lại nữa, chỉ bắn Deep Link vào app đã mở sẵn ở nền."""
        
        # Tạo URL Intent an toàn
        intent_url = f"roblox://experiences/start?placeId={place_id}"
        if job_id: intent_url += f"&gameInstanceId={job_id}"
        elif link_code: intent_url += f"&linkCode={link_code}"
        elif share_code: intent_url += f"&code={share_code}"
            
        cmd_base = f"-a android.intent.action.VIEW -d '{intent_url}'"
        cmd_primary = f"am start -p {pkg} {cmd_base}"
        cmd_fallback = f"am start -n {pkg}/com.roblox.client.ActivityProtocolLaunch {cmd_base}"

        if freeform:
            cls.exec_cmd("settings put global enable_freeform_support 1")
            cls.exec_cmd("settings put global force_resizable_activities 1")
            ff_flags = "--windowingMode 5"
            if bounds: ff_flags += f" --bounds {bounds}"
            
            ok, out = cls.exec_cmd(f"am start {ff_flags} -p {pkg} {cmd_base}")
            if not ok or "Error" in out or "Exception" in out:
                ok2, out2 = cls.exec_cmd(cmd_primary)
                if not ok2 or "Error" in out2 or "Exception" in out2:
                    return cls.exec_cmd(cmd_fallback)[0]
                return True
            return True
        else:
            ok, out = cls.exec_cmd(cmd_primary)
            if not ok or "Error" in out or "Exception" in out:
                return cls.exec_cmd(cmd_fallback)[0]
            return True

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

    def parse_place_info(self, raw_input: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        """SỬA LỖI: Thêm Regex bắt Share Code dạng mới"""
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

    def get_system_stats(self) -> Tuple[str, str]:
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
        w, h = 720, 1280
        if ok and out:
            for line in out.splitlines():
                if "size:" in line:
                    try:
                        parts = line.split(":")[-1].strip().lower().split("x")
                        w, h = int(parts[0]), int(parts[1])
                    except: pass
        return w, h

    def live_dashboard_thread(self):
        while self.ui_running:
            cpu, ram = self.get_system_stats()
            os.system("cls" if os.name == "nt" else "clear")
            print(f"\n{' '*20}{Colors.CYAN}CPU: {cpu} | RAM: {ram}{Colors.RESET}\n")
            
            w_pkg, w_usr, w_stt = 22, 16, 32 # Tăng size cột Status để ghi rõ lỗi
            
            print(f"┌{'─' * w_pkg}┬{'─' * w_usr}┬{'─' * w_stt}┐")
            print(f"│ {Colors.MAGENTA}{'Package':<{w_pkg-1}}{Colors.RESET}│ {Colors.MAGENTA}{'Username':<{w_usr-1}}{Colors.RESET}│ {Colors.MAGENTA}{'Status':<{w_stt-1}}{Colors.RESET}│")
            print(f"├{'─' * w_pkg}┼{'─' * w_usr}┼{'─' * w_stt}┤")
            
            for pkg in self.config.get("packages", []):
                usr = self.username_map.get(pkg, "Unknown")
                stt = self.status_map.get(pkg, "Waiting...")
                
                # Sửa màu tuỳ trạng thái báo lỗi cho trực quan
                col_stt = Colors.RED if "Lỗi" in stt or "Failed" in stt else Colors.GREEN
                print(f"│ {Colors.CYAN}{pkg:<{w_pkg-1}}{Colors.RESET}│ {Colors.GREEN}{usr:<{w_usr-1}}{Colors.RESET}│ {col_stt}{stt:<{w_stt-1}}{Colors.RESET}│")
                
            print(f"└{'─' * w_pkg}┴{'─' * w_usr}┴{'─' * w_stt}┘")
            print(f"\n{Colors.YELLOW}[*] {self.global_status}{Colors.RESET}")
            print(f"{Colors.MAGENTA}Bấm Ctrl+C để dừng và quay lại menu chính...{Colors.RESET}")
            time.sleep(1)

    def run_rejoin_sequence(self, with_bypass: bool = False) -> None:
        pkgs = self.config.get("packages", [])
        if not pkgs:
            print(f"{Colors.RED}[!] Chưa có app. Hãy thiết lập mục 3 trước.{Colors.RESET}")
            time.sleep(1.5)
            return

        print(f"\n{Colors.CYAN}--- Cài đặt thời gian chạy ---{Colors.RESET}")
        try:
            raw_time = input(f"{Colors.MAGENTA}Nhập thời gian lặp lại (phút) [Nhập 0 để KHÔNG đóng tab]: {Colors.RESET}").strip()
            interval_seconds = int(float(raw_time) * 60) if raw_time else 0
        except ValueError:
            interval_seconds = 0

        print(f"{Colors.YELLOW}[*] Đang khởi tạo luồng & trích xuất Username...{Colors.RESET}")
        
        self.status_map = {p: "Chuẩn bị..." for p in pkgs}
        self.username_map = {}
        for p in pkgs:
            cookie = self.config.get("cookies", {}).get(p)
            self.username_map[p] = self.fetch_username(cookie) if cookie else "Unknown"

        self.ui_running = True
        ui_thread = threading.Thread(target=self.live_dashboard_thread, daemon=True)
        ui_thread.start()

        try:
            while True:
                # --- ĐỢT 1: ĐÓNG VÀ MỞ SẢNH (Warm-up) ---
                self.global_status = "Đang chạy Đợt 1: Khởi động sảnh..."
                for pkg in pkgs:
                    self.status_map[pkg] = "Đợt 1: Khởi động sảnh..."
                    self.device.open_lobby(pkg)
                    time.sleep(4)
                    self.status_map[pkg] = "Đợt 1: Sẵn sàng (Nền)"

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

                # --- ĐỢT 2: BẮN DEEP LINK VÀO APP ĐÃ WARM-UP ---
                self.global_status = "Đang chạy Đợt 2: Join Game & Áp dụng Config..."
                for i, pkg in enumerate(pkgs):
                    self.status_map[pkg] = "Đợt 2: Đang bắn lệnh Join..."
                    link = self.config.get("server_links", {}).get(pkg, "")
                    place_id, job_id, link_code, share_code = self.parse_place_info(link)
                    
                    if not place_id:
                        self.status_map[pkg] = "Lỗi: Link/ID trống"
                        continue

                    if with_bypass: self.device.set_android_id(os.urandom(8).hex())
                    
                    b_str = grid_bounds[i] if auto_ar else ("0,0,500,700" if auto_rs else None)
                    
                    # SỬA LỖI: Kiểm tra kết quả thực tế từ lệnh am start
                    success = self.device.launch_place(pkg, place_id, job_id, link_code, share_code, freeform=freeform_enabled, bounds=b_str)
                    
                    if success:
                        self.status_map[pkg] = "Joined"
                    else:
                        # Fallback Retry 1 lần nếu lệnh bị từ chối
                        self.status_map[pkg] = "Retry Launching..."
                        time.sleep(2)
                        retry = self.device.launch_place(pkg, place_id, job_id, link_code, share_code, freeform=False) # Tắt thử size
                        self.status_map[pkg] = "Joined" if retry else "Lỗi Mở Game"
                    
                    time.sleep(4) # Chờ game load hòm hòm rồi mới mở acc tiếp theo

                # --- XỬ LÝ ĐẾM LÙI ---
                if interval_seconds <= 0:
                    self.global_status = "Hoàn tất! Các tab đang được giữ nguyên ở nền."
                    while True: time.sleep(1)
                else:
                    for remaining in range(interval_seconds, 0, -1):
                        mins = remaining // 60
                        secs = remaining % 60
                        self.global_status = f"Chu kỳ khởi động lại tiếp theo sau: {mins} phút {secs} giây"
                        time.sleep(1)

        except KeyboardInterrupt:
            pass
        finally:
            self.ui_running = False
            ui_thread.join(timeout=2)

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

    def auto_assign_all(self) -> None:
        link = input(f"\n{Colors.MAGENTA}Nhập 1 Link áp dụng cho tất cả Roblox packages: {Colors.RESET}").strip()
        all_pkgs = self.device.get_all_packages()
        matched = [p for p in all_pkgs if "roblox" in p.lower() or "clone" in p.lower()]
        if not matched: return
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
            ("Server Setup", [(3, "Select packages & assign server link"), (4, "List selected packages"), (5, "Auto-select all Roblox packages")]),
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
            5: self.engine.auto_assign_all,
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
