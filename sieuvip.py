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
