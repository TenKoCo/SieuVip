#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VIP AUTO REJOIN TOOL - ANDROID / TERMUX
Phát triển lại toàn bộ tính năng: Quản lý Cookie, Tự động Rejoin, Bypass VNG, Watchdog Anti-Crash.
"""

import os
import sys
import time
import json
import subprocess
from datetime import datetime
import pytz
import requests
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt, Confirm

console = Console()
VN_TZ = pytz.timezone("Asia/Ho_Chi_Minh")
CONFIG_FILE = "config.json"
ROBLOX_PACKAGE = "com.roblox.client"  # hoặc com.vng.roblox nếu dùng bản VNG

# ==================== CẤU HÌNH & QUẢN LÝ ====================

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "cookie": "",
        "place_id": "2753915549",  # Ví dụ: Blox Fruits Sea 1
        "job_id": "",
        "link_code": "",
        "delay_check": 15,
        "auto_restart_minutes": 0,
        "discord_webhook": "",
        "package_name": "com.roblox.client"
    }

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)

def get_current_time():
    return datetime.now(VN_TZ).strftime("%H:%M:%S %d/%m/%Y")

# ==================== ROBLOX API FUNCTIONS ====================

class RobloxSession:
    def __init__(self, cookie: str):
        self.cookie = cookie.strip()
        self.session = requests.Session()
        self.session.cookies[".ROBLOSECURITY"] = self.cookie
        self.csrf_token = ""
        self.user_info = {}

    def get_csrf_token(self):
        headers = {"User-Agent": "Roblox/WinInet"}
        r = self.session.post("https://auth.roblox.com/v1/authentication-ticket", headers=headers)
        if "x-csrf-token" in r.headers:
            self.csrf_token = r.headers["x-csrf-token"]
            return self.csrf_token
        return None

    def validate_cookie(self):
        try:
            r = self.session.get("https://users.roblox.com/v1/users/authenticated", timeout=10)
            if r.status_code == 200:
                self.user_info = r.json()
                # Lấy số dư Robux
                r_robux = self.session.get(f"https://economy.roblox.com/v1/users/{self.user_info['id']}/currency", timeout=10)
                robux = r_robux.json().get("robux", 0) if r_robux.status_code == 200 else 0
                self.user_info["robux"] = robux
                return True, self.user_info
            return False, "Cookie không hợp lệ hoặc đã hết hạn."
        except Exception as e:
            return False, str(e)

    def get_auth_ticket(self):
        self.get_csrf_token()
        headers = {
            "User-Agent": "Roblox/WinInet",
            "x-csrf-token": self.csrf_token,
            "Referer": "https://www.roblox.com/",
            "Content-Type": "application/json",
        }
        try:
            r = self.session.post("https://auth.roblox.com/v1/authentication-ticket", headers=headers, timeout=10)
            ticket = r.headers.get("rbx-authentication-ticket")
            return ticket
        except Exception:
            return None

# ==================== ANDROID & PROCESS CONTROL ====================

def is_roblox_running(package_name: str) -> bool:
    try:
        output = subprocess.check_output(f"pidof {package_name}", shell=True, stderr=subprocess.DEVNULL)
        return len(output.strip()) > 0
    except Exception:
        # Cách phụ dùng pgrep
        try:
            output = subprocess.check_output(f"pgrep -f {package_name}", shell=True, stderr=subprocess.DEVNULL)
            return len(output.strip()) > 0
        except Exception:
            return False

def stop_roblox(package_name: str):
    console.log(f"[yellow][!] Đang tắt tiến trình {package_name}...[/yellow]")
    os.system(f"am force-stop {package_name} > /dev/null 2>&1")
    os.system(f"pkill -f {package_name} > /dev/null 2>&1")
    time.sleep(2)

def launch_roblox(package_name: str, place_id: str, job_id: str = "", link_code: str = ""):
    console.log(f"[cyan][+] Đang khởi chạy Roblox vào Place ID: {place_id}...[/cyan]")
    
    # Xây dựng URL intent
    if link_code:
        # Private server qua linkCode
        uri = f"robloxmobile://placeID={place_id}&linkCode={link_code}"
    elif job_id:
        # Server chỉ định qua Job ID
        uri = f"robloxmobile://placeID={place_id}&gameInstanceId={job_id}"
    else:
        # Vào server thường
        uri = f"robloxmobile://placeID={place_id}"

    # Lệnh khởi chạy Intent trên Android/Termux
    cmd = f'am start -n {package_name}/com.roblox.client.ActivityProtocolLaunch -a android.intent.action.VIEW -d "{uri}"'
    os.system(f"{cmd} > /dev/null 2>&1")

# ==================== THÔNG BÁO WEBHOOK ====================

def send_discord_log(webhook_url: str, username: str, user_id: int, status: str, place_id: str):
    if not webhook_url:
        return
    embed = {
        "title": "VIP Auto Rejoin - Báo cáo trạng thái",
        "color": 65280 if "Thành công" in status else 16711680,
        "fields": [
            {"name": "Tài khoản", "value": f"`{username}` ({user_id})", "inline": True},
            {"name": "Place ID", "value": f"`{place_id}`", "inline": True},
            {"name": "Trạng thái", "value": f"**{status}**", "inline": False},
            {"name": "Thời gian", "value": get_current_time(), "inline": False},
        ],
        "footer": {"text": "VIP Rejoin Tool"}
    }
    try:
        requests.post(webhook_url, json={"embeds": [embed]}, timeout=5)
    except Exception:
        pass

# ==================== VÒNG LẶP AUTO REJOIN ====================

def start_auto_rejoin(config):
    package_name = config.get("package_name", "com.roblox.client")
    place_id = config.get("place_id")
    job_id = config.get("job_id", "")
    link_code = config.get("link_code", "")
    delay = config.get("delay_check", 15)
    webhook = config.get("discord_webhook", "")
    auto_restart_mins = config.get("auto_restart_minutes", 0)

    rbx = RobloxSession(config.get("cookie", ""))
    valid, user = rbx.validate_cookie()
    
    if not valid:
        console.print(f"[bold red][X] Lỗi Cookie: {user}[/bold red]")
        return

    console.print(Panel(
        f"[bold green]Tài khoản:[/bold green] {user['name']} (ID: {user['id']})\n"
        f"[bold green]Số dư Robux:[/bold green] {user.get('robux', 0)} R$\n"
        f"[bold green]Place ID:[/bold green] {place_id}\n"
        f"[bold green]Chu kỳ kiểm tra:[/bold green] {delay} giây",
        title="[bold cyan]BẮT ĐẦU CHẾ ĐỘ GIÁM SÁT & REJOIN[/bold cyan]",
        expand=False
    ))

    rejoin_count = 0
    last_restart_time = time.time()

    # Khởi động lần đầu
    if not is_roblox_running(package_name):
        launch_roblox(package_name, place_id, job_id, link_code)
        rejoin_count += 1
        send_discord_log(webhook, user['name'], user['id'], "Khởi động game lần đầu", place_id)

    try:
        while True:
            time.sleep(delay)
            running = is_roblox_running(package_name)
            current_t = get_current_time()

            # Kiểm tra tính năng tự restart định kỳ
            if auto_restart_mins > 0 and (time.time() - last_restart_time) > (auto_restart_mins * 60):
                console.log(f"[yellow][!] Đạt giới hạn thời gian định kỳ ({auto_restart_mins} phút). Đang reset game...[/yellow]")
                stop_roblox(package_name)
                time.sleep(3)
                launch_roblox(package_name, place_id, job_id, link_code)
                last_restart_time = time.time()
                rejoin_count += 1
                send_discord_log(webhook, user['name'], user['id'], f"Tự khởi động lại định kỳ (Lần #{rejoin_count})", place_id)
                continue

            if not running:
                console.log(f"[bold red][!] Phát hiện Roblox đã dừng/văng lúc {current_t}. Đang tiến hành Rejoin...[/bold red]")
                stop_roblox(package_name)
                time.sleep(3)
                launch_roblox(package_name, place_id, job_id, link_code)
                rejoin_count += 1
                last_restart_time = time.time()
                console.log(f"[bold green][✓] Đã Rejoin thành công lần #{rejoin_count}[/bold green]")
                send_discord_log(webhook, user['name'], user['id'], f"Tự Rejoin sau khi Crash/Disconnect (Lần #{rejoin_count})", place_id)
            else:
                console.log(f"[green][{current_t}] Roblox đang chạy ổn định | Rejoin đã thực hiện: {rejoin_count}[/green]")

    except KeyboardInterrupt:
        console.print("\n[yellow][!] Đã dừng công cụ Rejoin.[/yellow]")

# ==================== MENU CHÍNH ====================

def main_menu():
    config = load_config()

    while True:
        os.system("clear" if os.name != "nt" else "cls")
        table = Table(title="💎 VIP AUTO REJOIN TOOL (MÃ NGUỒN SẠCH) 💎", style="cyan")
        table.add_column("Mục", justify="center", style="bold yellow")
        table.add_column("Tên chức năng", style="bold white")
        table.add_column("Giá trị hiện tại", style="green")

        table.add_row("1", "Tài khoản (Cookie)", "Đã lưu" if config.get("cookie") else "[red]Chưa nhập[/red]")
        table.add_row("2", "Place ID (Game)", str(config.get("place_id", "")))
        table.add_row("3", "Job ID (Server ID)", str(config.get("job_id", "Không dùng")))
        table.add_row("4", "Private Link Code", str(config.get("link_code", "Không dùng")))
        table.add_row("5", "Độ trễ kiểm tra (giây)", f"{config.get('delay_check', 15)}s")
        table.add_row("6", "Tự Reset định kỳ (phút)", f"{config.get('auto_restart_minutes', 0)} phút (0 = tắt)")
        table.add_row("7", "Discord Webhook", "Đã bật" if config.get("discord_webhook") else "Tắt")
        table.add_row("8", "Bắt đầu chạy Rejoin", "[bold green]START[/bold green]")
        table.add_row("0", "Thoát", "")

        console.print(table)
        choice = Prompt.ask("[cyan]Nhập lựa chọn của bạn[/cyan]", choices=["0", "1", "2", "3", "4", "5", "6", "7", "8"])

        if choice == "1":
            cookie = Prompt.ask("Nhập Cookie (.ROBLOSECURITY)")
            config["cookie"] = cookie.strip()
            rbx = RobloxSession(config["cookie"])
            valid, res = rbx.validate_cookie()
            if valid:
                console.print(f"[green][✓] Đăng nhập thành công: {res['name']} (ID: {res['id']}) - Robux: {res['robux']}[/green]")
            else:
                console.print(f"[red][X] Cookie lỗi: {res}[/red]")
            save_config(config)
            Prompt.ask("\nNhấn Enter để tiếp tục")
        elif choice == "2":
            config["place_id"] = Prompt.ask("Nhập Place ID Game", default="2753915549")
            save_config(config)
        elif choice == "3":
            config["job_id"] = Prompt.ask("Nhập Job ID (để trống nếu không dùng)", default="")
            save_config(config)
        elif choice == "4":
            config["link_code"] = Prompt.ask("Nhập Link Code Server VIP (để trống nếu không dùng)", default="")
            save_config(config)
        elif choice == "5":
            config["delay_check"] = int(Prompt.ask("Nhập số giây kiểm tra giữa các lần", default="15"))
            save_config(config)
        elif choice == "6":
            config["auto_restart_minutes"] = int(Prompt.ask("Tự khởi động lại sau mỗi X phút (0 = tắt)", default="0"))
            save_config(config)
        elif choice == "7":
            config["discord_webhook"] = Prompt.ask("Nhập URL Discord Webhook", default="")
            save_config(config)
        elif choice == "8":
            if not config.get("cookie"):
                console.print("[red][X] Vui lòng nhập Cookie trước khi chạy![/red]")
                time.sleep(2)
                continue
            start_auto_rejoin(config)
            Prompt.ask("\nNhấn Enter để quay lại Menu")
        elif choice == "0":
            console.print("[yellow]Tạm biệt![/yellow]")
            sys.exit(0)

if __name__ == "__main__":
    main_menu()
