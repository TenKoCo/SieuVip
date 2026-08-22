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
        """Quét đa tầng toàn bộ package: Android PM (mọi User/Clone) + Quét thư mục /data/data/."""
        packages = set()

        # Tầng 1: Quét Package Manager của tất cả user profile (kèm app clone/không gian kép)
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

        # Tầng 2: Quét trực tiếp thư mục /data/data/ (Yêu cầu Root) để bắt mọi app ẩn/clone
        ok, data_out = cls.exec_cmd("ls -1 /data/data/")
        if ok and data_out:
            for item in data_out.splitlines():
                item = item.strip()
                if item and "." in item:
                    packages.add(item)

        # Tầng 3: Quét không gian Dual App (User 999 / Parallel Spaces)
        ok, dual_out = cls.exec_cmd("ls -1 /data/user/ 2>/dev/null")
        if ok and dual_out:
            for uid in dual_out.splitlines():
                uid = uid.strip()
                if uid.isdigit() and uid != "0":
                    _, u_apps = cls.exec_cmd(f"ls -1 /data/user/{uid}/ 2>/dev/null")
                    for app in (u_apps.splitlines() if u_apps else []):
                        if "." in app.strip():
                            packages.add(app.strip())

        return sorted(list(packages))

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

    def parse_place_info(self, raw_input: str) -> Tuple[Optional[str], Optional[str]]:
        place_match = re.search(r"games/(\d+)", raw_input)
        place_id = place_match.group(1) if place_match else None
        
        job_match = re.search(r"gameInstanceId=([a-f0-9\-]+)", raw_input)
        job_id = job_match.group(1) if job_match else None

        if not place_id and raw_input.isdigit():
            place_id = raw_input
            
        return place_id, job_id

    def filter_and_select_packages(self) -> None:
        print(f"\n{Colors.CYAN}[*] Đang quét toàn diện hệ thống (System, Clones, Dual Apps)...{Colors.RESET}")
        all_pkgs = self.device.get_all_packages()
        print(f"{Colors.GREEN}[+] Tổng số package phát hiện: {len(all_pkgs)}{Colors.RESET}")

        keyword = input(f"\n{Colors.MAGENTA}Nhập từ khóa package (vd: free, roblox, vphone): {Colors.RESET}").strip().lower()
        if not keyword:
            print(f"{Colors.RED}[!] Không được bỏ trống.{Colors.RESET}")
            time.sleep(1.5)
            return

        # Tìm kiếm không phân biệt hoa thường và loại bỏ khoảng trắng thừa
        matched = [p for p in all_pkgs if keyword in p.lower()]

        if not matched:
            print(f"{Colors.RED}[!] Vẫn không tìm thấy package nào chứa từ '{keyword}'.{Colors.RESET}")
            # Hỗ trợ nhập thủ công nếu quét tự động không ra
            manual = input(f"{Colors.YELLOW}Bạn có muốn tự gõ trực tiếp tên Package không? (y/n): {Colors.RESET}").strip().lower()
            if manual == "y":
                direct_pkg = input(f"{Colors.MAGENTA}Nhập chính xác tên package (vd: free.xxx.xxx): {Colors.RESET}").strip()
                if direct_pkg:
                    matched = [direct_pkg]
                else:
                    return
            else:
                time.sleep(1.5)
                return

        print(f"\n{Colors.GREEN}[+] Danh sách Package khớp:{Colors.RESET}")
        for i, p in enumerate(matched, start=1):
            print(f"  {i}. {p}")

        link = input(f"\n{Colors.MAGENTA}Nhập Server Link / Place ID: {Colors.RESET}").strip()

        self.config["packages"] = matched
        for p in matched:
            self.config.setdefault("server_links", {})[p] = link

        self._save_config()
        print(f"{Colors.GREEN}[+] Đã lưu {len(matched)} packages.{Colors.RESET}")
        time.sleep(2)

    def login_via_cookie_menu(self) -> None:
        cookies = self._read_cookie_file()
        if not cookies:
            print(f"\n{Colors.RED}[!] File cookie.txt rỗng hoặc chưa nhập cookie hợp lệ.{Colors.RESET}")
            input(f"{Colors.MAGENTA}Bấm Enter để quay lại...{Colors.RESET}")
            return

        print(f"\n{Colors.CYAN}--- Menu Login Cookie (Có {len(cookies)} cookies) ---{Colors.RESET}")
        print(" [1] Login to all package (Gán tuần tự)")
        print(" [2] Login to select package (Chọn app cụ thể rồi bấm 0 để lưu)")
        
        choice = input(f"\n{Colors.MAGENTA}Chọn (1 hoặc 2): {Colors.RESET}").strip()

        if choice == "1":
            pkgs = self.config.get("packages", [])
            if not pkgs:
                print(f"{Colors.RED}[!] Chưa chọn package ở mục 3.{Colors.RESET}")
                time.sleep(2)
                return

            limit = min(len(pkgs), len(cookies))
            print(f"{Colors.YELLOW}[*] Đang nạp {limit} cookies...{Colors.RESET}")
            for idx in range(limit):
                pkg = pkgs[idx]
                ck = cookies[idx]
                self.device.inject_cookie_to_pkg(pkg, ck)
                self.config.setdefault("cookies", {})[pkg] = ck
                print(f"{Colors.GREEN} [+] Đã gán Cookie #{idx+1} -> {pkg}{Colors.RESET}")
            
            self._save_config()
            print(f"{Colors.GREEN}[+] Hoàn tất!{Colors.RESET}")
            time.sleep(2)

        elif choice == "2":
            pkgs = self.config.get("packages", [])
            if not pkgs:
                print(f"{Colors.RED}[!] Chưa chọn package ở mục 3.{Colors.RESET}")
                time.sleep(2)
                return

            selected_mapping: List[str] = []
            cookie_idx = 0

            while cookie_idx < len(cookies):
                print(f"\n{Colors.CYAN}--- Gán Cookie #{cookie_idx + 1} ---{Colors.RESET}")
                for i, p in enumerate(pkgs, start=1):
                    status = f"{Colors.GREEN}(Đã chọn){Colors.RESET}" if p in selected_mapping else ""
                    print(f" [{i}] {p} {status}")
                print(f" [0] {Colors.YELLOW}Dừng chọn và bắt đầu nạp Cookie{Colors.RESET}")

                pick = input(f"{Colors.MAGENTA}Chọn số thứ tự Package: {Colors.RESET}").strip()
                if pick == "0":
                    break

                if pick.isdigit() and 1 <= int(pick) <= len(pkgs):
                    target_pkg = pkgs[int(pick) - 1]
                    if target_pkg in selected_mapping:
                        print(f"{Colors.YELLOW}[!] Package này đã được chọn. Chọn app khác.{Colors.RESET}")
                        continue
                    selected_mapping.append(target_pkg)
                    cookie_idx += 1
                else:
                    print(f"{Colors.RED}[!] Lựa chọn không hợp lệ.{Colors.RESET}")

            if not selected_mapping:
                print(f"{Colors.YELLOW}[*] Hủy nạp Cookie.{Colors.RESET}")
                time.sleep(1.5)
                return

            print(f"\n{Colors.YELLOW}[*] Đang nạp Cookie vào các app đã chọn...{Colors.RESET}")
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
            print(f"{Colors.RED}[!] Chưa có package nào. Hãy chọn mục 3 trước.{Colors.RESET}")
            time.sleep(2)
            return

        interval = self.config.get("check_ui_time", 180)
        print(f"{Colors.GREEN}[+] SieuVipPro Auto Rejoin ({len(pkgs)} packages | Chu kỳ: {interval}s). Bấm Ctrl+C để dừng.{Colors.RESET}")

        try:
            while True:
                for pkg in self.config.get("packages", []):
                    link = self.config.get("server_links", {}).get(pkg, "")
                    place_id, job_id = self.parse_place_info(link)
                    
                    if not place_id:
                        continue

                    if with_bypass:
                        self.device.set_android_id(os.urandom(8).hex())
                    
                    print(f"{Colors.CYAN}[SieuVipPro] Rejoin -> {pkg}{Colors.RESET}")
                    self.device.launch_place(pkg, place_id, job_id)

                for remaining in range(interval, 0, -1):
                    sys.stdout.write(f"\r{Colors.YELLOW}[SieuVipPro] Quét lại sau: {remaining}s... {Colors.RESET}")
                    sys.stdout.flush()
                    time.sleep(1)
                print()
        except KeyboardInterrupt:
            print(f"\n{Colors.YELLOW}[!] Đã dừng.{Colors.RESET}")
            time.sleep(1.5)

    def list_packages(self) -> None:
        pkgs = self.config.get("packages", [])
        print(f"\n{Colors.CYAN}--- Packages Hiện Tại ---{Colors.RESET}")
        if not pkgs:
            print("Chưa có package.")
        for i, p in enumerate(pkgs, start=1):
            link = self.config.get("server_links", {}).get(p, "Không có link")
            print(f" {i}. {p} | Link: {link}")
        input(f"\n{Colors.MAGENTA}Bấm Enter để quay lại...{Colors.RESET}")

    def export_cookies(self) -> None:
        print(f"\n{Colors.CYAN}--- Danh Sách Cookie ---{Colors.RESET}")
        cookies = self.config.get("cookies", {})
        if not cookies:
            print("Chưa có cookie nào.")
        for pkg, c in cookies.items():
            print(f"[{pkg}]: {c[:30]}... (ẩn)")
        input(f"\n{Colors.MAGENTA}Bấm Enter để quay lại...{Colors.RESET}")

    def download_apk(self) -> None:
        url = input(f"{Colors.MAGENTA}Nhập URL APK: {Colors.RESET}").strip()
        if not url:
            return
        cmd = f"curl -L \"{url}\" -o /sdcard/Download/roblox_update.apk"
        ok, _ = self.device.exec_cmd(cmd)
        if ok:
            self.device.exec_cmd("pm install -r /sdcard/Download/roblox_update.apk")
        time.sleep(2)

    def change_android_id(self) -> None:
        new_id = input(f"{Colors.MAGENTA}Nhập ID mới (hoặc Enter để random): {Colors.RESET}").strip()
        if not new_id:
            new_id = os.urandom(8).hex()
        self.device.set_android_id(new_id)
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
            9: lambda: [print(f"{Colors.GREEN}[+] Đã đồng bộ cookie.{Colors.RESET}"), time.sleep(1.5)],
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
