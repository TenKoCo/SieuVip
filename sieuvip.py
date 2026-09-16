#!/usr/bin/env python3
# ============================================================
#  ROBLOX AUTO REJOIN TOOL — TERMUX ROOT
#  Author  : Axiom
#  Platform: Android / Termux (ROOT required)
#  Version : 2.1 — + Key Check + Execute Check + Script Monitor
# ============================================================

import os, sys, json, time, subprocess, threading, re, shutil
import urllib.request, urllib.parse, urllib.error
import signal, random, hashlib, socket, copy
from pathlib import Path
from datetime import datetime, timedelta

# ─────────────────────────────────────────────
#  ANSI COLORS
# ─────────────────────────────────────────────
R   = "\033[91m"; G   = "\033[92m"; Y   = "\033[93m"
B   = "\033[94m"; M   = "\033[95m"; C   = "\033[96m"
W   = "\033[97m"; DIM = "\033[2m";  RST = "\033[0m"
BOLD= "\033[1m";  UL  = "\033[4m"

def clr(text, color): return f"{color}{text}{RST}"
def bold(text):       return f"{BOLD}{text}{RST}"

# ─────────────────────────────────────────────
#  PATHS
# ─────────────────────────────────────────────
BASE_DIR     = Path(os.environ.get("HOME", "/data/data/com.termux/files/home")) / ".roblox_tool"
CONFIG_FILE  = BASE_DIR / "config.json"
LOG_FILE     = BASE_DIR / "tool.log"
CRASH_LOG    = BASE_DIR / "crash.log"
PKG_FILE     = BASE_DIR / "packages.json"
ACCOUNT_FILE = BASE_DIR / "accounts.json"
COOKIE_DIR   = BASE_DIR / "cookies"
APK_DIR      = BASE_DIR / "apks"
SCRIPT_COPY  = BASE_DIR / "roblox_tool.py"           # bản copy script trong thư mục tool
BOOT_FILE    = Path("/data/data/com.termux/files/home/.termux/boot/roblox_autostart.sh")

# ─────────────────────────────────────────────
#  DEFAULT CONFIG — 32 settings
# ─────────────────────────────────────────────
DEFAULT_CONFIG = {
    # ACCOUNT
    "account_check_method"         : "executor",
    "auto_change_bloxfruit"        : False,
    "auto_change_custom"           : False,
    "auto_change_on_captcha"       : False,
    "no_rejoin_on_captcha"         : True,
    "no_rejoin_on_faceid"          : True,
    "auto_change_on_faceid"        : True,
    "no_rejoin_on_captcha_lock"    : True,
    "auto_change_on_captcha_lock"  : True,
    "auto_block_other_accounts"    : True,
    "new_account_source"           : "this_device",
    "save_changed_accounts_to"     : "this_device",
    # TABS / WINDOW
    "sort_roblox_tabs"             : True,
    "tiny_sort_tab"                : True,
    "kill_all_tabs_if_one_crash"   : False,
    "kill_tab_when_captcha_solved" : False,
    "auto_create_termux_boot"      : True,
    # TIMING
    "rotate_account_every"         : 0,
    "open_roblox_delay"            : 5,
    "sleep_after_kill"             : 0,
    "executor_check_timeout"       : 180,
    "delay_between_tabs"           : 15,
    # MISC
    "package_prefix"               : "ugphone",
    "captcha_solver_urls"          : [],
    "zeropoint_api_key"            : "",
    "faceid_solver_urls"           : [],
    "zeropoint_priority_queue"     : False,
    "codex_login_user"             : "",
    "codex_login_pass"             : "",
    "hwid_delta_mode"              : "auto",
    "captchalock_solver_urls"      : [],
    # WEBHOOK
    "webhook_monitor_url"          : "",
    "webhook_solver_url"           : "",
    "webhook_change_url"           : "",
    "webhook_username"             : "tuat",
    "webhook_interval_min"         : 1,
    "discord_ping_target"          : "@everyone",
    # EXECUTOR / KEY CHECK
    "executor_type"                : "delta",
    "executor_pkg"                 : "",
    "executor_key"                 : "",
    "key_check_enabled"            : True,
    "key_bypass_enabled"           : False,
    "heartbeat_file"               : "/sdcard/rbx_heartbeat.txt",
    "heartbeat_timeout"            : 60,
    "script_path"                  : "",
    "execute_check_enabled"        : True,
    "execute_check_interval"       : 30,
    "rejoin_on_dead_script"        : True,
    # CORE
    "rejoin_interval"              : 5,
    "max_retries"                  : 0,
    "default_link"                 : "",
    "android_id"                   : "",
    "gofile_token"                 : "",
    "kill_before_open"             : True,
}

# ─────────────────────────────────────────────
#  LOGGER
# ─────────────────────────────────────────────
class Logger:
    _lock = threading.Lock()

    @classmethod
    def _write(cls, tag, color, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        with cls._lock:
            print(f"{color}{BOLD}[{tag}]{RST} {W}{msg}{RST}")
            try:
                with open(LOG_FILE, "a") as f:
                    f.write(f"[{ts}] [{tag}] {msg}\n")
            except:
                pass

    @classmethod
    def info(cls, m):  cls._write("INFO", C, m)
    @classmethod
    def ok(cls, m):    cls._write(" OK ", G, m)
    @classmethod
    def warn(cls, m):  cls._write("WARN", Y, m)
    @classmethod
    def error(cls, m): cls._write("ERR!", R, m)
    @classmethod
    def debug(cls, m): cls._write("DBG ", DIM, m)

log = Logger()

# ─────────────────────────────────────────────
#  SHELL
# ─────────────────────────────────────────────
def sh(cmd, root=False, timeout=30):
    if root:
        # Wrap in single quotes, escape any existing single quotes inside cmd
        safe = cmd.replace("'", "'\\''")
        cmd  = f"su -c '{safe}'"
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -1, "", str(e)

def sh_ok(cmd, root=False):
    return sh(cmd, root=root)[0] == 0

# ─────────────────────────────────────────────
#  SELF-CHECK
# ─────────────────────────────────────────────
class SelfChecker:
    REQUIRED = ["su", "am", "pm", "settings", "curl"]

    @classmethod
    def check_root(cls):
        c, o, _ = sh("id", root=True)
        return c == 0 and "uid=0" in o

    @classmethod
    def check_bins(cls):
        return [b for b in cls.REQUIRED if not shutil.which(b)]

    @classmethod
    def fix_bins(cls, missing):
        pkg_map = {"su": "tsu", "curl": "curl", "wget": "wget", "python3": "python"}
        to_install = list({pkg_map[b] for b in missing if b in pkg_map})
        if to_install:
            log.info(f"Auto-installing: {' '.join(to_install)}")
            sh(f"pkg install -y {' '.join(to_install)}")

    @classmethod
    def check_network(cls):
        try:
            socket.create_connection(("8.8.8.8", 53), timeout=3)
            return True
        except:
            return False

    @classmethod
    def run_all(cls):
        sep = clr("=" * 52, B)
        print(f"\n{sep}")
        print(f"  {bold(clr('SELF-CHECK', C))}")
        print(sep)

        root_ok = cls.check_root()
        if root_ok:
            log.ok("Root access confirmed.")
        else:
            log.error("Root NOT available.")
            print(f"  {Y}Fix: Install Magisk/KernelSU then retry.{RST}")
            return False

        missing = cls.check_bins()
        if missing:
            cls.fix_bins(missing)
            missing = cls.check_bins()
        if missing:
            log.warn(f"Still missing: {missing} (non-fatal for some features)")
        else:
            log.ok("All binaries present.")

        for d in [BASE_DIR, COOKIE_DIR, APK_DIR]:
            d.mkdir(parents=True, exist_ok=True)
        log.ok("Directories initialized.")

        if cls.check_network():
            log.ok("Network reachable.")
        else:
            log.warn("Network unreachable — some features may fail.")

        print(f"\n  {G}{BOLD}✓ ALL CRITICAL CHECKS PASSED{RST}")
        print(sep + "\n")
        return True

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
class Config:
    _data = {}

    @classmethod
    def load(cls):
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE) as f:
                    cls._data = {**copy.deepcopy(DEFAULT_CONFIG), **json.load(f)}
            except:
                cls._data = copy.deepcopy(DEFAULT_CONFIG)
        else:
            cls._data = copy.deepcopy(DEFAULT_CONFIG)
            cls.save()

    @classmethod
    def save(cls):
        with open(CONFIG_FILE, "w") as f:
            json.dump(cls._data, f, indent=2)

    @classmethod
    def get(cls, key, default=None):
        return cls._data.get(key, DEFAULT_CONFIG.get(key, default))

    @classmethod
    def set(cls, key, val):
        cls._data[key] = val

    @classmethod
    def commit(cls):
        cls.save()

# ─────────────────────────────────────────────
#  ACCOUNT MANAGER
# ─────────────────────────────────────────────
class AccountManager:
    _accounts = []
    _blocked   = set()
    _lock      = threading.Lock()

    @classmethod
    def load(cls):
        if ACCOUNT_FILE.exists():
            try:
                with open(ACCOUNT_FILE) as f:
                    d = json.load(f)
                cls._accounts = d.get("accounts", [])
                cls._blocked  = set(d.get("blocked", []))
            except:
                cls._accounts = []; cls._blocked = set()

    @classmethod
    def save(cls):
        # Must be called inside cls._lock
        with open(ACCOUNT_FILE, "w") as f:
            json.dump({"accounts": cls._accounts,
                       "blocked": list(cls._blocked)}, f, indent=2)

    @classmethod
    def add(cls, cookie, pkg=""):
        with cls._lock:
            if cookie and cookie not in [a["cookie"] for a in cls._accounts]:
                cls._accounts.append({"cookie": cookie, "pkg": pkg, "blocked": False})
                cls.save()
                log.ok(f"Account added (total: {len(cls._accounts)})")

    @classmethod
    def get_next(cls, current_cookie="", exclude_cookies: set = None):
        """
        Thread-safe: chỉ trả về acc chưa bị block, không phải acc hiện tại,
        và không nằm trong exclude_cookies (để tránh 2 worker nhận cùng acc).
        """
        with cls._lock:
            excl = set(exclude_cookies or set()) | {current_cookie}
            available = [
                a for a in cls._accounts
                if not a.get("blocked")
                and not a.get("_in_flight")   # skip accounts being assigned right now
                and a["cookie"] not in cls._blocked
                and a["cookie"] not in excl
            ]
            if not available:
                return None
            chosen = available[0]
            chosen["_in_flight"] = True   # reserve until confirm_rotation() or block()
            return chosen

    @classmethod
    def confirm_rotation(cls, cookie):
        """Call after successfully injecting — clears the in-flight flag."""
        with cls._lock:
            for a in cls._accounts:
                if a["cookie"] == cookie:
                    a.pop("_in_flight", None)

    @classmethod
    def block(cls, cookie):
        """Block a cookie globally — affects all packages using it."""
        with cls._lock:
            cls._blocked.add(cookie)
            for a in cls._accounts:
                if a["cookie"] == cookie:
                    a["blocked"] = True
                    a.pop("_in_flight", None)
            cls.save()
        log.warn(f"Account blocked globally: ...{cookie[-20:]}")

    @classmethod
    def get_pkgs_using(cls, cookie) -> list:
        """Return list of package names currently using this cookie."""
        return [pkg for pkg, data in PackageManager._pkgs.items()
                if data.get("cookie","") == cookie]

    @classmethod
    def list_all(cls):
        return cls._accounts

    @classmethod
    def is_blocked(cls, cookie) -> bool:
        return cookie in cls._blocked

# ─────────────────────────────────────────────
#  PACKAGE MANAGER
# ─────────────────────────────────────────────
class PackageManager:
    _pkgs = {}

    @classmethod
    def load(cls):
        if PKG_FILE.exists():
            try:
                with open(PKG_FILE) as f:
                    cls._pkgs = json.load(f)
            except:
                cls._pkgs = {}

    @classmethod
    def save(cls):
        with open(PKG_FILE, "w") as f:
            json.dump(cls._pkgs, f, indent=2)

    @classmethod
    def get_installed_roblox(cls):
        prefix = Config.get("package_prefix", "ugphone")
        _, out, _ = sh("pm list packages", root=True)
        pkgs = []
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("package:"):
                pkg = line.replace("package:", "").strip()
                if "roblox" in pkg.lower() or prefix.lower() in pkg.lower():
                    pkgs.append(pkg)
        return pkgs

    @classmethod
    def get_all_packages(cls):
        _, out, _ = sh("pm list packages", root=True)
        pkgs = []
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("package:"):
                pkgs.append(line.replace("package:", "").strip())
        return sorted(pkgs)

    @classmethod
    def select(cls, pkg, link):
        cls._pkgs.setdefault(pkg, {})
        cls._pkgs[pkg].update({
            "link": link, "enabled": True,
            "cookie": cls._pkgs[pkg].get("cookie", ""),
            "crash_count": cls._pkgs[pkg].get("crash_count", 0)
        })
        cls.save()
        log.ok(f"Package selected: {pkg}")

    @classmethod
    def auto_select_all(cls, link):
        pkgs = cls.get_installed_roblox()
        if not pkgs:
            log.warn("No Roblox packages found.")
            return 0
        for p in pkgs:
            cls.select(p, link)
        log.ok(f"Auto-selected {len(pkgs)} package(s).")
        return len(pkgs)

    @classmethod
    def get_enabled(cls):
        return [(k, v) for k, v in cls._pkgs.items() if v.get("enabled", True)]

    @classmethod
    def list_selected(cls):
        return cls._pkgs

    @classmethod
    def increment_crash(cls, pkg):
        if pkg in cls._pkgs:
            cls._pkgs[pkg]["crash_count"] = cls._pkgs[pkg].get("crash_count", 0) + 1
            cls.save()

# ─────────────────────────────────────────────
#  COOKIE MANAGER
# ─────────────────────────────────────────────
class CookieManager:
    @classmethod
    def _file(cls, pkg):
        return COOKIE_DIR / f"{pkg}.txt"

    @classmethod
    def inject(cls, pkg, cookie):
        cookie = cookie.strip()
        if not cookie:
            log.error("Empty cookie."); return False
        cls._file(pkg).write_text(cookie)
        PackageManager._pkgs.setdefault(pkg, {})["cookie"] = cookie
        PackageManager.save()

        sh(f"am force-stop {pkg}", root=True)
        time.sleep(0.8)

        sp = f"/data/data/{pkg}/shared_prefs"
        sh(f"mkdir -p {sp}", root=True)
        xml_content = (
            "<?xml version='1.0' encoding='utf-8' standalone='yes' ?>\n"
            "<map>\n"
            f"    <string name=\".ROBLOSECURITY\">{cookie}</string>\n"
            "</map>"
        )
        tmp = "/data/local/tmp/_rbxck.xml"
        # Use printf to avoid shell quoting issues with long cookies
        sh(f"printf '%s' '{xml_content.replace(chr(39), chr(39)+chr(92)+chr(39)+chr(39))}' > {tmp}", root=True)
        sh(f"cp {tmp} {sp}/RobloxCookies.xml", root=True)
        sh(f"chmod 660 {sp}/RobloxCookies.xml", root=True)
        sh(f"chown {pkg}:{pkg} {sp}/RobloxCookies.xml 2>/dev/null || true", root=True)
        sh(f"restorecon {sp}/RobloxCookies.xml 2>/dev/null || true", root=True)
        sh(f"rm -f {tmp}", root=True)
        log.ok(f"Cookie injected → {pkg}")
        return True

    @classmethod
    def logout(cls, pkg):
        sh(f"am force-stop {pkg}", root=True)
        sh(f"pm clear {pkg}", root=True)
        if cls._file(pkg).exists():
            cls._file(pkg).unlink()
        if pkg in PackageManager._pkgs:
            PackageManager._pkgs[pkg]["cookie"] = ""
            PackageManager.save()
        log.ok(f"Logged out: {pkg}")

    @classmethod
    def fix_from(cls, source, target):
        cookie = PackageManager._pkgs.get(source, {}).get("cookie", "")
        if not cookie:
            f = cls._file(source)
            if f.exists():
                cookie = f.read_text().strip()
        if not cookie:
            log.error(f"No cookie found in source: {source}"); return
        cls.inject(target, cookie)
        log.ok(f"Cookie fixed: {source} → {target}")

    @classmethod
    def export_all(cls):
        result = {}
        for pkg, data in PackageManager._pkgs.items():
            c = data.get("cookie", "")
            if not c:
                f = cls._file(pkg)
                if f.exists():
                    c = f.read_text().strip()
            if c:
                result[pkg] = c
                (COOKIE_DIR / f"export_{pkg}.txt").write_text(c)
        log.ok(f"Exported {len(result)} cookie(s) → {COOKIE_DIR}")
        return result

    @classmethod
    def read(cls, pkg):
        c = PackageManager._pkgs.get(pkg, {}).get("cookie", "")
        if not c:
            f = cls._file(pkg)
            if f.exists():
                c = f.read_text().strip()
        return c

# ─────────────────────────────────────────────
#  ANDROID ID
# ─────────────────────────────────────────────
class AndroidIDManager:
    @staticmethod
    def get():
        _, out, _ = sh("settings get secure android_id", root=True)
        return out.strip()

    @staticmethod
    def set(new_id):
        new_id = new_id.strip().lower()
        if len(new_id) != 16 or not all(c in "0123456789abcdef" for c in new_id):
            log.error("Android ID must be exactly 16 hex chars."); return False
        c, _, err = sh(f"settings put secure android_id {new_id}", root=True)
        if c == 0:
            Config.set("android_id", new_id); Config.commit()
            log.ok(f"Android ID → {new_id}"); return True
        log.error(f"Failed: {err}"); return False

    @staticmethod
    def random():
        return hashlib.md5(os.urandom(16)).hexdigest()[:16]

# ─────────────────────────────────────────────
#  GOFILE DOWNLOADER
# ─────────────────────────────────────────────
class GoFileDownloader:
    API = "https://api.gofile.io"

    @classmethod
    def _req(cls, url, token=""):
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())

    @classmethod
    def download(cls, url, token=""):
        fid = url.rstrip("/").split("/")[-1]
        log.info(f"GoFile ID: {fid}")
        try:
            data = cls._req(f"{cls.API}/contents/{fid}", token)
        except Exception as e:
            log.error(f"API error: {e}"); return None

        if data.get("status") != "ok":
            log.error(f"GoFile status: {data.get('status')}"); return None

        children = data.get("data", {}).get("children", {})
        apks = [v for v in children.values()
                if v.get("type") == "file" and v.get("name", "").endswith(".apk")]
        if not apks:
            log.error("No APK found."); return None

        apk = apks[0]
        out = APK_DIR / apk["name"]
        log.info(f"Downloading: {apk['name']}")
        try:
            headers = {}
            if token:
                headers["Authorization"] = f"Bearer {token}"
                headers["Cookie"] = f"accountToken={token}"
            req = urllib.request.Request(apk["link"], headers=headers)
            with urllib.request.urlopen(req, timeout=120) as r:
                total = int(r.headers.get("Content-Length", 0))
                done  = 0
                with open(out, "wb") as f:
                    while True:
                        chunk = r.read(65536)
                        if not chunk: break
                        f.write(chunk)
                        done += len(chunk)
                        if total:
                            pct = done / total * 100
                            bar = "█" * int(pct // 5) + "░" * (20 - int(pct // 5))
                            print(f"\r  {G}[{bar}]{RST} {pct:.1f}%  ", end="", flush=True)
            print()
            log.ok(f"Saved: {out}")
            return out
        except Exception as e:
            log.error(f"Download failed: {e}"); return None

    @classmethod
    def install(cls, path):
        log.info(f"Installing: {path.name}")
        c, out, err = sh(f"pm install -r -d '{path}'", root=True, timeout=120)
        if c == 0 or "Success" in out:
            log.ok(f"Installed: {path.name}"); return True
        log.error(f"Install failed: {err or out}"); return False

# ─────────────────────────────────────────────
#  WEBHOOK
# ─────────────────────────────────────────────
class Webhook:
    _last_sent = {}

    @classmethod
    def _send(cls, url, content, ping=""):
        if not url: return
        payload = json.dumps({
            "content": f"{ping} {content}".strip(),
            "username": Config.get("webhook_username", "tuat")
        }).encode()
        try:
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            log.debug(f"Webhook send error: {e}")

    @classmethod
    def monitor(cls, msg):
        interval = Config.get("webhook_interval_min", 1) * 60
        now = time.time()
        if now - cls._last_sent.get("monitor", 0) >= interval:
            ping = Config.get("discord_ping_target", "@everyone")
            cls._send(Config.get("webhook_monitor_url", ""), msg, ping)
            cls._last_sent["monitor"] = now

    @classmethod
    def solver(cls, msg):
        cls._send(Config.get("webhook_solver_url", ""), msg)

    @classmethod
    def change(cls, msg):
        cls._send(Config.get("webhook_change_url", ""), msg)

# ─────────────────────────────────────────────
#  CAPTCHA / FACEID SOLVER
# ─────────────────────────────────────────────
class CaptchaSolver:
    @classmethod
    def _try_urls(cls, urls, payload):
        for url in urls:
            try:
                data = json.dumps(payload).encode()
                req  = urllib.request.Request(
                    url, data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=30) as r:
                    result = json.loads(r.read())
                    token  = result.get("token") or result.get("solution")
                    if token: return token
            except Exception as e:
                log.debug(f"Solver {url}: {e}")
        return None

    @classmethod
    def solve_captcha(cls, pkg):
        urls = Config.get("captcha_solver_urls", [])
        if not urls:
            log.warn(f"No captcha solver URLs set."); return False
        log.info(f"Solving captcha: {pkg}")
        token = cls._try_urls(urls, {"package": pkg, "type": "captcha"})
        if token:
            log.ok(f"Captcha solved: {pkg}")
            Webhook.solver(f"✔ Captcha solved: {pkg}")
            return True
        log.warn(f"Captcha solve failed: {pkg}")
        return False

    @classmethod
    def solve_faceid(cls, pkg):
        urls = Config.get("faceid_solver_urls", [])
        api  = Config.get("zeropoint_api_key", "")
        if not urls and not api:
            log.warn(f"No faceID solver configured."); return False
        log.info(f"Solving faceID: {pkg}")
        if api:
            try:
                req = urllib.request.Request(
                    "https://api.zeropoint.io/v1/faceid",
                    data=json.dumps({"package": pkg, "api_key": api}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                pq = Config.get("zeropoint_priority_queue", False)
                if pq: req.add_header("X-Priority-Queue", "true")
                with urllib.request.urlopen(req, timeout=30) as r:
                    result = json.loads(r.read())
                if result.get("success"):
                    log.ok(f"FaceID solved via ZeroPoint: {pkg}")
                    return True
            except Exception as e:
                log.debug(f"ZeroPoint: {e}")
        if urls:
            token = cls._try_urls(urls, {"package": pkg, "type": "faceid"})
            if token:
                log.ok(f"FaceID solved: {pkg}"); return True
        log.warn(f"FaceID solve failed: {pkg}")
        return False

    @classmethod
    def solve_captcha_lock(cls, pkg):
        urls = Config.get("captchalock_solver_urls", [])
        if not urls:
            log.warn(f"No captcha-lock solver URLs set."); return False
        log.info(f"Solving captcha-lock: {pkg}")
        token = cls._try_urls(urls, {"package": pkg, "type": "captcha_lock"})
        if token:
            log.ok(f"Captcha-lock solved: {pkg}"); return True
        return False

# ─────────────────────────────────────────────
#  ROBLOX MUTUAL BLOCKER
#  Block nhau trong Roblox → không vào cùng server
# ─────────────────────────────────────────────
class RobloxMutualBlocker:
    """
    Các acc block lẫn nhau trong Roblox.
    Khi 2 user block nhau → Roblox KHÔNG cho vào cùng server instance.
    → Mỗi tab sẽ join server RIÊNG, không bị trùng.

    API được dùng:
    ─────────────────────────────────────────────────
    1. GET  https://users.roblox.com/v1/users/authenticated
       → Lấy {id, name} từ cookie

    2. Lấy CSRF token:
       POST https://auth.roblox.com/v2/logout  (không cần body)
       → 403 response có header: x-csrf-token: <token>

    3. Block user:
       POST https://apis.roblox.com/user-blocking-api/v1/users/{myId}/block-user
       Body: {"blockeeUserId": targetId}
       Headers: Cookie, x-csrf-token

    4. Check block list:
       GET https://apis.roblox.com/user-blocking-api/v1/users/{myId}/blocked-users
       → {"blockedUsers": [{id, name}, ...]}

    5. Unblock:
       DELETE https://apis.roblox.com/user-blocking-api/v1/users/{myId}/block-user/{targetId}
    ─────────────────────────────────────────────────
    """

    # Cache: {cookie: {"id": int, "name": str, "csrf": str}}
    _cache: dict = {}
    _lock = threading.Lock()

    ROBLOX_HEADERS = {
        "User-Agent"  : "Roblox/WinInet",
        "Accept"      : "application/json",
        "Content-Type": "application/json",
        "Referer"     : "https://www.roblox.com/",
        "Origin"      : "https://www.roblox.com",
    }

    @classmethod
    def _req(cls, method: str, url: str, cookie: str,
             csrf: str = "", body: dict = None) -> tuple:
        """
        Gọi Roblox API.
        Returns: (status_code, response_dict, headers)
        """
        headers = dict(cls.ROBLOX_HEADERS)
        headers["Cookie"] = f".ROBLOSECURITY={cookie}"
        if csrf:
            headers["x-csrf-token"] = csrf

        data = json.dumps(body).encode() if body else None

        try:
            req = urllib.request.Request(url, data=data,
                                         headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=12) as r:
                _status  = r.status
                _headers = dict(r.headers)
                _raw     = r.read()
                resp_body = json.loads(_raw) if _raw else {}
                return _status, resp_body, _headers
        except urllib.error.HTTPError as e:
            try:
                resp_body = json.loads(e.read())
            except:
                resp_body = {}
            return e.code, resp_body, dict(e.headers)
        except Exception as ex:
            log.debug(f"Roblox API error: {ex}")
            return -1, {}, {}

    @classmethod
    def _get_csrf(cls, cookie: str) -> str:
        """
        Roblox trả CSRF token qua header khi POST /logout → 403.
        """
        status, _, headers = cls._req(
            "POST",
            "https://auth.roblox.com/v2/logout",
            cookie
        )
        csrf = headers.get("x-csrf-token","") or headers.get("X-Csrf-Token","")
        return csrf

    @classmethod
    def _get_user_info(cls, cookie: str) -> dict:
        """
        Lấy {id, name} từ cookie. Dùng cache để tránh gọi API nhiều lần.
        """
        with cls._lock:
            cached = cls._cache.get(cookie)
            if cached and cached.get("id"):
                return cached

        status, data, _ = cls._req(
            "GET",
            "https://users.roblox.com/v1/users/authenticated",
            cookie
        )
        if status == 200 and data.get("id"):
            info = {
                "id"  : data["id"],
                "name": data.get("name","?"),
                "csrf": cls._get_csrf(cookie),
            }
            with cls._lock:
                cls._cache[cookie] = info
            return info

        log.debug(f"get_user_info failed (status={status})")
        return {}

    @classmethod
    def block_user(cls, my_cookie: str, target_id: int) -> bool:
        """Acc dùng my_cookie block user có id = target_id."""
        info = cls._get_user_info(my_cookie)
        if not info:
            return False
        my_id = info["id"]
        csrf  = info.get("csrf","") or cls._get_csrf(my_cookie)

        status, resp, new_headers = cls._req(
            "POST",
            f"https://apis.roblox.com/user-blocking-api/v1/users/{my_id}/block-user",
            my_cookie,
            csrf  = csrf,
            body  = {"blockeeUserId": target_id},
        )

        # CSRF expired → refresh và thử lại
        if status == 403 and new_headers.get("x-csrf-token"):
            new_csrf = new_headers["x-csrf-token"]
            with cls._lock:
                if my_cookie in cls._cache:
                    cls._cache[my_cookie]["csrf"] = new_csrf
            status, resp, _ = cls._req(
                "POST",
                f"https://apis.roblox.com/user-blocking-api/v1/users/{my_id}/block-user",
                my_cookie,
                csrf = new_csrf,
                body = {"blockeeUserId": target_id},
            )

        return status in (200, 204)

    @classmethod
    def unblock_user(cls, my_cookie: str, target_id: int) -> bool:
        """Gỡ block user có id = target_id."""
        info = cls._get_user_info(my_cookie)
        if not info:
            return False
        my_id = info["id"]
        csrf  = info.get("csrf","") or cls._get_csrf(my_cookie)

        status, _, new_headers = cls._req(
            "DELETE",
            f"https://apis.roblox.com/user-blocking-api/v1/users/{my_id}/block-user/{target_id}",
            my_cookie,
            csrf=csrf,
        )
        if status == 403 and new_headers.get("x-csrf-token"):
            new_csrf = new_headers["x-csrf-token"]
            status, _, _ = cls._req(
                "DELETE",
                f"https://apis.roblox.com/user-blocking-api/v1/users/{my_id}/block-user/{target_id}",
                my_cookie,
                csrf=new_csrf,
            )
        return status in (200, 204)

    @classmethod
    def get_blocked_list(cls, cookie: str) -> list:
        """Lấy danh sách user đang bị block bởi acc này."""
        info = cls._get_user_info(cookie)
        if not info:
            return []
        my_id = info["id"]
        status, data, _ = cls._req(
            "GET",
            f"https://apis.roblox.com/user-blocking-api/v1/users/{my_id}/blocked-users",
            cookie,
        )
        if status == 200:
            return data.get("blockedUsers", [])
        return []

    @classmethod
    def mutual_block_all(cls, show_progress=True) -> dict:
        """
        Lấy tất cả packages đang enabled, sau đó:
        - Lấy userId của từng acc qua cookie
        - Mỗi acc block TẤT CẢ acc còn lại
        Kết quả: {pkg: {"ok": int, "fail": int}}
        """
        pkgs = PackageManager.get_enabled()
        if len(pkgs) < 2:
            log.warn("Cần ít nhất 2 package để mutual block.")
            return {}

        print(f"\n{B}{'─'*60}{RST}")
        print(f"  {BOLD}{C}ROBLOX MUTUAL BLOCK{RST}")
        print(f"  {DIM}Mỗi acc sẽ block tất cả acc còn lại trong Roblox.{RST}")
        print(f"  {DIM}→ Roblox tự tách server, không vào trùng nhau.{RST}")
        print(f"{B}{'─'*60}{RST}\n")

        # Bước 1: Thu thập user info cho tất cả packages
        pkg_info = {}   # {pkg: {"cookie", "id", "name"}}

        print(f"  {C}[1/3] Đang lấy thông tin account...{RST}")
        for pkg, data in pkgs:
            cookie = data.get("cookie","") or CookieManager.read(pkg)
            if not cookie:
                log.warn(f"  [{pkg}] No cookie — skip.")
                continue
            info = cls._get_user_info(cookie)
            if not info or not info.get("id"):
                log.warn(f"  [{pkg}] Cannot get user info (cookie invalid?) — skip.")
                continue
            pkg_info[pkg] = {
                "cookie": cookie,
                "id"    : info["id"],
                "name"  : info["name"],
            }
            log.ok(f"  [{pkg}] {info['name']} (id={info['id']})")

        if len(pkg_info) < 2:
            log.warn("Không đủ acc hợp lệ để block nhau.")
            return {}

        print(f"\n  {C}[2/3] Đang block lẫn nhau ({len(pkg_info)} acc)...{RST}")
        results = {pkg: {"ok":0,"fail":0} for pkg in pkg_info}

        pkg_list = list(pkg_info.items())  # [(pkg, info), ...]

        for i, (my_pkg, my_info) in enumerate(pkg_list):
            for j, (tgt_pkg, tgt_info) in enumerate(pkg_list):
                if i == j:
                    continue  # không block chính mình

                target_id = tgt_info["id"]
                ok = cls.block_user(my_info["cookie"], target_id)

                if ok:
                    results[my_pkg]["ok"] += 1
                    if show_progress:
                        print(f"  {G}✔{RST} {my_info['name'][:15]} → block {tgt_info['name'][:15]}")
                else:
                    results[my_pkg]["fail"] += 1
                    if show_progress:
                        print(f"  {R}✘{RST} {my_info['name'][:15]} → block {tgt_info['name'][:15]} FAILED")

                # Delay nhỏ để tránh rate limit
                time.sleep(0.3)

        print(f"\n  {C}[3/3] Kết quả:{RST}")
        total_ok   = sum(r["ok"]   for r in results.values())
        total_fail = sum(r["fail"] for r in results.values())
        total_ops  = len(pkg_info) * (len(pkg_info) - 1)
        print(f"  {G}✅ {total_ok}/{total_ops} block thành công{RST}")
        if total_fail:
            print(f"  {R}✘  {total_fail} thất bại{RST}")

        print(f"\n{B}{'─'*60}{RST}")
        log.ok("Mutual block done — các acc sẽ không vào cùng server nữa.")
        return results

    @classmethod
    def unblock_all_mutual(cls) -> dict:
        """Gỡ block tất cả — dùng khi muốn reset."""
        pkgs = PackageManager.get_enabled()
        print(f"\n  {Y}Đang gỡ block tất cả...{RST}\n")

        results = {}
        pkg_info = {}

        for pkg, data in pkgs:
            cookie = data.get("cookie","") or CookieManager.read(pkg)
            if not cookie: continue
            info = cls._get_user_info(cookie)
            if not info: continue
            pkg_info[pkg] = {"cookie": cookie, "id": info["id"], "name": info["name"]}

        for pkg, info in pkg_info.items():
            blocked = cls.get_blocked_list(info["cookie"])
            ok = 0
            for user in blocked:
                if cls.unblock_user(info["cookie"], user["id"]):
                    ok += 1
                time.sleep(0.3)
            results[pkg] = ok
            log.ok(f"  [{pkg}] {info['name']} → gỡ {ok}/{len(blocked)} block")

        return results

    @classmethod
    def show_block_status(cls):
        """Hiển thị ai đang block ai."""
        pkgs = PackageManager.get_enabled()
        print(f"\n{B}{'─'*60}{RST}")
        print(f"  {BOLD}{C}BLOCK STATUS{RST}")
        print(f"{B}{'─'*60}{RST}\n")

        for pkg, data in pkgs:
            cookie = data.get("cookie","") or CookieManager.read(pkg)
            if not cookie: continue
            info = cls._get_user_info(cookie)
            if not info: continue
            blocked = cls.get_blocked_list(cookie)
            names = [u.get("name","?") for u in blocked[:10]]
            print(f"  {W}{info['name']}{RST} ({pkg})")
            if names:
                print(f"    {G}Đang block:{RST} {', '.join(names)}")
            else:
                print(f"    {DIM}Chưa block ai.{RST}")
            time.sleep(0.2)
        print(f"\n{B}{'─'*60}{RST}")


# ─────────────────────────────────────────────
#  EXECUTOR CHECKER
# ─────────────────────────────────────────────
class ExecutorChecker:
    @classmethod
    def is_pkg_running(cls, pkg):
        _, out, _ = sh(f"dumpsys activity activities | grep -i '{pkg}'", root=True)
        return pkg in out

    @classmethod
    def is_in_game(cls, pkg, timeout=180):
        deadline = time.time() + timeout
        log.info(f"[{pkg}] Waiting for game (timeout={timeout}s)...")
        while time.time() < deadline:
            _, out, _ = sh(f"dumpsys activity activities | grep -E '{pkg}'", root=True)
            if pkg in out:
                _, procs, _ = sh(f"ps -A | grep {pkg}", root=True)
                if len(procs.splitlines()) >= 2:
                    return True
            time.sleep(3)
        return False

    @classmethod
    def detect_issues(cls, pkg):
        _, out, _ = sh(f"dumpsys activity activities | grep -i '{pkg}'", root=True)
        if not out or pkg not in out:
            return "crash"
        _, logcat, _ = sh("logcat -d -t 50 2>/dev/null | tail -30", root=True)
        low = logcat.lower()
        if "captcha" in low and "locked" in low:
            return "captcha_lock"
        if "captcha" in low:
            return "captcha"
        if "faceid" in low or "face_id" in low or "face id" in low:
            return "faceid"
        return None

# ─────────────────────────────────────────────
#  TERMUX BOOT
# ─────────────────────────────────────────────
class TermuxBoot:
    @classmethod
    def create(cls):
        try:
            BOOT_FILE.parent.mkdir(parents=True, exist_ok=True)
            script = (
                "#!/data/data/com.termux/files/usr/bin/bash\n"
                "# Auto-generated by Roblox Tool (Axiom)\n"
                f"cd {BASE_DIR.parent}\n"
                "sleep 10\n"
                f"python3 {Path(__file__).resolve()} &\n"
            )
            BOOT_FILE.write_text(script)
            os.chmod(BOOT_FILE, 0o755)
            log.ok(f"Boot file: {BOOT_FILE}")
        except Exception as e:
            log.warn(f"Boot file creation failed: {e}")

    @classmethod
    def remove(cls):
        if BOOT_FILE.exists():
            BOOT_FILE.unlink()
            log.ok("Boot file removed.")

# ─────────────────────────────────────────────
#  TAB SORTER
# ─────────────────────────────────────────────
class TabSorter:
    @classmethod
    def sort(cls, pkgs):
        if not Config.get("sort_roblox_tabs"): return
        log.info("Sorting tabs...")
        tiny = Config.get("tiny_sort_tab")
        for pkg, _ in pkgs:
            if tiny:
                sh("wm size 360x640 2>/dev/null || true", root=True)
            sh("input keyevent KEYCODE_HOME 2>/dev/null || true", root=True)
            time.sleep(0.3)
        log.ok("Tabs sorted.")

# ─────────────────────────────────────────────
#  LIVE DASHBOARD
# ─────────────────────────────────────────────
class Dashboard:
    """
    Live terminal dashboard — refresh every 1 second during rejoin.
    Shows: config summary, CPU/RAM, per-package status table.
    """
    _stop   = threading.Event()
    _thread = None
    _lock   = threading.Lock()

    # {pkg: {"username": str, "status": str, "last_update": float, "rejoins": int}}
    _state: dict = {}

    # Status display strings
    STATUS_JOINED     = f"{G}✅ Joined{RST}"
    STATUS_REJOINING  = f"{Y}🔄 Rejoining{RST}"
    STATUS_KEY_WAIT   = f"{C}⏳ Waiting key{RST}"
    STATUS_KEY_BAD    = f"{R}🔑 Key invalid{RST}"
    STATUS_DEAD       = f"{R}💀 Script dead{RST}"
    STATUS_CRASHED    = f"{R}💥 Crashed{RST}"
    STATUS_CAPTCHA    = f"{Y}⚠️  Captcha{RST}"
    STATUS_FACEID     = f"{M}😐 FaceID{RST}"
    STATUS_CAPLOCK    = f"{R}🔒 Captcha lock{RST}"
    STATUS_STARTING   = f"{DIM}⏺  Starting...{RST}"

    @classmethod
    def init_pkg(cls, pkg):
        with cls._lock:
            if pkg not in cls._state:
                cls._state[pkg] = {
                    "username" : "",
                    "status"   : cls.STATUS_STARTING,
                    "last_update": time.time(),
                    "rejoins"  : 0,
                }

    @classmethod
    def update(cls, pkg, status, inc_rejoin=False):
        with cls._lock:
            if pkg not in cls._state:
                cls._state[pkg] = {"username":"","rejoins":0}
            cls._state[pkg]["status"]      = status
            cls._state[pkg]["last_update"] = time.time()
            if inc_rejoin:
                cls._state[pkg]["rejoins"] = cls._state[pkg].get("rejoins", 0) + 1

    @classmethod
    def set_username(cls, pkg, username):
        with cls._lock:
            if pkg not in cls._state:
                cls._state[pkg] = {"status": cls.STATUS_STARTING, "rejoins": 0}
            cls._state[pkg]["username"] = username

    @classmethod
    def _mask_username(cls, name: str) -> str:
        """Mask like: *****_ZN606 — keep last 6 chars, mask the rest."""
        if not name or name == "?":
            return f"{DIM}???{RST}"
        if len(name) <= 6:
            return name
        visible = name[-6:]
        masked  = "*" * min(len(name) - 6, 7)
        return f"{DIM}{masked}{RST}{W}{visible}{RST}"

    @classmethod
    def _get_cpu(cls) -> float:
        """Read CPU usage from /proc/stat — compare two snapshots."""
        def _read_stat():
            try:
                with open("/proc/stat") as f:
                    line = f.readline()
                vals = list(map(int, line.split()[1:]))
                idle = vals[3]
                total = sum(vals)
                return idle, total
            except:
                return 0, 1

        idle1, total1 = _read_stat()
        time.sleep(0.1)
        idle2, total2 = _read_stat()
        diff_idle  = idle2  - idle1
        diff_total = total2 - total1
        if diff_total == 0:
            return 0.0
        return round((1.0 - diff_idle / diff_total) * 100, 1)

    @classmethod
    def _get_ram(cls) -> tuple:
        """Read RAM from /proc/meminfo. Returns (used_mb, total_mb, pct)."""
        try:
            with open("/proc/meminfo") as f:
                lines = f.readlines()
            info = {}
            for l in lines:
                parts = l.split()
                if len(parts) >= 2:
                    info[parts[0].rstrip(":")] = int(parts[1])
            total    = info.get("MemTotal",    1)
            free     = info.get("MemFree",     0)
            buffers  = info.get("Buffers",     0)
            cached   = info.get("Cached",      0)
            sreclm   = info.get("SReclaimable",0)
            used_kb  = total - free - buffers - cached - sreclm
            used_mb  = used_kb  // 1024
            total_mb = total    // 1024
            pct      = round(used_kb / total * 100, 1)
            return used_mb, total_mb, pct
        except:
            return 0, 0, 0.0

    @classmethod
    def _fetch_username_from_api(cls, pkg) -> str:
        """Fetch Roblox username via API using stored cookie."""
        cookie = CookieManager.read(pkg)
        if not cookie:
            return ""
        try:
            req = urllib.request.Request(
                "https://users.roblox.com/v1/users/authenticated",
                headers={
                    "Cookie": f".ROBLOSECURITY={cookie}",
                    "User-Agent": "Roblox/WinInet",
                }
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read())
            return data.get("name", "")
        except:
            return ""

    @classmethod
    def _fetch_all_usernames(cls, pkgs):
        """Fetch usernames for all packages in parallel threads."""
        def _fetch(pkg):
            name = cls._fetch_username_from_api(pkg)
            if not name:
                # Fallback: try reading from stored account data
                cookie = CookieManager.read(pkg)
                for acc in AccountManager._accounts:
                    if acc.get("cookie","") == cookie and acc.get("name",""):
                        name = acc["name"]
                        break
            cls.set_username(pkg, name or "?")

        threads = [threading.Thread(target=_fetch, args=(pkg,), daemon=True)
                   for pkg in pkgs]
        for t in threads: t.start()
        # Don't join — let them complete in bg, dashboard will update live

    @classmethod
    def _ansi_len(cls, s: str) -> int:
        """Length of string excluding ANSI escape codes."""
        return len(re.sub(r'\033\[[0-9;]*m', '', s))

    @classmethod
    def _pad(cls, s: str, width: int, align="left") -> str:
        """Pad string to visual width, ignoring ANSI codes."""
        vis = cls._ansi_len(s)
        pad = max(0, width - vis)
        if align == "right":
            return " " * pad + s
        elif align == "center":
            lp = pad // 2
            rp = pad - lp
            return " " * lp + s + " " * rp
        return s + " " * pad

    @classmethod
    def _draw(cls):
        """Render the full dashboard to stdout."""
        try:
            tw = shutil.get_terminal_size((120, 40)).columns
        except:
            tw = 120
        tw = max(tw, 80)

        # ── Move cursor to top-left without clearing (avoids flicker) ──
        print("\033[H\033[J", end="", flush=True)

        now_str = datetime.now().strftime("%H:%M:%S")

        # ══════════════════════════════════════════════════════
        # TOP HEADER
        # ══════════════════════════════════════════════════════
        title = f" 🚀 ROBLOX AUTO REJOIN — LIVE DASHBOARD  [{now_str}] "
        pad   = max(0, tw - cls._ansi_len(title) - 2)
        print(f"{M}╭{'─'*(tw-2)}╮{RST}")
        print(f"{M}│{RST}{BOLD}{C}{title}{RST}{' '*pad}{M}│{RST}")

        # ── Config summary ──
        def cfg_row(label, key, fmt=None, true_str="Enable", false_str="Disable"):
            val = Config.get(key)
            if isinstance(val, bool):
                vs  = f"{G}{true_str}{RST}" if val else f"{R}{false_str}{RST}"
            elif fmt:
                vs = f"{W}{fmt(val)}{RST}"
            else:
                vs = f"{W}{val}{RST}"
            row = f" {DIM}{label}:{RST} {vs}"
            pad = max(0, tw - cls._ansi_len(row) - 4)
            print(f"{M}│{RST} {row}{' '*pad} {M}│{RST}")

        print(f"{M}├{'─'*(tw-2)}┤{RST}")
        cfg_row("Check executor method",    "account_check_method",    fmt=str)
        cfg_row("Change accounts",          "auto_change_bloxfruit")
        cfg_row("Change accounts custom",   "auto_change_custom")
        cfg_row("Change acc face id",       "auto_change_on_faceid")
        cfg_row("Change acc captcha lock",  "auto_change_on_captcha_lock")
        cfg_row("Check UI time",            "executor_check_timeout",  fmt=str)
        cfg_row("Auto block",               "auto_block_other_accounts")
        cfg_row("HWID Delta Mode",          "hwid_delta_mode",         fmt=str)
        cfg_row("Key check",                "key_check_enabled")
        cfg_row("Script monitor",           "execute_check_enabled")
        print(f"{M}╰{'─'*(tw-2)}╯{RST}")

        # ══════════════════════════════════════════════════════
        # CPU / RAM BAR
        # ══════════════════════════════════════════════════════
        cpu              = cls._get_cpu()
        used_mb, tot_mb, ram_pct = cls._get_ram()

        def _bar(pct, width=12):
            filled = int(pct / 100 * width)
            color  = G if pct < 60 else (Y if pct < 85 else R)
            return f"{color}{'█'*filled}{'░'*(width-filled)}{RST}"

        cpu_bar = _bar(cpu)
        ram_bar = _bar(ram_pct)
        mid_str = (f"  CPU {cpu_bar} {Y}{cpu:5.1f}%{RST}   "
                   f"RAM {ram_bar} {Y}{ram_pct:5.1f}%{RST}  "
                   f"{DIM}({used_mb}MB/{tot_mb}MB){RST}  ")
        vis_len  = cls._ansi_len(mid_str)
        total_pad = max(0, tw - vis_len)
        lpad = total_pad // 2
        rpad = total_pad - lpad
        print(f"{' '*lpad}{mid_str}{' '*rpad}")

        # ══════════════════════════════════════════════════════
        # PACKAGE TABLE
        # ══════════════════════════════════════════════════════
        COL_PKG  = 22
        COL_USER = 18
        COL_STAT = tw - COL_PKG - COL_USER - 10  # remaining

        def _hline(l, m, r, sep="─"):
            return (f"{M}{l}{sep*(COL_PKG+2)}{m}{sep*(COL_USER+2)}"
                    f"{m}{sep*(COL_STAT+2)}{r}{RST}")

        # Header
        print(_hline("╭", "┬", "╮"))
        h_pkg  = cls._pad(f"{BOLD}{C}Package{RST}",  COL_PKG,  "center")
        h_user = cls._pad(f"{BOLD}{C}Username{RST}", COL_USER, "center")
        h_stat = cls._pad(f"{BOLD}{C}Status{RST}",   COL_STAT, "center")
        print(f"{M}│{RST} {h_pkg} {M}│{RST} {h_user} {M}│{RST} {h_stat} {M}│{RST}")
        print(_hline("├", "┼", "┤"))

        with cls._lock:
            state_snap = dict(cls._state)

        if not state_snap:
            empty = cls._pad(f"{DIM}No packages running{RST}", tw - 4, "center")
            print(f"{M}│{RST} {empty} {M}│{RST}")
        else:
            for pkg, info in state_snap.items():
                # Package: truncate
                pkg_short = pkg if len(pkg) <= COL_PKG else pkg[:COL_PKG-2] + ".."
                pkg_cell  = cls._pad(f"{W}{pkg_short}{RST}", COL_PKG)

                # Username: masked
                raw_user  = info.get("username", "")
                user_cell = cls._pad(cls._mask_username(raw_user), COL_USER)

                # Status
                status     = info.get("status", cls.STATUS_STARTING)
                rejoins    = info.get("rejoins", 0)
                rej_str    = f" {DIM}(+{rejoins}){RST}" if rejoins else ""
                age        = time.time() - info.get("last_update", time.time())
                age_str    = f" {DIM}{age:.0f}s ago{RST}" if age > 2 else ""
                stat_full  = f"{status}{rej_str}{age_str}"
                stat_cell  = cls._pad(stat_full, COL_STAT)

                print(f"{M}│{RST} {pkg_cell} {M}│{RST} {user_cell} {M}│{RST} {stat_cell} {M}│{RST}")

        print(_hline("╰", "┴", "╯"))

        # footer
        total_pkgs   = len(state_snap)
        joined_count = sum(1 for s in state_snap.values()
                          if "Joined" in str(s.get("status","")))
        footer = (f"  {G}✅ {joined_count} joined{RST}  "
                  f"{DIM}/{RST}  "
                  f"{W}{total_pkgs} total{RST}  "
                  f"  {DIM}ENTER to stop{RST}")
        print(footer)

    @classmethod
    def _loop(cls):
        while not cls._stop.is_set():
            try:
                cls._draw()
            except Exception as e:
                pass  # never crash dashboard thread
            cls._stop.wait(timeout=1)

    @classmethod
    def start(cls, pkgs: list):
        cls._stop.clear()
        cls._state.clear()

        # Init all packages
        for pkg in pkgs:
            cls.init_pkg(pkg)

        # Fetch usernames in background
        cls._fetch_all_usernames(pkgs)

        cls._thread = threading.Thread(target=cls._loop, daemon=True,
                                       name="dashboard")
        cls._thread.start()

    @classmethod
    def stop(cls):
        cls._stop.set()
        if cls._thread:
            cls._thread.join(timeout=2)
        # Restore terminal
        print("\033[?25h", end="", flush=True)  # show cursor
        os.system("clear 2>/dev/null || cls 2>/dev/null || true")


# ─────────────────────────────────────────────
#  REJOIN ENGINE
# ─────────────────────────────────────────────
class RejoinEngine:
    _stop    = threading.Event()
    _threads = []

    @classmethod
    def _link_to_intent(cls, link):
        patterns = [
            r"roblox\.com/games/(\d+)",
            r"placeId=(\d+)",
            r"^(\d+)$",
        ]
        for pat in patterns:
            m = re.search(pat, link)
            if m:
                pid  = m.group(1)
                lc_m = re.search(r"privateServerLinkCode=([^&\s]+)", link)
                lc   = lc_m.group(1) if lc_m else ""
                if lc:
                    return f"roblox://placeId={pid}&linkType=Server&accessCode={lc}"
                return f"roblox://placeId={pid}"
        return link

    @classmethod
    def _launch(cls, pkg, link, bypass=False):
        delay      = Config.get("open_roblox_delay", 5)
        kill_first = Config.get("kill_before_open", True)

        if kill_first:
            sh(f"am force-stop {pkg}", root=True)
            kill_sleep = Config.get("sleep_after_kill", 0)
            time.sleep(kill_sleep if kill_sleep > 0 else 1)

        if bypass:
            # pm clear-cache doesn't exist on Android 8+ — use pm clear instead
            sh(f"pm clear {pkg}", root=True)
            time.sleep(random.uniform(0.5, 2.0))

        time.sleep(delay)

        if link:
            intent = cls._link_to_intent(link)
            # Correct syntax: am start -a VIEW -d <uri>
            # Do NOT append pkg name — it overrides intent resolution wrongly
            cmd = f"am start -a android.intent.action.VIEW -d '{intent}'"
        else:
            cmd = f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1"

        c, out, err = sh(cmd, root=True)
        if c == 0 or not err:
            log.ok(f"[{pkg}] Launched.")
            Webhook.monitor(f"🔄 Rejoining: `{pkg}`")
        else:
            log.error(f"[{pkg}] Launch failed: {err}")

    # Tracks which acc cookie each pkg is "reserved" to get (to prevent double-assign)
    _reserved: dict = {}   # {pkg: cookie_being_assigned}
    _res_lock = threading.Lock()

    @classmethod
    def _rotate_account(cls, pkg, old_cookie, reason, _cross_call=False):
        """
        Rotate account for pkg away from old_cookie.
        _cross_call=True means this is a forced rotation because another pkg blocked
        old_cookie — we don't block again (avoid double-block).

        Block chéo (cross-block):
        Khi pkg A block cookie X → check xem pkg nào khác đang dùng cookie X
        → force rotate chúng NGAY (không đợi vòng lặp tiếp theo).
        """
        if not _cross_call and Config.get("auto_block_other_accounts"):
            AccountManager.block(old_cookie)

        # Reserve a slot so other concurrent workers don't pick the same new acc
        with cls._res_lock:
            in_use_now = set(v for k, v in cls._reserved.items() if k != pkg)

        next_acc = AccountManager.get_next(old_cookie, exclude_cookies=in_use_now)
        if not next_acc:
            log.warn(f"[{pkg}] No next account available.")
            return True  # skip rejoin

        new_cookie = next_acc["cookie"]

        # Register reservation
        with cls._res_lock:
            cls._reserved[pkg] = new_cookie

        log.info(f"[{pkg}] Rotating account (reason={reason})")
        CookieManager.inject(pkg, new_cookie)
        AccountManager.confirm_rotation(new_cookie)

        # Clear reservation
        with cls._res_lock:
            cls._reserved.pop(pkg, None)

        Webhook.change(f"🔄 Account changed on `{pkg}` [{reason}]")

        # ── Block chéo: force rotate mọi pkg khác đang dùng old_cookie ──
        if not _cross_call and Config.get("auto_block_other_accounts"):
            affected = AccountManager.get_pkgs_using(old_cookie)
            for other_pkg in affected:
                if other_pkg == pkg:
                    continue
                other_cookie = CookieManager.read(other_pkg)
                if other_cookie == old_cookie:
                    log.warn(f"[cross-block] {other_pkg} is using blocked acc → force rotating")
                    Webhook.change(f"🔄 Cross-block: `{other_pkg}` forced off blocked acc")
                    cls._rotate_account(other_pkg, old_cookie, "cross-block",
                                        _cross_call=True)

        return False

    @classmethod
    def _handle_issue(cls, pkg, issue, current_cookie):
        if issue == "captcha":
            Webhook.monitor(f"⚠ Captcha: `{pkg}`")
            if Config.get("no_rejoin_on_captcha"):
                log.warn(f"[{pkg}] Captcha — skipping rejoin.")
                if Config.get("auto_change_on_captcha"):
                    return cls._rotate_account(pkg, current_cookie, "captcha")
                return True
            if Config.get("auto_change_on_captcha"):
                return cls._rotate_account(pkg, current_cookie, "captcha")

        elif issue == "faceid":
            Webhook.monitor(f"⚠ FaceID: `{pkg}`")
            if Config.get("no_rejoin_on_faceid"):
                log.warn(f"[{pkg}] FaceID — skipping rejoin.")
                if Config.get("auto_change_on_faceid"):
                    return cls._rotate_account(pkg, current_cookie, "faceid")
                return True
            if Config.get("auto_change_on_faceid"):
                return cls._rotate_account(pkg, current_cookie, "faceid")

        elif issue == "captcha_lock":
            Webhook.monitor(f"🔒 Captcha-lock: `{pkg}`")
            if Config.get("no_rejoin_on_captcha_lock"):
                log.warn(f"[{pkg}] Captcha-lock — skipping rejoin.")
                if Config.get("auto_change_on_captcha_lock"):
                    return cls._rotate_account(pkg, current_cookie, "captcha_lock")
                return True
            if Config.get("auto_change_on_captcha_lock"):
                return cls._rotate_account(pkg, current_cookie, "captcha_lock")

        elif issue == "crash":
            PackageManager.increment_crash(pkg)
            log.warn(f"[{pkg}] Crash detected.")
            Webhook.monitor(f"💥 Crash: `{pkg}`")
            if Config.get("kill_all_tabs_if_one_crash"):
                log.warn("Killing ALL tabs due to crash!")
                for p, _ in PackageManager.get_enabled():
                    sh(f"am force-stop {p}", root=True)

        return False  # proceed with rejoin

    @classmethod
    def _check_key_in_worker(cls, pkg: str, exe_type: str) -> bool:
        """
        Kiểm tra key executor còn hạn không.
        Trả về True = key OK (hoặc key check bị tắt).
        Trả về False = key invalid → cần rejoin/bypass trước.
        """
        if not Config.get("key_check_enabled", True):
            return True

        key = ExecutorKeyManager._read_key_auto(exe_type)
        if not key:
            key = Config.get("executor_key", "")

        if not key:
            log.warn(f"[{pkg}] No executor key found — rejoin để executor lấy key mới.")
            Webhook.monitor(f"🔑 No key: `{pkg}` — rejoining to re-fetch")
            return False  # trigger rejoin

        valid = ExecutorKeyManager.validate_key(key, exe_type)
        if valid:
            log.debug(f"[{pkg}] Key OK.")
            return True

        # Key hết hạn
        log.warn(f"[{pkg}] Key EXPIRED/INVALID.")
        Webhook.monitor(f"🔑 Key expired: `{pkg}`")

        # Thử bypass tự động
        if Config.get("key_bypass_enabled", False):
            log.info(f"[{pkg}] Auto-bypass key...")
            new_key = ExecutorKeyManager._attempt_bypass(exe_type, pkg)
            if new_key:
                ExecutorKeyManager.set_key_manual(new_key, exe_type)
                log.ok(f"[{pkg}] Key bypassed OK.")
                Webhook.monitor(f"🔑 Key bypassed: `{pkg}`")
                return True
            log.warn(f"[{pkg}] Bypass failed — rejoin để executor tự lấy key.")

        return False  # key invalid → rejoin

    @classmethod
    def _check_script_in_worker(cls, pkg: str) -> bool:
        """
        Kiểm tra script / executor còn chạy không.
        Trả về True = script alive (hoặc execute check bị tắt).
        Trả về False = script chết → cần rejoin.
        """
        if not Config.get("execute_check_enabled", True):
            return True

        r = ScriptMonitor.check_execute_status(pkg)

        if r["alive"]:
            log.debug(f"[{pkg}] Script alive via {r['method']}.")
            return True

        if r["crashed"]:
            log.warn(f"[{pkg}] Script/game CRASHED.")
            Webhook.monitor(f"💀 Script crashed: `{pkg}`")
        else:
            beat_age = ""
            if r["last_beat"]:
                age = time.time() - r["last_beat"]
                beat_age = f" (last beat {age:.0f}s ago)"
            log.warn(f"[{pkg}] Script DEAD{beat_age} — no signal from any method.")
            Webhook.monitor(f"💀 Script dead: `{pkg}`{beat_age}")

        return False  # script dead → rejoin

    @classmethod
    def _worker(cls, pkg, link, bypass,
                key_gate: threading.Event = None,
                is_first: bool = True):
        """
        key_gate  : threading.Event — tab 1 set() khi key valid, tab khác wait() trước khi chạy
        is_first  : True nếu đây là tab 1 (gate keeper)
        """
        interval       = Config.get("rejoin_interval", 5)
        max_retries    = Config.get("max_retries", 0)
        rotate_every   = Config.get("rotate_account_every", 0)
        exe_timeout    = Config.get("executor_check_timeout", 180)
        check_method   = Config.get("account_check_method", "executor")
        exe_type       = Config.get("executor_type", "delta")
        exec_check_iv  = Config.get("execute_check_interval", 30)
        retries        = 0
        last_rotate    = time.time()
        last_exe_check = time.time()
        last_key_check = 0             # force check ngay lần đầu
        KEY_CHECK_IV   = 300

        # Default gate nếu không truyền vào
        if key_gate is None:
            key_gate = threading.Event()
            key_gate.set()

        # ══════════════════════════════════════════════
        #  KEY GATE LOGIC
        # ══════════════════════════════════════════════
        if not is_first:
            # Tab phụ: chờ gate keeper (tab 1) set key_gate trước khi làm gì
            Dashboard.update(pkg, Dashboard.STATUS_KEY_WAIT)
            while not cls._stop.is_set():
                got = key_gate.wait(timeout=2)
                if got:
                    break
                Dashboard.update(pkg, Dashboard.STATUS_KEY_WAIT)
            if cls._stop.is_set():
                return

        log.info(f"[{pkg}] Worker ON | is_first={is_first} | bypass={bypass} | "
                 f"interval={interval}s | exe={exe_type}")

        while not cls._stop.is_set():
            # ── Non-first tabs: re-check key gate every iteration ──
            # If tab 1 clears gate mid-run (lost key), other tabs pause here.
            if not is_first and not key_gate.is_set():
                Dashboard.update(pkg, Dashboard.STATUS_KEY_WAIT)
                key_gate.wait(timeout=2)
                continue

            current_cookie = CookieManager.read(pkg)
            now = time.time()

            # ── 1. Scheduled account rotation ──
            if rotate_every > 0 and (now - last_rotate) >= rotate_every:
                log.info(f"[{pkg}] Scheduled account rotation...")
                cls._rotate_account(pkg, current_cookie, "scheduled")
                last_rotate = now

            # ── 2. Key check (throttled) ──
            if (now - last_key_check) >= KEY_CHECK_IV:
                key_ok = cls._check_key_in_worker(pkg, exe_type)
                last_key_check = now

                if not key_ok:
                    if is_first and key_gate.is_set():
                        key_gate.clear()
                        Webhook.monitor(f"🔑 Key lost — gate closed, other tabs paused")

                    Dashboard.update(pkg, Dashboard.STATUS_KEY_BAD)
                    cls._launch(pkg, link, bypass)
                    retries += 1
                    Dashboard.update(pkg, Dashboard.STATUS_REJOINING, inc_rejoin=True)
                    time.sleep(interval)
                    continue
                else:
                    if is_first and not key_gate.is_set():
                        key_gate.set()
                        Webhook.monitor(f"🔑 Key valid — gate opened, all tabs resuming")

            # ── 3. App running check ──
            running = ExecutorChecker.is_pkg_running(pkg)

            if not running:
                issue = ExecutorChecker.detect_issues(pkg)
                if issue:
                    # Map issue → dashboard status
                    _issue_map = {
                        "captcha"      : Dashboard.STATUS_CAPTCHA,
                        "faceid"       : Dashboard.STATUS_FACEID,
                        "captcha_lock" : Dashboard.STATUS_CAPLOCK,
                        "crash"        : Dashboard.STATUS_CRASHED,
                    }
                    Dashboard.update(pkg, _issue_map.get(issue, Dashboard.STATUS_CRASHED))
                    skip = cls._handle_issue(pkg, issue, current_cookie)
                    if skip:
                        time.sleep(interval)
                        continue

                Dashboard.update(pkg, Dashboard.STATUS_REJOINING, inc_rejoin=True)
                cls._launch(pkg, link, bypass)
                retries += 1

                if check_method == "executor":
                    in_game = ExecutorChecker.is_in_game(pkg, exe_timeout)
                    if in_game:
                        last_exe_check = time.time()
                        Dashboard.update(pkg, Dashboard.STATUS_JOINED)
                        if is_first and not key_gate.is_set():
                            key_gate.set()
                    else:
                        Dashboard.update(pkg, Dashboard.STATUS_REJOINING)
                else:
                    Dashboard.update(pkg, Dashboard.STATUS_JOINED)

            else:
                # ── 4. Script / execute check (throttled) ──
                if (now - last_exe_check) >= exec_check_iv:
                    script_ok = cls._check_script_in_worker(pkg)
                    last_exe_check = now

                    if not script_ok and Config.get("rejoin_on_dead_script", True):
                        if is_first and key_gate.is_set():
                            key_gate.clear()
                        Dashboard.update(pkg, Dashboard.STATUS_DEAD)
                        cls._launch(pkg, link, bypass)
                        retries += 1
                        Dashboard.update(pkg, Dashboard.STATUS_REJOINING, inc_rejoin=True)
                        time.sleep(interval)
                        continue
                else:
                    # App running, no check needed this tick — mark joined
                    Dashboard.update(pkg, Dashboard.STATUS_JOINED)

            # ── 5. Max retries guard ──
            if max_retries > 0 and retries >= max_retries:
                log.info(f"[{pkg}] Max retries ({max_retries}) reached. Stopping worker.")
                break

            time.sleep(interval)

        log.info(f"[{pkg}] Worker stopped.")

    @classmethod
    def start(cls, bypass=False):
        pkgs = PackageManager.get_enabled()
        if not pkgs:
            log.warn("No packages selected. Use option 3 or 5 first.")
            input(f"\n  {DIM}Press ENTER...{RST}")
            return

        cls._stop.clear()
        cls._threads.clear()

        # ── Key gate ──
        key_gate = threading.Event()
        if not Config.get("key_check_enabled", True) or len(pkgs) <= 1:
            key_gate.set()

        if Config.get("sort_roblox_tabs"):
            TabSorter.sort(pkgs)

        # ── Start live dashboard BEFORE workers ──
        pkg_names = [p for p, _ in pkgs]
        print("\033[?25l", end="", flush=True)  # hide cursor
        Dashboard.start(pkg_names)

        for i, (pkg, data) in enumerate(pkgs):
            link     = data.get("link", Config.get("default_link", ""))
            is_first = (i == 0)
            t = threading.Thread(
                target=cls._worker,
                args=(pkg, link, bypass, key_gate, is_first),
                daemon=True, name=f"rejoin-{pkg}"
            )
            t.start()
            cls._threads.append(t)
            time.sleep(0.2)

        # Wait for ENTER — dashboard is drawing, main thread just blocks here
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass
        cls.stop()

    @classmethod
    def stop(cls):
        cls._stop.set()
        Dashboard.stop()   # clears screen, shows cursor
        for t in cls._threads:
            t.join(timeout=3)
        log.ok("All workers stopped.")

# ─────────────────────────────────────────────
#  TAB OPENER
# ─────────────────────────────────────────────
class TabOpener:
    @classmethod
    def open_all(cls):
        pkgs = PackageManager.get_enabled()
        if not pkgs:
            log.warn("No packages selected."); return
        delay = Config.get("delay_between_tabs", 15)
        for i, (pkg, data) in enumerate(pkgs):
            link = data.get("link", Config.get("default_link", ""))
            log.info(f"[{i+1}/{len(pkgs)}] Opening: {pkg}")
            if link:
                intent = RejoinEngine._link_to_intent(link)
                sh(f"am start -a android.intent.action.VIEW -d '{intent}'", root=True)
            else:
                sh(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1", root=True)
            if i < len(pkgs) - 1:
                log.info(f"Waiting {delay}s...")
                time.sleep(delay)
        log.ok(f"Opened {len(pkgs)} tab(s).")

# ─────────────────────────────────────────────
#  CONFIG UI — 32 settings, tabular layout
# ─────────────────────────────────────────────
SETTINGS_LAYOUT = [
    ("ACCOUNT", [
        ("account_check_method",        "Account check method",         ["executor","cookie"],        "executor"),
        ("auto_change_bloxfruit",       "Auto change account (Blox Fruit)", "bool",                  False),
        ("auto_change_custom",          "Auto change account (Custom)", "bool",                       False),
        ("auto_change_on_captcha",      "Auto change on captcha",       "bool",                       False),
        ("no_rejoin_on_captcha",        "No rejoin when captcha",       "bool",                       True),
        ("no_rejoin_on_faceid",         "No rejoin when face ID",       "bool",                       True),
        ("auto_change_on_faceid",       "Auto change on face ID",       "bool",                       True),
        ("no_rejoin_on_captcha_lock",   "No rejoin when captcha lock",  "bool",                       True),
        ("auto_change_on_captcha_lock", "Auto change on captcha lock",  "bool",                       True),
        ("auto_block_other_accounts",   "Mutual block (Roblox in-app)",  "mutual_block",                True),
        ("new_account_source",          "New account source",           ["this_device","file"],       "this_device"),
        ("save_changed_accounts_to",    "Save changed accounts to",     ["this_device","file"],       "this_device"),
    ]),
    ("TABS / WINDOW", [
        ("sort_roblox_tabs",            "Sort Roblox tabs",             "bool",                       True),
        ("tiny_sort_tab",               "Tiny sort tab (smallest size)","bool",                       True),
        ("kill_all_tabs_if_one_crash",  "Kill all tabs if 1 crash",     "bool",                       False),
        ("kill_tab_when_captcha_solved","Kill tab when captcha solved",  "bool",                       False),
        ("auto_create_termux_boot",     "Auto-create Termux boot file", "bool",                       True),
    ]),
    ("TIMING", [
        ("rotate_account_every",        "Rotate account every (0=skip)","int",                        0),
        ("open_roblox_delay",           "Open Roblox delay (sec)",      "int",                        5),
        ("sleep_after_kill",            "Sleep after kill (0=skip)",    "int",                        0),
        ("executor_check_timeout",      "Executor check timeout (sec)", "int",                        180),
        ("delay_between_tabs",          "Delay between opening tabs",   "int",                        15),
    ]),
    ("MISC", [
        ("package_prefix",              "Package prefix",               "str",                        "ugphone"),
        ("captcha_solver_urls",         "Captcha solver URLs",          "list",                       []),
        ("zeropoint_api_key",           "ZeroPoint API Key (faceid)",   "str",                        ""),
        ("faceid_solver_urls",          "Face ID solver URLs",          "list",                       []),
        ("zeropoint_priority_queue",    "ZeroPoint Priority Queue",     "bool",                       False),
        ("codex_login_user",            "Codex login",                  "codex",                      ""),
        ("hwid_delta_mode",             "HWID Delta Mode",              ["auto","manual"],             "auto"),
        ("captchalock_solver_urls",     "Captcha-lock solver URLs",     "list",                       []),
    ]),
    ("WEBHOOK", [
        ("webhook_monitor_url",         "Webhook setup",                "webhook_group",               ""),
        ("discord_ping_target",         "Discord ping target",          "str",                        "@everyone"),
    ]),
]

def _display_val(key, val, staging=None):
    s = staging or Config._data
    if isinstance(val, bool):
        return "✔ Enabled" if val else "✘ Disabled"
    if isinstance(val, list):
        return ", ".join(val) if val else "(empty)"
    if val in (None, ""):
        return "not set"
    if key == "account_check_method":
        return "Check Executor (recommended)" if val == "executor" else "Cookie"
    if key in ("new_account_source", "save_changed_accounts_to"):
        return "This device" if val == "this_device" else "File"
    if key in ("rotate_account_every", "sleep_after_kill"):
        return "skip" if val == 0 else f"{val} sec"
    if key in ("open_roblox_delay", "executor_check_timeout", "delay_between_tabs"):
        return f"{val} sec"
    if key == "zeropoint_priority_queue":
        return "Disable" if not val else "Enable"
    if key == "hwid_delta_mode":
        return val.capitalize()
    if key == "codex_login_user":
        pw = s.get("codex_login_pass", "")
        u  = s.get("codex_login_user", "")
        return f"{u or 'not set'} / {pw or 'not set'}"
    if key == "webhook_monitor_url":
        mu = s.get("webhook_monitor_url", "")
        su = s.get("webhook_solver_url", "")
        cu = s.get("webhook_change_url", "")
        un = s.get("webhook_username", "tuat")
        iv = s.get("webhook_interval_min", 1)
        parts = [
            f"Monitor: {'set' if mu else 'off'}",
            f"Solver: {'set' if su else 'off'}",
            f"Change: {'set' if cu else 'off'}",
            str(un), f"{iv} min"
        ]
        return " | ".join(parts)
    if key == "auto_block_other_accounts":
        return "▶ Press to run mutual block"
    return str(val)

class ConfigUI:
    @classmethod
    def show(cls):
        # Build flat list with row numbers
        flat = []
        num = 0
        for section, items in SETTINGS_LAYOUT:
            flat.append(("__section__", section, None, None, None, None))
            for entry in items:
                num += 1
                flat.append(("__item__", entry[0], entry[1], entry[2], entry[3], num))

        staging = copy.deepcopy(Config._data)

        while True:
            os.system("clear")
            W1, W2, W3 = 6, 40, 57
            top  = f"{M}╭{'─'*W1}┬{'─'*W2}┬{'─'*W3}╮{RST}"
            mid  = f"{M}├{'─'*W1}┼{'─'*W2}┼{'─'*W3}┤{RST}"
            bot  = f"{M}╰{'─'*W1}┴{'─'*W2}┴{'─'*W3}╯{RST}"

            print(f"\n{top}")
            print(f"{M}│{RST}{Y}{'#':>{W1-1}} {RST}{M}│{RST}"
                  f" {W}{'Setting':<{W2-2}}{RST}{M}│{RST}"
                  f" {W}{'Current value':<{W3-2}}{RST}{M}│{RST}")
            print(mid)

            for row in flat:
                kind = row[0]
                if kind == "__section__":
                    sec = row[1]
                    print(f"{M}│{RST}{' '*W1}{M}│{RST}"
                          f" {C}{BOLD}{sec:<{W2-2}}{RST}{M}│{RST}"
                          f" {' '*(W3-2)}{M}│{RST}")
                    print(mid)
                else:
                    _, key, label, typ, default, n = row
                    val = staging.get(key, default)
                    dv  = _display_val(key, val, staging)
                    if len(dv) > W3 - 3:
                        dv = dv[:W3-6] + "..."
                    if "✔" in dv or dv == "set":
                        dv_c = f"{G}{dv}{RST}"
                    elif "✘" in dv or "Disabled" in dv or dv == "not set":
                        dv_c = f"{R}{dv}{RST}"
                    else:
                        dv_c = f"{W}{dv}{RST}"
                    print(f"{M}│{RST}{Y}{n:>{W1-1}} {RST}{M}│{RST}"
                          f" {W}{label:<{W2-2}}{RST}{M}│{RST}"
                          f" {dv_c:<{W3-2+len(dv_c)-len(dv)}}{M}│{RST}")

            print(mid)
            print(f"{M}│{RST}{Y}{'S':>{W1-1}} {RST}{M}│{RST}"
                  f" {G}{'Save & back to main menu':<{W2-2}}{RST}{M}│{RST}"
                  f" {' '*(W3-2)}{M}│{RST}")
            print(f"{M}│{RST}{Y}{'X':>{W1-1}} {RST}{M}│{RST}"
                  f" {R}{'Discard & back to main menu':<{W2-2}}{RST}{M}│{RST}"
                  f" {' '*(W3-2)}{M}│{RST}")
            print(bot)

            choice = input(f"\n{C}  Enter # to edit (S=save, X=discard): {RST}").strip().upper()

            if choice == "S":
                Config._data.update(staging)
                Config.commit()
                if staging.get("auto_create_termux_boot"):
                    TermuxBoot.create()
                else:
                    TermuxBoot.remove()
                log.ok("Config saved.")
                time.sleep(0.8)
                break

            elif choice == "X":
                log.info("Changes discarded.")
                break

            else:
                try:
                    target_n = int(choice)
                    match = None
                    for row in flat:
                        if row[0] == "__item__" and row[5] == target_n:
                            match = row; break
                    if not match:
                        log.warn("Invalid #"); time.sleep(0.5); continue

                    _, key, label, typ, default, _ = match
                    cur = staging.get(key, default)

                    print(f"\n  {C}Editing:{RST} {W}{label}{RST}")
                    print(f"  {DIM}Current: {_display_val(key, cur, staging)}{RST}")

                    if typ == "webhook_group":
                        print(f"\n  {C}Webhook setup:{RST}")
                        for wk, wl in [("webhook_monitor_url","Monitor URL"),
                                       ("webhook_solver_url","Solver URL"),
                                       ("webhook_change_url","Change URL"),
                                       ("webhook_username","Username"),
                                       ("webhook_interval_min","Interval (min)")]:
                            v = input(f"  {W}{wl}{RST} [{staging.get(wk,'')}]: ").strip()
                            if v:
                                if wk == "webhook_interval_min":
                                    try: staging[wk] = int(v)
                                    except: pass
                                else:
                                    staging[wk] = v

                    elif typ == "mutual_block":
                        # Không phải toggle — chạy action Roblox mutual block ngay
                        print(f"\n  {M}{'─'*50}{RST}")
                        print(f"  {BOLD}{C}ROBLOX MUTUAL BLOCK{RST}")
                        print(f"  {DIM}Các acc sẽ block nhau trong Roblox.{RST}")
                        print(f"  {DIM}Sau khi block → Roblox sẽ tách server cho từng acc.{RST}")
                        print(f"  {M}{'─'*50}{RST}")
                        print(f"\n  {Y}1.{RST} Block lẫn nhau (mutual block all)")
                        print(f"  {Y}2.{RST} Gỡ block tất cả")
                        print(f"  {Y}3.{RST} Xem trạng thái block hiện tại")
                        sub = input(f"\n  {C}Choice (1/2/3, blank=cancel): {RST}").strip()
                        if sub == "1":
                            RobloxMutualBlocker.mutual_block_all()
                            input(f"\n  {DIM}Press ENTER to return to config...{RST}")
                        elif sub == "2":
                            RobloxMutualBlocker.unblock_all_mutual()
                            input(f"\n  {DIM}Press ENTER to return to config...{RST}")
                        elif sub == "3":
                            RobloxMutualBlocker.show_block_status()
                            input(f"\n  {DIM}Press ENTER to return to config...{RST}")
                        # không thay đổi staging value — đây là action, không phải toggle
                        continue

                    elif typ == "codex":
                        u = input(f"  {W}Codex username: {RST}").strip()
                        p = input(f"  {W}Codex password: {RST}").strip()
                        if u: staging["codex_login_user"] = u
                        if p: staging["codex_login_pass"] = p

                    elif isinstance(typ, list):
                        for i, opt in enumerate(typ, 1):
                            print(f"  {Y}{i}.{RST} {opt}")
                        v = input(f"  Choice (#/name): ").strip()
                        try:
                            staging[key] = typ[int(v)-1]
                        except:
                            if v in typ: staging[key] = v

                    elif typ == "bool":
                        v = input(f"  Toggle (y/n): ").strip().lower()
                        staging[key] = v in ("y", "1", "yes", "true", "enable")

                    elif typ == "int":
                        v = input(f"  Value (number): ").strip()
                        try: staging[key] = int(v)
                        except: log.warn("Must be a number.")

                    elif typ == "list":
                        v = input(f"  URLs comma-separated (blank=clear): ").strip()
                        staging[key] = [x.strip() for x in v.split(",") if x.strip()] if v else []

                    else:
                        v = input(f"  New value: ").strip()
                        if v: staging[key] = v

                except (ValueError, IndexError):
                    log.warn("Invalid input."); time.sleep(0.5)

# ─────────────────────────────────────────────
#  EXECUTOR KEY MANAGER
#  Hỗ trợ: Delta X, Arceus X, Fluxus, Codex, Hydrogen, Custom
# ─────────────────────────────────────────────
class ExecutorKeyManager:
    """
    Cơ chế key của các executor Android phổ biến:
    ─────────────────────────────────────────────────────────
    1. DELTA X
       • Key lưu tại: shared_prefs/DeltaSettings.xml → <string name="key">
       • Check validity: POST https://delta-api.xyz/validate {key, hwid}
       • Key expire: thường 24h, bypass qua lootlabs checkpoint skip
       • HWID: android_id + /proc/cpuinfo fingerprint

    2. ARCEUS X
       • Key lưu tại: shared_prefs/arceus_prefs.xml → <string name="user_key">
       • Check validity: GET https://arceus-x.eu/api/check?key=KEY
       • Bypass: lootlabs.org task skip endpoint

    3. FLUXUS
       • Key lưu tại: /sdcard/Fluxus/key.txt (readable thường)
       • Check validity: GET https://fluxteam.net/api/checkKey?key=KEY
       • Expire: 1 day

    4. CODEX
       • Dùng username/password login thay vì key
       • Endpoint: POST https://codex-api.com/auth {user, pass}
       • Token lưu tại shared_prefs/CodexPrefs.xml

    5. HYDROGEN
       • Key lưu: /sdcard/Hydrogen/key.txt
       • Check: GET https://hydrogen.lol/api/key?key=KEY

    6. CUSTOM (tự điền API)
    ─────────────────────────────────────────────────────────
    """

    EXECUTOR_PROFILES = {
        "delta": {
            "prefs_pkg"    : "com.delta.executor",
            "prefs_file"   : "DeltaSettings.xml",
            "prefs_key"    : "key",
            "sdcard_path"  : None,
            "validate_url" : "https://delta-api.xyz/validate",
            "validate_method": "POST",
            "validate_payload": lambda k, hwid: json.dumps({"key": k, "hwid": hwid}),
            "valid_field"  : "valid",
        },
        "arceus": {
            "prefs_pkg"    : "com.arceus.x",
            "prefs_file"   : "arceus_prefs.xml",
            "prefs_key"    : "user_key",
            "sdcard_path"  : None,
            "validate_url" : "https://arceus-x.eu/api/check",
            "validate_method": "GET",
            "validate_payload": None,
            "valid_field"  : "valid",
        },
        "fluxus": {
            "prefs_pkg"    : "com.fluxteam.fluxus",
            "prefs_file"   : None,
            "sdcard_path"  : "/sdcard/Fluxus/key.txt",
            "validate_url" : "https://fluxteam.net/api/checkKey",
            "validate_method": "GET",
            "validate_payload": None,
            "valid_field"  : "status",
        },
        "hydrogen": {
            "prefs_pkg"    : "com.hydrogen.lol",
            "prefs_file"   : None,
            "sdcard_path"  : "/sdcard/Hydrogen/key.txt",
            "validate_url" : "https://hydrogen.lol/api/key",
            "validate_method": "GET",
            "validate_payload": None,
            "valid_field"  : "success",
        },
        "codex": {
            "prefs_pkg"    : "com.codex.executor",
            "prefs_file"   : "CodexPrefs.xml",
            "prefs_key"    : "auth_token",
            "sdcard_path"  : None,
            "validate_url" : "https://codex-api.com/auth",
            "validate_method": "POST",
            "validate_payload": lambda k, hwid: json.dumps({
                "user": Config.get("codex_login_user",""),
                "pass": Config.get("codex_login_pass",""),
            }),
            "valid_field"  : "success",
        },
    }

    @classmethod
    def _get_hwid(cls) -> str:
        """Tạo HWID từ android_id + cpu info (giống cách executor build)."""
        android_id = AndroidIDManager.get()
        _, cpu, _  = sh("cat /proc/cpuinfo 2>/dev/null | grep Serial | head -1", root=True)
        raw = f"{android_id}:{cpu}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    @classmethod
    def _read_key_from_prefs(cls, profile: dict) -> str:
        """Đọc key từ shared_prefs của executor APK (root required)."""
        pkg  = profile.get("prefs_pkg","")
        file = profile.get("prefs_file","")
        pkey = profile.get("prefs_key","key")
        if not pkg or not file:
            return ""
        path = f"/data/data/{pkg}/shared_prefs/{file}"
        _, out, _ = sh(f"cat '{path}' 2>/dev/null", root=True)
        if not out:
            return ""
        # Parse XML: <string name="KEY">VALUE</string>
        m = re.search(rf'name="{re.escape(pkey)}"[^>]*>([^<]+)<', out)
        return m.group(1).strip() if m else ""

    @classmethod
    def _read_key_from_sdcard(cls, path: str) -> str:
        """Đọc key từ file trực tiếp trên sdcard."""
        _, out, _ = sh(f"cat '{path}' 2>/dev/null", root=True)
        return out.strip()

    @classmethod
    def _read_key_auto(cls, exe_type: str) -> str:
        """Tự động tìm key từ đúng vị trí."""
        profile = cls.EXECUTOR_PROFILES.get(exe_type)
        if not profile:
            # Custom: dùng key từ config
            return Config.get("executor_key","")
        if profile.get("sdcard_path"):
            return cls._read_key_from_sdcard(profile["sdcard_path"])
        return cls._read_key_from_prefs(profile)

    @classmethod
    def validate_key(cls, key: str, exe_type: str) -> bool:
        """Gọi API để validate key còn hạn hay không."""
        profile = cls.EXECUTOR_PROFILES.get(exe_type)
        if not profile:
            log.warn("Custom executor — skipping API validate.")
            return bool(key)

        url    = profile.get("validate_url","")
        method = profile.get("validate_method","GET")
        field  = profile.get("valid_field","valid")
        hwid   = cls._get_hwid()

        if not url:
            return bool(key)

        try:
            if method == "POST":
                payload_fn = profile.get("validate_payload")
                payload = payload_fn(key, hwid).encode() if payload_fn else b""
                req = urllib.request.Request(
                    url, data=payload,
                    headers={"Content-Type": "application/json",
                             "User-Agent": "Dalvik/2.1.0"},
                    method="POST"
                )
            else:
                sep = "&" if "?" in url else "?"
                req = urllib.request.Request(
                    f"{url}{sep}key={urllib.parse.quote(key)}&hwid={hwid}",
                    headers={"User-Agent": "Dalvik/2.1.0"}
                )

            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())

            result = data.get(field, False)
            if isinstance(result, str):
                result = result.lower() in ("true","ok","success","1","valid")
            return bool(result)

        except Exception as e:
            log.warn(f"Key validate error ({exe_type}): {e}")
            return False  # treat as invalid on error

    @classmethod
    def check_all_packages(cls) -> dict:
        """
        Check key cho tất cả packages đã chọn.
        Returns: {pkg: {"key": str, "valid": bool, "source": str}}
        """
        exe_type = Config.get("executor_type","delta")
        results  = {}
        pkgs     = PackageManager.get_enabled()

        if not pkgs:
            log.warn("No packages selected.")
            return {}

        print(f"\n{B}{'─'*60}{RST}")
        print(f"  {BOLD}{C}KEY CHECK — executor: {exe_type.upper()}{RST}")
        print(f"{B}{'─'*60}{RST}\n")

        for pkg, _ in pkgs:
            # 1. Thử đọc key từ executor storage
            profile = cls.EXECUTOR_PROFILES.get(exe_type,{})
            # Swap prefs_pkg với actual roblox clone pkg
            cloned_profile = dict(profile)
            # Đối với clone packages, executor thường inject vào roblox pkg data
            # Key vẫn lưu ở executor pkg, nhưng cần verify per roblox pkg
            key    = cls._read_key_auto(exe_type)
            source = "auto-detected"

            # Nếu không tìm được, dùng key từ config
            if not key:
                key    = Config.get("executor_key","")
                source = "config"

            if not key:
                log.warn(f"  [{pkg}] No key found!")
                results[pkg] = {"key":"","valid":False,"source":"none"}
                print(f"  {R}✘{RST} {W}{pkg}{RST} — {R}NO KEY{RST}")
                continue

            key_short = key[:12] + "..." if len(key) > 12 else key

            # 2. Validate key qua API
            log.info(f"  [{pkg}] Validating key: {key_short}")
            valid = cls.validate_key(key, exe_type)

            results[pkg] = {"key": key, "valid": valid, "source": source}

            if valid:
                print(f"  {G}✔{RST} {W}{pkg}{RST} — key {G}VALID{RST} ({DIM}{key_short}{RST})")
            else:
                print(f"  {R}✘{RST} {W}{pkg}{RST} — key {R}INVALID/EXPIRED{RST} ({DIM}{key_short}{RST})")
                if Config.get("key_bypass_enabled"):
                    log.info(f"  [{pkg}] Attempting key bypass...")
                    new_key = cls._attempt_bypass(exe_type, pkg)
                    if new_key:
                        results[pkg]["key"]   = new_key
                        results[pkg]["valid"]  = True
                        results[pkg]["source"] = "bypass"
                        Config.set("executor_key", new_key)
                        Config.commit()
                        print(f"    {G}↳ Bypass OK! New key: {new_key[:16]}...{RST}")

        print(f"\n{B}{'─'*60}{RST}")
        ok  = sum(1 for v in results.values() if v["valid"])
        bad = len(results) - ok
        print(f"  {G}{ok} valid{RST}  |  {R}{bad} invalid{RST}  |  total {len(results)}")
        print(f"{B}{'─'*60}{RST}")
        return results

    @classmethod
    def _attempt_bypass(cls, exe_type: str, pkg: str) -> str:
        """
        Bypass key heuristic theo từng executor.
        Delta: checkpoint skip via known bypass endpoints
        Fluxus/Hydrogen: sdcard key file spoof
        """
        bypass_endpoints = {
            "delta"   : "https://delta-api.xyz/bypass",
            "fluxus"  : "https://fluxteam.net/api/bypass",
            "hydrogen": "https://hydrogen.lol/api/bypass",
            "arceus"  : "https://arceus-x.eu/api/bypass",
        }
        url = bypass_endpoints.get(exe_type,"")
        if not url:
            return ""
        hwid = cls._get_hwid()
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps({"hwid": hwid, "package": pkg}).encode(),
                headers={"Content-Type":"application/json",
                         "User-Agent":"Dalvik/2.1.0"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
            return data.get("key","")
        except Exception as e:
            log.debug(f"Bypass error: {e}")
            return ""

    @classmethod
    def set_key_manual(cls, key: str, exe_type: str):
        """Lưu key thủ công vào config + thử ghi vào executor storage."""
        Config.set("executor_key", key)
        Config.commit()

        profile = cls.EXECUTOR_PROFILES.get(exe_type,{})
        sdcard  = profile.get("sdcard_path","")
        if sdcard:
            sh(f"mkdir -p '{os.path.dirname(sdcard)}' 2>/dev/null || true", root=True)
            sh(f"echo '{key}' > '{sdcard}'", root=True)
            log.ok(f"Key written to {sdcard}")

        prefs_pkg  = profile.get("prefs_pkg","")
        prefs_file = profile.get("prefs_file","")
        prefs_key  = profile.get("prefs_key","key")
        if prefs_pkg and prefs_file:
            sp = f"/data/data/{prefs_pkg}/shared_prefs"
            sh(f"mkdir -p {sp}", root=True)
            xml = (
                "<?xml version='1.0' encoding='utf-8' standalone='yes' ?>\n"
                "<map>\n"
                f"    <string name=\"{prefs_key}\">{key}</string>\n"
                "</map>"
            )
            tmp = "/data/local/tmp/_exkey.xml"
            sh(f"printf '%s' '{xml}' > {tmp}", root=True)
            sh(f"cp {tmp} {sp}/{prefs_file}", root=True)
            sh(f"chmod 660 {sp}/{prefs_file}", root=True)
            sh(f"chown {prefs_pkg}:{prefs_pkg} {sp}/{prefs_file} 2>/dev/null || true", root=True)
            sh(f"rm -f {tmp}", root=True)
            log.ok(f"Key written to {prefs_pkg} shared_prefs.")


# ─────────────────────────────────────────────
#  SCRIPT / EXECUTOR CHECK ENGINE
#  Cơ chế heartbeat + logcat + process inspect
# ─────────────────────────────────────────────
class ScriptMonitor:
    """
    Cách executor inject & script alive detection:
    ────────────────────────────────────────────────────────
    Heartbeat file method (phổ biến nhất):
      • Lua script viết timestamp vào file mỗi N giây:
            while task.wait(30) do
                writefile("rbx_heartbeat.txt", tostring(os.time()))
            end
      • Tool đọc file, nếu timestamp cũ hơn threshold → script chết

    Logcat method:
      • Executor log injection success: "Execution successful", "[Delta] Injected"
      • Roblox crash: "FATAL EXCEPTION", "Process: com.roblox"
      • Script error: "LuaError", "Script execution failed"

    Process method:
      • Executor attach process: ptrace vào roblox process
      • Check: `ps -A | grep ptrace` or `cat /proc/<pid>/status | grep TracerPid`
      • Nếu TracerPid != 0 → executor đang inject

    Socket method:
      • Một số executor tạo socket: /data/local/tmp/executor.sock
      • Tool check socket tồn tại → executor running
    ────────────────────────────────────────────────────────
    """

    _monitor_thread = None
    _stop_monitor   = threading.Event()
    _status: dict   = {}   # {pkg: {"alive": bool, "last_beat": float, "method": str}}

    # Logcat tags của các executor phổ biến
    EXECUTOR_LOG_TAGS = {
        "delta"   : ["Delta", "DeltaX", "DeltaExecutor"],
        "arceus"  : ["ArceusX", "Arceus"],
        "fluxus"  : ["Fluxus", "FluxTeam"],
        "hydrogen": ["Hydrogen"],
        "codex"   : ["Codex", "CodexExecutor"],
    }

    INJECT_SUCCESS_PATTERNS = [
        r"[Ii]njection.{0,10}success",
        r"[Ee]xecution.{0,10}success",
        r"\[Delta\].{0,20}[Ii]njected",
        r"\[Arceus\].{0,20}ready",
        r"Script.{0,10}loaded",
        r"[Ll]ua.{0,10}execut",
        r"[Ii]njected.{0,10}successfully",
    ]

    CRASH_PATTERNS = [
        r"FATAL EXCEPTION",
        r"Process.*died",
        r"ANR in",
        r"force.close",
        r"Signal 11",     # SIGSEGV
        r"Signal 6",      # SIGABRT
    ]

    @classmethod
    def _get_roblox_pid(cls, pkg: str) -> str:
        """Lấy PID của process Roblox."""
        _, out, _ = sh(f"pidof {pkg} 2>/dev/null || ps -A | grep {pkg} | awk '{{print $1}}' | head -1", root=True)
        return out.strip().split()[0] if out.strip() else ""

    @classmethod
    def _check_heartbeat(cls, pkg: str) -> tuple:
        """
        Kiểm tra heartbeat file bằng cách đọc file modification time (mtime).
        Lua chỉ cần ghi bất kỳ gì vào file — Python check mtime.

        Không dùng content (os.time() trong Roblox ≠ Unix timestamp).

        Returns: (alive: bool, last_beat: float)
        """
        hb_file   = Config.get("heartbeat_file", "/sdcard/rbx_heartbeat.txt")
        timeout   = Config.get("heartbeat_timeout", 60)
        pkg_suffix = pkg.replace(".", "_").replace(" ", "_")
        pkg_hb    = hb_file.replace(".txt", f"_{pkg_suffix}.txt")

        # Chỉ check pkg-specific file — không fallback về shared file
        # (shared file bị tất cả tab ghi vào, sẽ luôn mới → false positive)
        _, out, _ = sh(f"stat -c '%Y' '{pkg_hb}' 2>/dev/null", root=True)
        if out.strip():
            try:
                mtime = float(out.strip())
                age   = time.time() - mtime
                return (age < timeout), mtime
            except (ValueError, TypeError):
                pass

        # Fallback: thử đọc content nếu file tồn tại và có timestamp hợp lệ
        _, content, _ = sh(f"cat '{pkg_hb}' 2>/dev/null", root=True)
        if content.strip():
            try:
                ts  = float(content.strip())
                # Nếu ts trông giống Unix timestamp (> năm 2020)
                if ts > 1_580_000_000:
                    age = time.time() - ts
                    return (age < timeout), ts
            except (ValueError, TypeError):
                pass

        return False, 0.0

    @classmethod
    def _check_logcat(cls, pkg: str, exe_type: str) -> tuple:
        """
        Check logcat gần nhất cho inject success / crash.
        Returns: (injected: bool, crashed: bool)
        """
        tags = cls.EXECUTOR_LOG_TAGS.get(exe_type, [exe_type])
        tag_filter = " ".join(f"-s {t}:V" for t in tags)

        # Lấy 100 dòng logcat gần nhất
        _, out, _ = sh(f"logcat -d -t 100 {tag_filter} 2>/dev/null", root=True)
        if not out:
            # Fallback: tất cả logcat filtered by package
            _, out, _ = sh(f"logcat -d -t 200 --pid=$(pidof {pkg} 2>/dev/null) 2>/dev/null | tail -100", root=True)

        low = out.lower()

        injected = any(re.search(p, out, re.IGNORECASE) for p in cls.INJECT_SUCCESS_PATTERNS)
        crashed  = any(re.search(p, out, re.IGNORECASE) for p in cls.CRASH_PATTERNS)

        return injected, crashed

    @classmethod
    def _check_ptrace(cls, pkg: str) -> bool:
        """
        Kiểm tra executor có đang attach vào Roblox qua ptrace không.
        TracerPid != 0 → đang bị inject.
        """
        pid = cls._get_roblox_pid(pkg)
        if not pid:
            return False
        _, out, _ = sh(f"cat /proc/{pid}/status 2>/dev/null | grep TracerPid", root=True)
        m = re.search(r"TracerPid:\s*(\d+)", out)
        if m:
            return int(m.group(1)) != 0
        return False

    @classmethod
    def _check_executor_socket(cls, exe_type: str) -> bool:
        """Check nếu executor socket tồn tại."""
        sockets = {
            "delta"   : ["/data/local/tmp/delta.sock", "/data/local/tmp/dx.pipe"],
            "arceus"  : ["/data/local/tmp/arceus.sock"],
            "fluxus"  : ["/data/local/tmp/fluxus.sock"],
            "hydrogen": ["/data/local/tmp/hydrogen.sock"],
            "codex"   : ["/data/local/tmp/codex.sock"],
        }
        paths = sockets.get(exe_type, [])
        for p in paths:
            c, _, _ = sh(f"test -e '{p}'", root=True)
            if c == 0:
                return True
        return False

    @classmethod
    def check_execute_status(cls, pkg: str) -> dict:
        """
        Full check: heartbeat + logcat + ptrace + socket.
        Returns detailed status dict.
        """
        exe_type = Config.get("executor_type","delta")
        results  = {
            "pkg"       : pkg,
            "alive"     : False,
            "method"    : "none",
            "heartbeat" : False,
            "logcat_ok" : False,
            "ptrace_ok" : False,
            "socket_ok" : False,
            "crashed"   : False,
            "last_beat" : 0.0,
        }

        # 1. Heartbeat check (most reliable if script supports it)
        hb_alive, last_beat = cls._check_heartbeat(pkg)
        results["heartbeat"] = hb_alive
        results["last_beat"] = last_beat
        if hb_alive:
            results["alive"]  = True
            results["method"] = "heartbeat"

        # 2. Logcat check
        injected, crashed = cls._check_logcat(pkg, exe_type)
        results["logcat_ok"] = injected
        results["crashed"]   = crashed
        if injected and not results["alive"]:
            results["alive"]  = True
            results["method"] = "logcat"

        # 3. ptrace check
        pt = cls._check_ptrace(pkg)
        results["ptrace_ok"] = pt
        if pt and not results["alive"]:
            results["alive"]  = True
            results["method"] = "ptrace"

        # 4. Socket check
        sock = cls._check_executor_socket(exe_type)
        results["socket_ok"] = sock
        if sock and not results["alive"]:
            results["alive"]  = True
            results["method"] = "socket"

        # Crashed overrides alive
        if crashed:
            results["alive"] = False

        cls._status[pkg] = {
            "alive"    : results["alive"],
            "last_beat": last_beat,
            "method"   : results["method"],
        }
        return results

    @classmethod
    def check_all_packages(cls) -> dict:
        """Check execute status cho tất cả packages đã chọn."""
        pkgs = PackageManager.get_enabled()
        if not pkgs:
            log.warn("No packages selected.")
            return {}

        print(f"\n{B}{'─'*64}{RST}")
        print(f"  {BOLD}{C}EXECUTE CHECK{RST}")
        print(f"  {DIM}heartbeat · logcat · ptrace · socket{RST}")
        print(f"{B}{'─'*64}{RST}\n")

        all_results = {}
        for pkg, _ in pkgs:
            log.info(f"Checking: {pkg}")
            r = cls.check_execute_status(pkg)
            all_results[pkg] = r

            alive_str = f"{G}✔ ALIVE{RST}" if r["alive"] else f"{R}✘ DEAD{RST}"
            methods   = []
            if r["heartbeat"]: methods.append(f"{G}HB{RST}")
            if r["logcat_ok"]: methods.append(f"{C}LOG{RST}")
            if r["ptrace_ok"]: methods.append(f"{Y}PTRACE{RST}")
            if r["socket_ok"]: methods.append(f"{M}SOCK{RST}")
            if r["crashed"]  : methods.append(f"{R}CRASH{RST}")
            method_str = " ".join(methods) if methods else f"{DIM}no signal{RST}"

            beat_str = ""
            if r["last_beat"]:
                age = time.time() - r["last_beat"]
                beat_str = f" | beat {age:.0f}s ago"

            print(f"  {W}{pkg}{RST}")
            print(f"    Status : {alive_str}  via [{method_str}]{beat_str}")

            if not r["alive"] and Config.get("rejoin_on_dead_script"):
                print(f"    {Y}↳ Script dead — queuing rejoin...{RST}")
                Webhook.monitor(f"💀 Script dead: `{pkg}` — rejoining")

        print(f"\n{B}{'─'*64}{RST}")
        dead = [p for p, r in all_results.items() if not r["alive"]]
        ok   = len(all_results) - len(dead)
        print(f"  {G}{ok} alive{RST}  |  {R}{len(dead)} dead{RST}  |  total {len(all_results)}")
        print(f"{B}{'─'*64}{RST}")

        if dead and Config.get("rejoin_on_dead_script"):
            print(f"\n  {Y}Rejoining dead packages in 3s...{RST}")
            time.sleep(3)
            for pkg in dead:
                data = PackageManager._pkgs.get(pkg, {})
                link = data.get("link", Config.get("default_link",""))
                RejoinEngine._launch(pkg, link)

        return all_results

    @classmethod
    def start_background_monitor(cls):
        """
        Background thread liên tục check execute status.
        Nếu script chết → tự rejoin.
        """
        if cls._monitor_thread and cls._monitor_thread.is_alive():
            log.warn("Monitor already running.")
            return

        cls._stop_monitor.clear()

        def _loop():
            interval = Config.get("execute_check_interval", 30)
            log.ok(f"Script monitor started (interval={interval}s)")
            while not cls._stop_monitor.is_set():
                time.sleep(interval)
                if cls._stop_monitor.is_set():
                    break
                pkgs = PackageManager.get_enabled()
                for pkg, data in pkgs:
                    r = cls.check_execute_status(pkg)
                    if not r["alive"] and Config.get("rejoin_on_dead_script"):
                        log.warn(f"[Monitor] Script dead: {pkg} — auto rejoin!")
                        link = data.get("link", Config.get("default_link",""))
                        RejoinEngine._launch(pkg, link)
                        Webhook.monitor(f"💀 Script dead → rejoined: `{pkg}`")
            log.info("Script monitor stopped.")

        cls._monitor_thread = threading.Thread(target=_loop, daemon=True,
                                               name="script-monitor")
        cls._monitor_thread.start()
        log.ok("Background script monitor ON.")

    @classmethod
    def stop_background_monitor(cls):
        cls._stop_monitor.set()
        log.ok("Script monitor stopping...")

    @classmethod
    def get_heartbeat_lua(cls, pkg: str = "") -> str:
        """
        Trả về Lua script snippet cần thêm vào script của user.

        Quan trọng:
        - Mỗi package có file heartbeat RIÊNG (dùng pkg suffix)
        - Dùng file mtime làm clock thay vì os.time() (os.time() trong Roblox
          trả uptime của game, không phải Unix timestamp)
        - Python check sẽ đọc os.clock() của file thay vì content
        """
        hb_file  = Config.get("heartbeat_file", "/sdcard/rbx_heartbeat.txt")
        interval = max(Config.get("heartbeat_timeout", 60) // 3, 10)

        # Tạo tên file riêng cho pkg này
        if pkg:
            pkg_suffix = pkg.replace(".", "_").replace(" ", "_")
            hb_file = hb_file.replace(".txt", f"_{pkg_suffix}.txt")

        return f"""\
-- ════════════════════════════════════════
-- AXIOM HEARTBEAT — paste vào đầu script
-- File: {hb_file}
-- Interval: {interval}s
-- ════════════════════════════════════════
local _HB_FILE     = "{hb_file}"
local _HB_INTERVAL = {interval}

-- Ghi "1" vào file mỗi {interval}s
-- Python tool check file modification time, KHÔNG check content
-- → không cần Unix timestamp, chỉ cần file được cập nhật
task.spawn(function()
    while true do
        pcall(function()
            writefile(_HB_FILE, "1")
        end)
        task.wait(_HB_INTERVAL)
    end
end)
-- ════════════════════════════════════════
"""


# ─────────────────────────────────────────────
#  EXECUTE ALL — gửi script tới tất cả packages
# ─────────────────────────────────────────────
class ScriptExecutor:
    """
    Execute script trên tất cả Roblox package đang chạy.

    Cơ chế (theo executor type):
    ─────────────────────────────────────────────
    1. File drop: Copy script vào executor script folder
       Delta:   /sdcard/Delta/scripts/autorun/
       Arceus:  /sdcard/ArceusX/autorun/
       Fluxus:  /sdcard/Fluxus/autoexec/
       Hydrogen:/sdcard/Hydrogen/autoexec/

    2. HTTP API (nếu executor có local HTTP server):
       Một số executor expose: http://127.0.0.1:PORT/execute
       POST body: {"script": "lua code here"}

    3. Socket write:
       echo "LUA_CODE" > /data/local/tmp/executor.sock

    4. Intent launch:
       am start với extras chứa script
    ─────────────────────────────────────────────
    """

    AUTORUN_DIRS = {
        "delta"   : ["/sdcard/Delta/scripts/autorun/",
                     "/sdcard/Delta/autorun/"],
        "arceus"  : ["/sdcard/ArceusX/autorun/",
                     "/sdcard/Arceus X/autorun/"],
        "fluxus"  : ["/sdcard/Fluxus/autoexec/",
                     "/sdcard/Fluxus/scripts/autoexec/"],
        "hydrogen": ["/sdcard/Hydrogen/autoexec/",
                     "/sdcard/Hydrogen/scripts/"],
        "codex"   : ["/sdcard/Codex/autoexec/",
                     "/sdcard/Codex/scripts/autorun/"],
    }

    HTTP_PORTS = {
        "delta"   : 7331,
        "arceus"  : 7335,
        "fluxus"  : 7332,
        "hydrogen": 7333,
        "codex"   : 7334,
    }

    @classmethod
    def _write_to_autorun(cls, script: str, exe_type: str) -> bool:
        """Drop script vào autorun folder của executor."""
        dirs = cls.AUTORUN_DIRS.get(exe_type, [])
        for d in dirs:
            sh(f"mkdir -p '{d}'", root=True)
            fname = f"{d}axiom_script_{int(time.time())}.lua"
            # Write via tmp to handle special chars
            tmp = "/data/local/tmp/_axiom_script.lua"
            # Escape single quotes in script
            escaped = script.replace("'", "'\\''")
            sh(f"printf '%s' '{escaped}' > '{tmp}'", root=True)
            c, _, _ = sh(f"cp '{tmp}' '{fname}'", root=True)
            sh(f"rm -f '{tmp}'", root=True)
            if c == 0:
                log.ok(f"Script dropped: {fname}")
                return True
        return False

    @classmethod
    def _execute_via_http(cls, script: str, exe_type: str, host="127.0.0.1") -> bool:
        """Gửi script qua local HTTP server của executor."""
        port = cls.HTTP_PORTS.get(exe_type)
        if not port:
            return False
        try:
            payload = json.dumps({"script": script}).encode()
            req = urllib.request.Request(
                f"http://{host}:{port}/execute",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status == 200
        except Exception as e:
            log.debug(f"HTTP execute error (port {port}): {e}")
            return False

    @classmethod
    def _execute_via_socket(cls, script: str, exe_type: str) -> bool:
        """Write script vào executor socket."""
        sockets = {
            "delta"   : "/data/local/tmp/delta.sock",
            "arceus"  : "/data/local/tmp/arceus.sock",
            "fluxus"  : "/data/local/tmp/fluxus.sock",
            "hydrogen": "/data/local/tmp/hydrogen.sock",
            "codex"   : "/data/local/tmp/codex.sock",
        }
        sock_path = sockets.get(exe_type,"")
        if not sock_path:
            return False
        c, _, _ = sh(f"test -e '{sock_path}'", root=True)
        if c != 0:
            return False
        try:
            import socket as _sock
            s = _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM)
            s.connect(sock_path)
            s.sendall((script + "\n\0").encode())
            s.close()
            return True
        except Exception as e:
            log.debug(f"Socket execute error: {e}")
            return False

    @classmethod
    def execute_on_package(cls, pkg: str, script: str) -> bool:
        """
        Thử execute script trên 1 package, lần lượt các method.
        """
        exe_type = Config.get("executor_type","delta")
        log.info(f"[{pkg}] Executing script via {exe_type}...")

        # Method 1: HTTP (nhanh nhất nếu available)
        if cls._execute_via_http(script, exe_type):
            log.ok(f"[{pkg}] Script executed via HTTP.")
            return True

        # Method 2: Unix socket
        if cls._execute_via_socket(script, exe_type):
            log.ok(f"[{pkg}] Script executed via socket.")
            return True

        # Method 3: Autorun file drop (requires app restart)
        if cls._write_to_autorun(script, exe_type):
            log.ok(f"[{pkg}] Script written to autorun.")
            return True

        log.warn(f"[{pkg}] All execute methods failed.")
        return False

    @classmethod
    def execute_all(cls, script: str = "") -> dict:
        """Execute script trên tất cả enabled packages."""
        if not script:
            script_path = Config.get("script_path","")
            if script_path:
                try:
                    with open(script_path) as f:
                        script = f.read()
                    log.ok(f"Loaded script from: {script_path}")
                except Exception as e:
                    log.error(f"Cannot read script file: {e}")
                    return {}
            else:
                log.warn("No script provided and no script_path in config.")
                return {}

        pkgs    = PackageManager.get_enabled()
        results = {}

        print(f"\n{B}{'─'*60}{RST}")
        print(f"  {BOLD}{C}EXECUTE ALL — {len(pkgs)} package(s){RST}")
        print(f"  {DIM}Script: {script[:60]}...{RST}")
        print(f"{B}{'─'*60}{RST}\n")

        for pkg, _ in pkgs:
            ok = cls.execute_on_package(pkg, script)
            results[pkg] = ok
            status = f"{G}✔ OK{RST}" if ok else f"{R}✘ FAIL{RST}"
            print(f"  {W}{pkg}{RST} — {status}")

        ok_count = sum(1 for v in results.values() if v)
        print(f"\n  Executed: {G}{ok_count}{RST}/{len(results)}")
        print(f"{B}{'─'*60}{RST}")
        return results


# ─────────────────────────────────────────────
#  MENU
# ─────────────────────────────────────────────
def draw_header():
    os.system("clear")
    print(f"{M}{BOLD}")
    print("  ██████╗  ██████╗ ██████╗ ██╗      ██████╗ ██╗  ██╗")
    print("  ██╔══██╗██╔════╝ ██╔══██╗██║     ██╔═══██╗╚██╗██╔╝")
    print("  ██████╔╝██║  ███╗██████╔╝██║     ██║   ██║ ╚███╔╝ ")
    print("  ██╔══██╗██║   ██║██╔══██╗██║     ██║   ██║ ██╔██╗ ")
    print("  ██║  ██║╚██████╔╝██████╔╝███████╗╚██████╔╝██╔╝ ██╗")
    print("  ╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚══════╝ ╚═════╝ ╚═╝  ╚═╝")
    print(f"{RST}{DIM}  Roblox Auto Rejoin — Termux Root | by Axiom v2.0{RST}\n")

def draw_menu():
    print(f"""\
{M}╭───────┬───────────────────────────────────────────────╮{RST}
{M}│{RST}{C}{BOLD}       │ ── Auto Rejoin ──                             {M}│{RST}
{M}│{RST}{Y}     1 {M}│{RST} Start auto rejoin                             {M}│{RST}
{M}│{RST}{Y}     2 {M}│{RST} Start auto rejoin with bypass                 {M}│{RST}
{M}├───────┼───────────────────────────────────────────────┤{RST}
{M}│{RST}{C}{BOLD}       │ ── Server Setup ──                            {M}│{RST}
{M}│{RST}{Y}     3 {M}│{RST} Select packages & assign server link          {M}│{RST}
{M}│{RST}{Y}     4 {M}│{RST} List selected packages                        {M}│{RST}
{M}│{RST}{Y}     5 {M}│{RST} Auto-select all Roblox packages with one link {M}│{RST}
{M}├───────┼───────────────────────────────────────────────┤{RST}
{M}│{RST}{C}{BOLD}       │ ── Tabs ──                                    {M}│{RST}
{M}│{RST}{Y}     6 {M}│{RST} Open all Roblox tabs                          {M}│{RST}
{M}├───────┼───────────────────────────────────────────────┤{RST}
{M}│{RST}{C}{BOLD}       │ ── Account / Cookie ──                        {M}│{RST}
{M}│{RST}{Y}     7 {M}│{RST} Login via cookie                              {M}│{RST}
{M}│{RST}{Y}     8 {M}│{RST} Logout Roblox                                 {M}│{RST}
{M}│{RST}{Y}     9 {M}│{RST} Fix login cookie (copy from existing package) {M}│{RST}
{M}│{RST}{Y}    10 {M}│{RST} Export cookies from packages                  {M}│{RST}
{M}├───────┼───────────────────────────────────────────────┤{RST}
{M}│{RST}{C}{BOLD}       │ ── System ──                                  {M}│{RST}
{M}│{RST}{Y}    11 {M}│{RST} Set Android ID                                {M}│{RST}
{M}│{RST}{Y}    12 {M}│{RST} Download APK from GoFile                      {M}│{RST}
{M}├───────┼───────────────────────────────────────────────┤{RST}
{M}│{RST}{C}{BOLD}       │ ── Executor ──                                {M}│{RST}
{M}│{RST}{Y}    15 {M}│{RST} Check key (all packages)                      {M}│{RST}
{M}│{RST}{Y}    16 {M}│{RST} Check execute / script alive                  {M}│{RST}
{M}│{RST}{Y}    17 {M}│{RST} Execute script on all tabs                    {M}│{RST}
{M}├───────┼───────────────────────────────────────────────┤{RST}
{M}│{RST}{Y}    13 {M}│{RST} ⚙  Configuration                              {M}│{RST}
{M}│{RST}{Y}    14 {M}│{RST} Exit                                          {M}│{RST}
{M}╰───────┴───────────────────────────────────────────────╯{RST}
""")

def pick_pkg(prompt="Select package"):
    pkgs = PackageManager.get_installed_roblox()
    if not pkgs:
        pkgs = PackageManager.get_all_packages()
    print(f"\n{C}  Packages:{RST}")
    for i, p in enumerate(pkgs, 1):
        print(f"  {Y}{i}.{RST} {p}")
    v = input(f"  {C}{prompt} (#/name): {RST}").strip()
    try:
        idx = int(v) - 1
        if 0 <= idx < len(pkgs):
            return pkgs[idx]
    except:
        pass
    return v

def pause():
    input(f"\n  {DIM}Press ENTER to continue...{RST}")

# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
def main():
    if not SelfChecker.run_all():
        print(f"\n{R}  Cannot start — fix errors above.{RST}\n")
        sys.exit(1)

    Config.load()
    PackageManager.load()
    AccountManager.load()

    if Config.get("auto_create_termux_boot"):
        TermuxBoot.create()

    def _sig(sig, frame):
        print(f"\n{Y}  Interrupted — shutting down...{RST}")
        RejoinEngine.stop()
        sys.exit(0)
    signal.signal(signal.SIGINT,  _sig)
    signal.signal(signal.SIGTERM, _sig)

    while True:
        draw_header()
        draw_menu()
        choice = input(f"{C}Enter choice: {RST}").strip()

        if choice == "1":
            RejoinEngine.start(bypass=False)

        elif choice == "2":
            RejoinEngine.start(bypass=True)

        elif choice == "3":
            pkg  = pick_pkg()
            link = input(f"  {C}Server link / placeId: {RST}").strip()
            PackageManager.select(pkg, link)
            pause()

        elif choice == "4":
            pkgs = PackageManager.list_selected()
            print(f"\n{B}{'─'*64}{RST}")
            if not pkgs:
                print(f"  {Y}No packages selected yet.{RST}")
            for pkg, data in pkgs.items():
                en = data.get("enabled", True)
                c  = data.get("cookie", "")
                cs = (c[:30]+"...") if c else "none"
                cr = data.get("crash_count", 0)
                _state = f"{G}on{RST}" if en else f"{R}off{RST}"
                print(f"  {BOLD}{W}{pkg}{RST} [{_state}]")
                print(f"    Link    : {C}{data.get('link','—')}{RST}")
                print(f"    Cookie  : {DIM}{cs}{RST}")
                print(f"    Crashes : {Y}{cr}{RST}")
            print(f"{B}{'─'*64}{RST}")
            pause()

        elif choice == "5":
            link = input(f"  {C}Server link for ALL packages: {RST}").strip()
            n = PackageManager.auto_select_all(link)
            log.ok(f"Selected {n} package(s).")
            pause()

        elif choice == "6":
            TabOpener.open_all()
            pause()

        elif choice == "7":
            pkg = pick_pkg()
            cookie = input(f"  {C}Paste .ROBLOSECURITY cookie: {RST}").strip()
            CookieManager.inject(pkg, cookie)
            AccountManager.add(cookie, pkg)
            pause()

        elif choice == "8":
            pkg = pick_pkg("Select package to logout")
            if input(f"  {R}Confirm logout {pkg}? (y/n): {RST}").strip().lower() == "y":
                CookieManager.logout(pkg)
            pause()

        elif choice == "9":
            print(f"\n  {C}Source (working cookie):{RST}")
            src = pick_pkg("Source")
            print(f"\n  {C}Target (needs fix):{RST}")
            tgt = pick_pkg("Target")
            CookieManager.fix_from(src, tgt)
            pause()

        elif choice == "10":
            result = CookieManager.export_all()
            for pkg, c in result.items():
                print(f"  {W}{pkg}{RST}: {DIM}{c[:40]}...{RST}")
            print(f"  {C}Export dir: {COOKIE_DIR}{RST}")
            pause()

        elif choice == "11":
            cur = AndroidIDManager.get()
            print(f"\n  Current: {Y}{cur}{RST}")
            print(f"  {DIM}1. Enter custom  2. Generate random{RST}")
            sub = input(f"  {C}Choice: {RST}").strip()
            if sub == "1":
                nid = input(f"  {C}New Android ID (16 hex): {RST}").strip()
                AndroidIDManager.set(nid)
            elif sub == "2":
                nid = AndroidIDManager.random()
                print(f"  Generated: {G}{nid}{RST}")
                if input(f"  Apply? (y/n): ").strip().lower() == "y":
                    AndroidIDManager.set(nid)
            pause()

        elif choice == "12":
            url   = input(f"  {C}GoFile URL: {RST}").strip()
            token = Config.get("gofile_token", "")
            if not token:
                token = input(f"  {C}GoFile token (blank=public): {RST}").strip()
                if token:
                    Config.set("gofile_token", token); Config.commit()
            path = GoFileDownloader.download(url, token)
            if path and input(f"  Install APK? (y/n): ").strip().lower() == "y":
                GoFileDownloader.install(path)
            pause()

        elif choice == "13":
            ConfigUI.show()

        elif choice == "15":
            # ── Check Key ──
            exe_type = Config.get("executor_type","delta")
            print(f"\n  {C}Executor type: {Y}{exe_type}{RST}")
            print(f"  {DIM}1. Check all packages  2. Set key manually  3. Show heartbeat Lua snippet{RST}")
            sub = input(f"  {C}Choice: {RST}").strip()
            if sub == "1":
                ExecutorKeyManager.check_all_packages()
            elif sub == "2":
                key = input(f"  {C}Paste key: {RST}").strip()
                if key:
                    ExecutorKeyManager.set_key_manual(key, exe_type)
                    log.ok("Key set manually.")
            elif sub == "3":
                # Show valid key from config / auto-detected
                key = ExecutorKeyManager._read_key_auto(exe_type) or Config.get("executor_key","")
                if key:
                    short = key[:20] + "..." if len(key) > 20 else key
                    print(f"\n  {G}Stored key: {W}{short}{RST}")
                    valid = ExecutorKeyManager.validate_key(key, exe_type)
                    print(f"  Status: {G+'VALID'+RST if valid else R+'INVALID/EXPIRED'+RST}")
                else:
                    print(f"  {R}No key stored.{RST}")
            pause()

        elif choice == "16":
            # ── Check Execute / Script Alive ──
            print(f"\n  {DIM}1. Check all packages now  2. Start background monitor  3. Stop monitor  4. Show heartbeat Lua{RST}")
            sub = input(f"  {C}Choice: {RST}").strip()
            if sub == "1":
                ScriptMonitor.check_all_packages()
            elif sub == "2":
                ScriptMonitor.start_background_monitor()
                print(f"\n  {G}Background monitor started — checking every {Config.get('execute_check_interval',30)}s{RST}")
                print(f"  {DIM}Script dead → auto rejoin will fire.{RST}")
            elif sub == "3":
                ScriptMonitor.stop_background_monitor()
            elif sub == "4":
                pkgs = PackageManager.get_enabled()
                if pkgs:
                    print(f"\n  {C}Chọn package để tạo heartbeat snippet:{RST}")
                    for i, (p, _) in enumerate(pkgs, 1):
                        print(f"  {Y}{i}.{RST} {p}")
                    pi = input(f"  {C}# (Enter=all/generic): {RST}").strip()
                    try:
                        sel_pkg = pkgs[int(pi)-1][0]
                    except:
                        sel_pkg = ""
                else:
                    sel_pkg = ""
                lua = ScriptMonitor.get_heartbeat_lua(sel_pkg)
                print(f"\n{C}{'─'*60}{RST}")
                print(lua)
                print(f"{C}{'─'*60}{RST}")
                if sel_pkg:
                    print(f"  {DIM}File riêng cho: {sel_pkg}{RST}")
                print(f"  {DIM}Copy snippet trên vào đầu Lua script của bạn.{RST}")
            pause()

        elif choice == "17":
            # ── Execute Script on All Tabs ──
            script_path = Config.get("script_path","")
            print(f"\n  {DIM}Script path in config: {script_path or 'not set'}{RST}")
            print(f"  {DIM}1. Execute from config script_path  2. Paste script inline  3. Set script path{RST}")
            sub = input(f"  {C}Choice: {RST}").strip()
            if sub == "1":
                if not script_path:
                    log.warn("No script_path set in config (option 13).")
                else:
                    ScriptExecutor.execute_all()
            elif sub == "2":
                print(f"  {C}Paste Lua script (type END on new line when done):{RST}")
                lines = []
                while True:
                    l = input()
                    if l.strip().upper() == "END":
                        break
                    lines.append(l)
                script = "\n".join(lines)
                if script.strip():
                    ScriptExecutor.execute_all(script)
                else:
                    log.warn("Empty script.")
            elif sub == "3":
                p = input(f"  {C}Script file path (.lua): {RST}").strip()
                if p:
                    Config.set("script_path", p)
                    Config.commit()
                    log.ok(f"Script path set: {p}")
            pause()



        elif choice in ("14", "q", "exit", "quit"):
            print(f"\n{G}{BOLD}  later boss man.{RST}\n")
            sys.exit(0)

        else:
            log.warn(f"Unknown: '{choice}'")
            time.sleep(0.6)

if __name__ == "__main__":
    main()
