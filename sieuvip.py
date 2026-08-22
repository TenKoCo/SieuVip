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
    def get_all_packages(cls) -> List[str]:
        """Lấy toàn bộ danh sách package ứng dụng đã cài trên máy."""
        success, out = cls.exec_cmd("pm list packages")
        if not success or not out:
            return []
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

    @classmethod
    def inject_cookie_to_pkg(cls, pkg: str, raw_cookie: str) -> bool:
        """Inject cookie ROBLOSECURITY trực tiếp vào shared_prefs XML của package."""
        c = raw_cookie.strip()
        if not c.startswith("_|WARNING:-DO-NOT-SHARE-THIS"):
            c = f"_|WARNING:-DO-NOT-SHARE-THIS.--Sharing-this-will-allow-someone-to-log-into-your-account-and-rob-your-robox.--|_{c}"

        xml_path = f"/data/data/{pkg}/shared_prefs/com.roblox.client_preferences.xml"
        # Tạo file XML nếu chưa có hoặc thay thế session
        cmd = (
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
                return [line.strip() for line in f.readlines() if line.strip()]
        except Exception:
            return []

    def parse_place_info(self, raw_input: str) -> Tuple[Optional[str], Optional[str]]:
        place_match = re.search(r"games/(\d+)", raw_input)
        place_id = place_match.group(1) if place_match else None
        
        job_match = re.search(r"gameInstanceId=([a-f0-9\-]+)", raw_input)
        job_id = job_match.group(1) if job_match else None

        if not place_id and raw_input.isdigit():
            place_id = raw_input
            
        return place_id, job_id

    def filter_and_select_packages(self) -> None:
        """Lọc packages theo từ khóa nhập vào (vd: 'free' -> lấy toàn bộ package có chứa chữ 'free')."""
        keyword = input(f"\n{Colors.MAGENTA}Nhập từ khóa package cần quét (vd: free, roblox, clone): {Colors.RESET}").strip().lower()
        if not keyword:
            print(f"{Colors.RED}[!] Từ khóa không được để trống.{Colors.RESET}")
            time.sleep(1.5)
            return

        all_pkgs = self.device.get_all_packages()
        matched = [p for p in all_pkgs if keyword in p.lower()]

        if not matched:
            print(f"{Colors.RED}[!] Không tìm thấy package nào khớp với từ khóa '{keyword}'.{Colors.RESET}")
            time.sleep(2)
            return

        print(f"\n{Colors.GREEN}[+] Tìm thấy {len(matched)} packages khớp với '{keyword}':{Colors.RESET}")
        for i, p in enumerate(matched, start=1):
            print(f"  {i}. {p}")

        link = input(f"\n{Colors.MAGENTA}Nhập Server Link / Place ID áp dụng cho các package trên: {Colors.RESET}").strip()
        if not link:
            print(f"{Colors.YELLOW}[!] Chưa nhập link. Các package đã được chọn nhưng chưa có link.{Colors.RESET}")

        self.config["packages"] = matched
        for p in matched:
            self.config.setdefault("server_links", {})[p] = link

        self._save_config()
        print(f"{Colors.GREEN}[+] Đã lưu {len(matched)} packages vào cấu hình.{Colors.RESET}")
        time.sleep(2)

    def login_via_cookie_menu(self) -> None:
        """Menu xử lý đăng nhập Cookie từ file cookie.txt."""
        cookies = self._read_cookie_file()
        if not cookies:
            print(f"\n{Colors.RED}[!] File cookie.txt tại /sdcard/Download/cookie.txt đang rỗng hoặc không tồn tại.{Colors.RESET}")
            input(f"{Colors.MAGENTA}Bấm Enter để quay lại...{Colors.RESET}")
            return

        print(f"\n{Colors.CYAN}--- Cookie Login Menu (Tìm thấy {len(cookies)} cookies) ---{Colors.RESET}")
        print(" [1] Login to all package (Gán tuần tự Cookie 1 -> App 1, Cookie 2 -> App 2...)")
        print(" [2] Login to select package (Chọn app cụ thể từ danh sách mục 3)")
        
        choice = input(f"\n{Colors.MAGENTA}Chọn chức năng (1 hoặc 2): {Colors.RESET}").strip()

        if choice == "1":
            pkgs = self.config.get("packages", [])
            if not pkgs:
                print(f"{Colors.RED}[!] Danh sách package rỗng. Hãy quét package ở mục 3 trước.{Colors.RESET}")
                time.sleep(2)
                return

            limit = min(len(pkgs), len(cookies))
            print(f"{Colors.YELLOW}[*] Bắt đầu nạp Cookie cho {limit} packages...{Colors.RESET}")
            for idx in range(limit):
                pkg = pkgs[idx]
                ck = cookies[idx]
                self.device.inject_cookie_to_pkg(pkg, ck)
                self.config.setdefault("cookies", {})[pkg] = ck
                print(f"{Colors.GREEN} [+] Đã gán Cookie #{idx+1} -> {pkg}{Colors.RESET}")
            
            self._save_config()
            print(f"{Colors.GREEN}[+] Hoàn tất nạp Cookie cho toàn bộ app!{Colors.RESET}")
            time.sleep(2)

        elif choice == "2":
            pkgs = self.config.get("packages", [])
            if not pkgs:
                print(f"{Colors.RED}[!] Danh sách package rỗng. Hãy quét package ở mục 3 trước.{Colors.RESET}")
                time.sleep(2)
                return

            selected_mapping: List[str] = []
            cookie_idx = 0

            while cookie_idx < len(cookies):
                print(f"\n{Colors.CYAN}--- Chọn Package cho Cookie #{cookie_idx + 1} ---{Colors.RESET}")
                for i, p in enumerate(pkgs, start=1):
                    status = f"{Colors.GREEN}(Đã chọn){Colors.RESET}" if p in selected_mapping else ""
                    print(f" [{i}] {p} {status}")
                print(f" [0] {Colors.YELLOW}Dừng chọn và bắt đầu nạp Cookie{Colors.RESET}")

                pick = input(f"{Colors.MAGENTA}Chọn số tương ứng với Package: {Colors.RESET}").strip()
                if pick == "0":
                    break

                if pick.isdigit() and 1 <= int(pick) <= len(pkgs):
                    target_pkg = pkgs[int(pick) - 1]
                    if target_pkg in selected_mapping:
                        print(f"{Colors.YELLOW}[!] Package này đã được gán Cookie. Hãy chọn app khác.{Colors.RESET}")
                        continue
                    selected_mapping.append(target_pkg)
                    cookie_idx += 1
                else:
                    print(f"{Colors.RED}[!] Lựa chọn không hợp lệ.{Colors.RESET}")

            if not selected_mapping:
                print(f"{Colors.YELLOW}[*] Không có package nào được chọn.{Colors.RESET}")
                time.sleep(1.5)
                return

            print(f"\n{Colors.YELLOW}[*] Đang thực thi nạp Cookie vào các app đã chọn...{Colors.RESET}")
            for idx, pkg in enumerate(selected_mapping):
                ck = cookies[idx]
                self.device.inject_cookie_to_pkg(pkg, ck)
                self.config.setdefault("cookies", {})[pkg] = ck
                print(f"{Colors.GREEN} [+] Cookie #{idx+1} -> {pkg}{Colors.RESET}")

            self._save_config()
            print(f"{Colors.GREEN}[+] Hoàn tất!{Colors.RESET}")
            time.sleep(2)

    def run_rejoin_loop(self, with_bypass: bool = False) -> None:
        pkgs = self.config.get("packages", [])
        if not pkgs:
            print(f"{Colors.RED}[!] SieuVipPro: Chưa cấu hình packages. Hãy chọn mục 3 trước.{Colors.RESET}")
            time.sleep(2)
            return

        interval = self.config.get("check_ui_time", 180)
        print(f"{Colors.GREEN}[+] [SieuVipPro] Auto Rejoin bắt đầu ({len(pkgs)} packages | Chu kỳ: {interval}s | Bypass={with_bypass}). Bấm Ctrl+C để dừng.{Colors.RESET}")

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
                    
                    print(f"{Colors.CYAN}[SieuVipPro] Chạy -> {pkg} | Place: {place_id} | Instance: {job_id or 'Auto'}{Colors.RESET}")
                    self.device.launch_place(pkg, place_id, job_id)

                for remaining in range(interval, 0, -1):
                    sys.stdout.write(f"\r{Colors.YELLOW}[SieuVipPro] Quét lại sau: {remaining}s... {Colors.RESET}")
                    sys.stdout.flush()
                    time.sleep(1)
                print()
        except KeyboardInterrupt:
            print(f"\n{Colors.YELLOW}[!] Đã dừng Auto Rejoin.{Colors.RESET}")
            time.sleep(1.5)

    def list_packages(self) -> None:
        pkgs = self.config.get("packages", [])
        print(f"\n{Colors.CYAN}--- Danh Sách Packages Đang Chọn ---{Colors.RESET}")
        if not pkgs:
            print("Chưa có package nào được chọn.")
        for i, p in enumerate(pkgs, start=1):
            link = self.config.get("server_links", {}).get(p, "Chưa gán link")
            print(f" {i}. {p} | Link: {link}")
        input(f"\n{Colors.MAGENTA}Bấm Enter để quay lại...{Colors.RESET}")

    def export_cookies(self) -> None:
        print(f"\n{Colors.CYAN}--- SieuVipPro: Danh Sách Cookie ---{Colors.RESET}")
        cookies = self.config.get("cookies", {})
        if not cookies:
            print("Chưa có cookie nào được gán.")
        for pkg, c in cookies.items():
            print(f"[{pkg}]: {c[:30]}... (hidden)")
        input(f"\n{Colors.MAGENTA}Bấm Enter để quay lại...{Colors.RESET}")

    def download_apk(self) -> None:
        url = input(f"{Colors.MAGENTA}Nhập URL APK: {Colors.RESET}").strip()
        if not url:
            return
        print(f"{Colors.CYAN}[*] Đang tải APK...{Colors.RESET}")
        cmd = f"curl -L \"{url}\" -o /sdcard/Download/roblox_update.apk"
        ok, _ = self.device.exec_cmd(cmd)
        if ok:
            print(f"{Colors.GREEN}[+] Đang cài đặt APK...{Colors.RESET}")
            self.device.exec_cmd("pm install -r /sdcard/Download/roblox_update.apk")
        time.sleep(2)

    def change_android_id(self) -> None:
        new_id = input(f"{Colors.MAGENTA}Nhập Android ID mới (hoặc bấm Enter để random): {Colors.RESET}").strip()
        if not new_id:
            new_id = os.urandom(8).hex()
        ok = self.device.set_android_id(new_id)
        print(f"{Colors.GREEN}[+] Đã đổi Android ID: {new_id}{Colors.RESET}" if ok else f"{Colors.RED}[!] Thất bại.{Colors.RESET}")
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
            3: self.engine.filter_and_select_packages,
            4: self.engine.list_packages,
            5: self.engine.filter_and_select_packages,
            6: lambda: [self.engine.device.launch_place(p, "0") for p in self.engine.config.get("packages", [])],
            7: self.engine.login_via_cookie_menu,
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
