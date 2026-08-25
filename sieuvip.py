import subprocess
import urllib.parse
from typing import Optional, Tuple


def launch_roblox_direct(
    package: str,
    place_id: str,
    link_code: Optional[str] = None
) -> Tuple[bool, str]:
    """
    Kích hoạt đưa tài khoản vào thẳng Server VIP hoặc Place ID.
    
    Tham số:
    - package: Tên gói ứng dụng (ví dụ: com.roblox.client).
    - place_id: ID của map game (ví dụ Blox Fruits: 2753915549).
    - link_code: Mã link server riêng (nếu có).
    """
    # 1. Dừng app cũ để giải phóng RAM và làm mới trạng thái
    subprocess.run(["su", "-c", f"am force-stop {package}"], capture_output=True)
    
    # 2. Xây dựng đường dẫn Intent
    params = {"placeId": str(place_id)}
    if link_code:
        params["linkCode"] = str(link_code)
    
    query = urllib.parse.urlencode(params)
    deep_link = f"roblox://experiences/start?{query}"
    
    # 3. Kích hoạt Intent bằng quyền Root
    cmd = (
        f"am start -W "
        f"-a android.intent.action.VIEW "
        f"-d '{deep_link}' "
        f"-p {package}"
    )
    
    res = subprocess.run(["su", "-c", cmd], capture_output=True, text=True)
    if res.returncode == 0 and "error" not in res.stdout.lower():
        return True, "Khởi chạy vào Server thành công"
    return False, f"Lỗi: {res.stdout or res.stderr}"
