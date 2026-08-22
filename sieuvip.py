import os
import sys
import time
import subprocess
import json
import re
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
    """Điều khiển hệ thống Android OS trực tiếp bằng Root Shell ($O(1)$ dispatch)"""

    @staticmethod
    def exec_cmd(command: str) -> Tuple[bool, str]:
        try:
            # SỬA LỖI: Python đã chạy bằng 'su' từ file cài đặt, chỉ cần gọi shell=True
            res = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=15)
            if res.returncode == 0:
                return True, res.stdout.strip()
            return False, res.stderr.strip()
        except Exception as e:
            return False, str(e)

    @classmethod
    def get_all_packages(cls) -> List[str]:
        packages = set()
        
        # Quét bằng Package Manager của Android
        pm_commands = [
            "pm list packages -u --user all",
            "pm list packages -f",
            "pm list packages"
        ]
        for cmd in pm_commands:
            ok, out = cls.exec_cmd(cmd)
            if ok and out:
                for line in out.splitlines():
                    clean_line = line.strip()
                    if clean_line.startswith("package:"):
                        pkg = clean_line.replace("package:", "").split("=")[-1].strip()
                        if pkg:
                            packages.add(pkg)

        # Quét Fallback trong thư mục Data (Bắt Clones / VPhoneGaGa)
        ok, data_out = cls.exec_cmd("ls -1 /data/data/")
        if ok and data_out:
            for item in data_out.splitlines():
                if "." in item.strip():
                    packages.add(item.strip())

        return sorted(list(packages))

    @classmethod
    def kill_package(cls, pkg: str) -> None:
        cls.exec_cmd(f"am force-stop {pkg}")

    @classmethod
    def launch_place(cls, pkg: str, place_id: str, job_id: Optional[str] = None, link_code: Optional[str] = None) -> None:
        cls.kill_package(pkg)
        time.sleep(1.5)
        
        intent_url = f"roblox://experiences/start?placeId={place_id}"
        if job_id:
            intent_url += f"&gameInstanceId={job_id}"
        elif link_code:
            intent_url += f"&linkCode={link_code}"
            
        # SỬA LỖI: Dùng nháy đơn (') để bọc URL, tránh lỗi shell parsing
        cmd_primary = f"am start -n {pkg}/com.roblox.client.ActivityProtocolLaunch -a android.intent.action.VIEW -d '{intent_url}'"
        ok, out = cls.exec_cmd(cmd_primary)
        
        # Fallback mechanism nếu Component bị đổi tên trong app clone
        if not ok or "Error" in out or "Exception" in out:
            cmd_fallback = f"am start -p {pkg} -a android.intent.action.VIEW -d '{intent_url}'"
            cls.exec_cmd(cmd_fallback)

    @classmethod
    def set_android_id(cls, new_id: str) -> bool:
        cmd = f"settings put secure android_id {new_id}"
        ok, _ = cls.exec_cmd(cmd)
        return ok

    @classmethod
    def inject_cookie_to_pkg(cls, pkg: str, raw_cookie: str) -> bool:
        c = raw_cookie.strip()
        if not c.startswith("_|WARNING:-DO-NOT-SHARE-THIS"):
            c = f"_|WARNING:-DO-NOT-SHARE-THIS.--Sharing-this-will-allow-someone-to-log-into-your-account-and-rob-your-robox.--|_{c}"

        xml_path = f"/data/data/{pkg}/shared_prefs/com.roblox.client_preferences.xml"
        cmd = (
            f"mkdir -p /data/data/{pkg}/shared_prefs && "
            f"if [ ! -f '{xml_path}' ]; then "
            f"echo '<?xml version=\"1.0\" encoding=\"utf-8\" standalone=\"yes\" ?><map><string name=\"RBXSession\">{c}</string></map>' > '{xml_path}'; "
            f"else "
            f"sed -i 's|<string name=\"RBXSession\">.*</string>|<string name=\"RBXSession\">{c}</string>|g' '{xml_path}'; "
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

    def _load_config(self) -> Dict:
        if os.path.exists(self.CONFIG_FILE):
            try:
                with open(self.CONFIG_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "check_ui_time": 180,
            "auto_block": True,
            "packages": [],
            "server_links": {},
            "cookies": {}
        }

    def _save_config(self) -> None:
        with open(self.CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=4)

    def _read_cookie_file(self) -> List[str]:
        if not os.path.exists(self.COOKIE_FILE):
            return []
        try:
            with open(self.COOKIE_FILE, "r", encoding="utf-8") as f:
                return [line.strip() for line in f.readlines() if line.strip() and not line.startswith("#")]
        except Exception:
            return []

    def parse_place_info(self, raw_input: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """SỬA LỖI: Bổ sung Regex bắt Private Server Link Code"""
        place_match = re.search(r"games/(\d+)", raw_input)
        place_id = place_match.group(1) if place_match else None
        
        if not place_id and raw_input.isdigit():
            place_id = raw_input

        job_match = re.search(r"gameInstanceId=([a-f0-9\-]+)", raw_input)
        job_id = job_match.group(1) if job_match else None
        
        link_code_match = re.search(r"privateServerLinkCode=([a-zA-Z0-9\-_]+)", raw_input)
        link_code = link_code_match.group(1) if link_code_match else None

        return place_id, job_id, link_code

    def filter_and_select_packages(self) -> None:
        print(f"\n{Colors.CYAN}[*] Đang quét hệ thống...{Colors.RESET}")
        all_pkgs = self.device.get_all_packages()
        
        if not all_pkgs:
            print(f"{Colors.RED}[!] Lỗi lấy danh sách (Hệ thống từ chối quyền).{Colors.RESET}")
            return

        print(f"{Colors.GREEN}[+] Tìm thấy: {len(all_pkgs)} packages.{Colors.RESET}")

        keyword = input(f"\n{Colors.MAGENTA}Nhập từ khóa package (vd: free, roblox, vphone): {Colors.RESET}").strip().lower()
        if not keyword:
            print(f"{Colors.RED}[!] Từ khóa trống.{Colors.RESET}")
            time.sleep(1.5)
            return

        matched = [p for p in all_pkgs if keyword in p.lower()]

        if not matched:
            print(f"{Colors.RED}[!] Không tìm thấy app nào chứa từ '{keyword}'.{Colors.RESET}")
            manual = input(f"{Colors.YELLOW}Bạn có muốn gõ trực tiếp tên Package không? (y/n): {Colors.RESET}").strip().lower()
            if manual == "y":
                direct_pkg = input(f"{Colors.MAGENTA}Nhập chính xác tên (vd: free.xxx.xxx): {Colors.RESET}").strip()
                if direct_pkg:
                    matched = [direct_pkg]
                else:
                    return
            else:
                return

        print(f"\n{Colors.GREEN}[+] Các Package đã chọn:{Colors.RESET}")
        for i, p in enumerate(matched, start=1):
            print(f"  {i}. {p}")

        link = input(f"\n{Colors.MAGENTA}Nhập Server Link / Place ID (Hỗ trợ VIP Server): {Colors.RESET}").strip()

        self.config["packages"] = matched
        for p in matched:
            self.config.setdefault("server_links", {})[p] = link

        self._save_config()
        print(f"{Colors.GREEN}[+] Đã lưu vào cấu hình.{Colors.RESET}")
        time.sleep(2)

    def run_rejoin_loop(self, with_bypass: bool = False) -> None:
        pkgs = self.config.get("packages", [])
        if not pkgs:
            print(f"{Colors.RED}[!] Chưa có app. Hãy chọn mục 3 trước.{Colors.RESET}")
            time.sleep(2)
            return

        interval = self.config.get("check_ui_time", 180)
        print(f"{Colors.GREEN}[+] Đang chạy Auto Rejoin ({len(pkgs)} apps | {interval}s). Bấm Ctrl+C dừng.{Colors.RESET}")

        try:
            while True:
                for pkg in self.config.get("packages", []):
                    link = self.config.get("server_links", {}).get(pkg, "")
                    place_id, job_id, link_code = self.parse_place_info(link)
                    
                    if not place_id:
                        continue

                    if with_bypass:
                        self.device.set_android_id(os.urandom(8).hex())
                    
                    log_target = job_id or link_code or 'Auto'
                    print(f"{Colors.CYAN}[Rejoin] Khởi chạy -> {pkg} | Map: {place_id} | Job: {log_target}{Colors.RESET}")
                    self.device.launch_place(pkg, place_id, job_id, link_code)

                for remaining in range(interval, 0, -1):
                    sys.stdout.write(f"\r{Colors.YELLOW}[*] Quét lại sau: {remaining}s... {Colors.RESET}")
                    sys.stdout.flush()
                    time.sleep(1)
                print()
        except KeyboardInterrupt:
            print(f"\n{Colors.YELLOW}[!] Đã dừng.{Colors.RESET}")
            time.sleep(1.5)
            
    # Các hàm khác giữ nguyên
    def login_via_cookie_menu(self) -> None:
        cookies = self._read_cookie_file()
        if not cookies:
            print(f"\n{Colors.RED}[!] File cookie.txt rỗng.{Colors.RESET}")
            input(f"{Colors.MAGENTA}Enter để quay lại...{Colors.RESET}")
            return
        print(f"\n{Colors.CYAN}--- Gán Cookie (Có {len(cookies)} cookies) ---{Colors.RESET}")
        print(" [1] Gán tất cả tự động")
        print(" [2] Chọn app thủ công")
        choice = input(f"\n{Colors.MAGENTA}Chọn (1 hoặc 2): {Colors.RESET}").strip()
        if choice == "1":
            pkgs = self.config.get("packages", [])
            limit = min(len(pkgs), len(cookies))
            for idx in range(limit):
                self.device.inject_cookie_to_pkg(pkgs[idx], cookies[idx])
                self.config.setdefault("cookies", {})[pkgs[idx]] = cookies[idx]
                print(f"{Colors.GREEN} [+] Gán Cookie #{idx+1} -> {pkgs[idx]}{Colors.RESET}")
            self._save_config()
            time.sleep(2)
        elif choice == "2":
            pkgs = self.config.get("packages", [])
            selected_mapping = []
            cookie_idx = 0
            while cookie_idx < len(cookies):
                print(f"\n{Colors.CYAN}--- Gán Cookie #{cookie_idx + 1} ---{Colors.RESET}")
                for i, p in enumerate(pkgs, start=1):
                    print(f" [{i}] {p} " + (f"{Colors.GREEN}(Đã chọn){Colors.RESET}" if p in selected_mapping else ""))
                print(f" [0] {Colors.YELLOW}Bắt đầu nạp Cookie{Colors.RESET}")
                pick = input(f"{Colors.MAGENTA}Chọn app: {Colors.RESET}").strip()
                if pick == "0": break
                if pick.isdigit() and 1 <= int(pick) <= len(pkgs):
                    if pkgs[int(pick)-1] not in selected_mapping:
                        selected_mapping.append(pkgs[int(pick)-1])
                        cookie_idx += 1
            for idx, pkg in enumerate(selected_mapping):
                self.device.inject_cookie_to_pkg(pkg, cookies[idx])
                self.config.setdefault("cookies", {})[pkg] = cookies[idx]
                print(f"{Colors.GREEN} [+] Cookie #{idx+1} -> {pkg}{Colors.RESET}")
            self._save_config()
            time.sleep(2)

    def list_packages(self) -> None:
        pkgs = self.config.get("packages", [])
        print(f"\n{Colors.CYAN}--- Apps Đã Chọn ---{Colors.RESET}")
        for i, p in enumerate(pkgs, start=1):
            print(f" {i}. {p}")
        input(f"\n{Colors.MAGENTA}Enter quay lại...{Colors.RESET}")

    def export_cookies(self) -> None:
        pass
    def download_apk(self) -> None:
        pass
    def change_android_id(self) -> None:
        pass

class SieuVipProApp:
    def __init__(self) -> None:
        self.engine = RobloxRejoinEngine()
    def clear(self) -> None:
        os.system("cls" if os.name == "nt" else "clear")
    def render(self) -> None:
        self.clear()
        print(f"{' '*16}⚡ {Colors.CYAN}{Colors.BOLD}SieuVipPro Menu{Colors.RESET}\n")
        menu = [
            "1. Start Rejoin", "2. Start Rejoin Bypass", 
            "3. Select packages & Link", "4. List packages", 
            "7. Login Cookie", "8. Clear Data App", "14. Exit"
        ]
        for m in menu: print(f" {Colors.GREEN}{m}{Colors.RESET}")
    def run(self) -> None:
        actions = {
            1: lambda: self.engine.run_rejoin_loop(),
            2: lambda: self.engine.run_rejoin_loop(True),
            3: self.engine.filter_and_select_packages,
            4: self.engine.list_packages,
            7: self.engine.login_via_cookie_menu,
            8: lambda: [self.engine.device.exec_cmd(f"pm clear {p}") for p in self.engine.config.get("packages", [])],
            14: lambda: sys.exit(0)
        }
        while True:
            self.render()
            try:
                c = int(input(f"\n{Colors.MAGENTA}Enter choice: {Colors.RESET}").strip())
                if c in actions: actions[c]()
            except Exception: pass

if __name__ == "__main__":
    SieuVipProApp().run()
