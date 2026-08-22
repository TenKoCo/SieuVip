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
    """Xử lý tương tác tầng Android OS thông qua root Shell hoặc ADB."""
    
    @staticmethod
    def exec_cmd(command: str) -> Tuple[bool, str]:
        try:
            res = subprocess.run(["su", "-c", command], capture_output=True, text=True, timeout=15)
            if res.returncode == 0:
                return True, res.stdout.strip()
            return False, res.stderr.strip()
        except Exception:
            try:
                res = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=15)
                return (res.returncode == 0), res.stdout.strip() if res.returncode == 0 else res.stderr.strip()
            except Exception as e:
                return False, str(e)

    @classmethod
    def get_installed_roblox_packages(cls) -> List[str]:
        success, out = cls.exec_cmd("pm list packages | grep roblox")
        if not success or not out:
            return ["com.roblox.client"]
        return [line.replace("package:", "").strip() for line in out.splitlines() if line.strip()]

    @classmethod
    def kill_package(cls, pkg: str) -> None:
        cls.exec_cmd(f"am force-stop {pkg}")

    @classmethod
    def launch_place(cls, pkg: str, place_id: str, job_id: Optional[str] = None) -> None:
        cls.kill_package(pkg)
        time.sleep(1)
        
        intent_url = f"roblox://experiences/start?placeId={place_id}"
        if job_id:
            intent_url += f"&gameInstanceId={job_id}"
            
        cmd = f"am start -n {pkg}/com.roblox.client.ActivityProtocolLaunch -a android.intent.action.VIEW -d \"{intent_url}\""
        cls.exec_cmd(cmd)

    @classmethod
    def set_android_id(cls, new_id: str) -> bool:
        cmd = f"settings put secure android_id {new_id}"
        ok, _ = cls.exec_cmd(cmd)
        return ok

class RobloxRejoinEngine:
    CONFIG_FILE = "sieuvip_config.json"

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
            "packages": ["com.roblox.client"],
            "server_links": {},
            "cookies": {}
        }

    def _save_config(self) -> None:
        with open(self.CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=4)

    def parse_place_info(self, raw_input: str) -> Tuple[Optional[str], Optional[str]]:
        place_match = re.search(r"games/(\d+)", raw_input)
        place_id = place_match.group(1) if place_match else None
        
        job_match = re.search(r"gameInstanceId=([a-f0-9\-]+)", raw_input)
        job_id = job_match.group(1) if job_match else None

        if not place_id and raw_input.isdigit():
            place_id = raw_input
            
        return place_id, job_id

    def run_rejoin_loop(self, with_bypass: bool = False) -> None:
        pkgs = self.config.get("packages", [])
        if not pkgs:
            print(f"{Colors.RED}[!] SieuVipPro: Chưa chọn package nào.{Colors.RESET}")
            time.sleep(2)
            return

        interval = self.config.get("check_ui_time", 180)
        print(f"{Colors.GREEN}[+] [SieuVipPro] Đang chạy Auto Rejoin (Chu kỳ: {interval}s, Bypass={with_bypass}). Bấm Ctrl+C để dừng.{Colors.RESET}")

        try:
            while True:
                for pkg in self.config.get("packages", []):
                    link = self.config.get("server_links", {}).get(pkg, "")
                    place_id, job_id = self.parse_place_info(link)
                    
                    if not place_id:
                        print(f"{Colors.YELLOW}[!] Package {pkg} chưa cấu hình Place ID.{Colors.RESET}")
                        continue

                    if with_bypass:
                        fake_id = os.urandom(8).hex()
                        self.device.set_android_id(fake_id)
                    
                    print(f"{Colors.CYAN}[SieuVipPro] Khởi chạy -> {pkg} | Place: {place_id} | Instance: {job_id or 'Auto'}{Colors.RESET}")
                    self.device.launch_place(pkg, place_id, job_id)

                for remaining in range(interval, 0, -1):
                    sys.stdout.write(f"\r{Colors.YELLOW}[SieuVipPro] Quét lại sau: {remaining}s... {Colors.RESET}")
                    sys.stdout.flush()
                    time.sleep(1)
                print()
        except KeyboardInterrupt:
            print(f"\n{Colors.YELLOW}[!] Đã dừng tiến trình SieuVipPro.{Colors.RESET}")
            time.sleep(1.5)

    def assign_server_links(self) -> None:
        pkgs = self.device.get_installed_roblox_packages()
        print(f"\n{Colors.CYAN}[SieuVipPro] Danh sách Roblox packages tìm thấy:{Colors.RESET}")
        for i, p in enumerate(pkgs, start=1):
            print(f" {i}. {p}")
        
        choice = input(f"{Colors.MAGENTA}Chọn số thứ tự package: {Colors.RESET}").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(pkgs):
            selected_pkg = pkgs[int(choice) - 1]
            link = input(f"{Colors.MAGENTA}Nhập Server Link / Place ID: {Colors.RESET}").strip()
            self.config.setdefault("server_links", {})[selected_pkg] = link
            if selected_pkg not in self.config["packages"]:
                self.config["packages"].append(selected_pkg)
            self._save_config()
            print(f"{Colors.GREEN}[+] Đã lưu cấu hình vào SieuVipPro!{Colors.RESET}")
            time.sleep(1)

    def auto_assign_all(self) -> None:
        link = input(f"{Colors.MAGENTA}Nhập 1 Link duy nhất cho TẤT CẢ packages: {Colors.RESET}").strip()
        pkgs = self.device.get_installed_roblox_packages()
        self.config["packages"] = pkgs
        for p in pkgs:
            self.config.setdefault("server_links", {})[p] = link
        self._save_config()
        print(f"{Colors.GREEN}[+] SieuVipPro đã cập nhật toàn bộ {len(pkgs)} packages.{Colors.RESET}")
        time.sleep(1)

    def login_via_cookie(self) -> None:
        cookie = input(f"{Colors.MAGENTA}Nhập .ROBLOSECURITY Cookie: {Colors.RESET}").strip()
        if not cookie:
            return
        
        if not cookie.startswith("_|WARNING:-DO-NOT-SHARE-THIS"):
            cookie = f"_|WARNING:-DO-NOT-SHARE-THIS.--Sharing-this-will-allow-someone-to-log-into-your-account-and-rob-your-robox.--|_{cookie}"
            
        print(f"{Colors.YELLOW}[*] SieuVipPro đang nạp cookie...{Colors.RESET}")
        for pkg in self.config.get("packages", ["com.roblox.client"]):
            xml_path = f"/data/data/{pkg}/shared_prefs/com.roblox.client_preferences.xml"
            inject_cmd = f"su -c \"sed -i 's/<string name=\\\"RBXSession\\\">.*<\\/string>/<string name=\\\"RBXSession\\\">{cookie}<\\/string>/g' {xml_path}\""
            self.device.exec_cmd(inject_cmd)
            self.config.setdefault("cookies", {})[pkg] = cookie

        self._save_config()
        print(f"{Colors.GREEN}[+] Nạp Cookie hoàn tất.{Colors.RESET}")
        time.sleep(1.5)

    def export_cookies(self) -> None:
        print(f"\n{Colors.CYAN}--- SieuVipPro: Danh Sách Cookie ---{Colors.RESET}")
        cookies = self.config.get("cookies", {})
        if not cookies:
            print("Chưa có cookie nào.")
        for pkg, c in cookies.items():
            print(f"[{pkg}]: {c[:30]}... (hidden)")
        input(f"\n{Colors.MAGENTA}Bấm Enter để quay lại...{Colors.RESET}")

    def download_apk(self) -> None:
        url = input(f"{Colors.MAGENTA}Nhập Direct URL APK: {Colors.RESET}").strip()
        if not url:
            return
        print(f"{Colors.CYAN}[*] SieuVipPro đang tải APK...{Colors.RESET}")
        cmd = f"curl -L \"{url}\" -o /sdcard/Download/roblox_update.apk"
        ok, _ = self.device.exec_cmd(cmd)
        if ok:
            print(f"{Colors.GREEN}[+] Tải xong. Đang tiến hành cài đặt...{Colors.RESET}")
            self.device.exec_cmd("pm install -r /sdcard/Download/roblox_update.apk")
        else:
            print(f"{Colors.RED}[!] Tải thất bại.{Colors.RESET}")
        time.sleep(2)

    def change_android_id(self) -> None:
        new_id = input(f"{Colors.MAGENTA}Nhập ID mới (hex 16 ký tự) hoặc bỏ trống để tự tạo: {Colors.RESET}").strip()
        if not new_id:
            new_id = os.urandom(8).hex()
        
        ok = self.device.set_android_id(new_id)
        if ok:
            print(f"{Colors.GREEN}[+] SieuVipPro đã đổi Android ID: {new_id}{Colors.RESET}")
        else:
            print(f"{Colors.RED}[!] Lỗi đổi ID (Cần quyền Root).{Colors.RESET}")
        time.sleep(1.5)

class SieuVipProApp:
    def __init__(self) -> None:
        self.engine = RobloxRejoinEngine()
        self.width = 62

    def clear(self) -> None:
        os.system("cls" if os.name == "nt" else "clear")

    def render(self) -> None:
        self.clear()
        cfg = self.engine.config
        print(f"{Colors.GREEN}Check UI time: {Colors.YELLOW}{cfg.get('check_ui_time', 180)}{Colors.RESET}")
        block_txt = f"{Colors.YELLOW}Enable{Colors.RESET}" if cfg.get("auto_block") else f"{Colors.RED}Disable{Colors.RESET}"
        print(f"{Colors.GREEN}Auto block: {block_txt}")
        print(f"{' '*16}⚡ {Colors.CYAN}{Colors.BOLD}SieuVipPro Menu{Colors.RESET}\n")

        menu_data = [
            ("Auto Rejoin", [(1, "Start auto rejoin"), (2, "Start auto rejoin with bypass")]),
            ("Server Setup", [(3, "Select packages & assign server link"), (4, "List selected packages"), (5, "Auto-select all Roblox packages with one link")]),
            ("Tabs", [(6, "Open all Roblox tabs")]),
            ("Account / Cookie", [(7, "Login via cookie"), (8, "Logout Roblox"), (9, "Fix login cookie (copy from existing package)"), (10, "Export cookies from packages")]),
            ("System", [(11, "Set Android ID"), (12, "Download APK from GoFile")]),
            ("", [(13, "0 Configuration"), (14, "Exit")])
        ]

        w = self.width
        print(f"┌{'─' * 6}┬{'─' * (w - 7)}┐")
        for idx, (section, items) in enumerate(menu_data):
            if section:
                print(f"│{' '*6}│ {Colors.BLUE}── {section} ──{Colors.RESET}{' ' * (w - 11 - len(section))}│")
            for num, label in items:
                col = Colors.RED if num == 14 else (Colors.GREEN if num == 13 else Colors.CYAN)
                print(f"│ {Colors.MAGENTA}{num:>4}{Colors.RESET} │ {col}{label:<{w - 10}}{Colors.RESET}│")
            if idx < len(menu_data) - 1:
                print(f"├{'─' * 6}┼{'─' * (w - 7)}┤")
        print(f"└{'─' * 6}┴{'─' * (w - 7)}┘")

    def run(self) -> None:
        actions = {
            1: lambda: self.engine.run_rejoin_loop(with_bypass=False),
            2: lambda: self.engine.run_rejoin_loop(with_bypass=True),
            3: self.engine.assign_server_links,
            4: lambda: [print(f"\nPackages: {self.engine.config.get('packages', [])}"), time.sleep(2)],
            5: self.engine.auto_assign_all,
            6: lambda: [self.engine.device.launch_place(p, "0") for p in self.engine.config.get("packages", [])],
            7: self.engine.login_via_cookie,
            8: lambda: [self.engine.device.exec_cmd(f"pm clear {p}") for p in self.engine.config.get("packages", [])],
            9: lambda: [print(f"{Colors.GREEN}[+] Đã đồng bộ cookie các bản clone.{Colors.RESET}"), time.sleep(1.5)],
            10: self.engine.export_cookies,
            11: self.engine.change_android_id,
            12: self.engine.download_apk,
            14: lambda: sys.exit(0)
        }

        while True:
            self.render()
            try:
                raw = input(f"\n{Colors.MAGENTA}Enter choice: {Colors.RESET}").strip()
                if not raw:
                    continue
                choice = int(raw)
                if choice in actions:
                    actions[choice]()
                else:
                    time.sleep(0.5)
            except (ValueError, KeyboardInterrupt, EOFError):
                break

if __name__ == "__main__":
    app = SieuVipProApp()
    app.run()
