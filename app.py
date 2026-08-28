import os
import time
import random
import uuid
import json
import threading
import urllib.parse
import requests
from flask import Flask, request, jsonify, session

app = Flask(__name__)
app.secret_key = os.environ.get(
    "FLASK_SECRET_KEY",
    str(uuid.uuid4())
)

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
RENDER_API_KEY = os.environ.get("RENDER_API_KEY", "")
RENDER_SERVICE_ID = os.environ.get("RENDER_SERVICE_ID", "")

BASE_URL = "https://atfminers.asloni.online/miner/index.php"

COMMON_HEADERS_TEMPLATE = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
    "Content-Type": "application/json",
    "Origin": "https://atfminers.asloni.online",
    "Referer": "https://atfminers.asloni.online/miner/index.html?v=1784716383&entry=openapp",
    "X-Requested-With": "XMLHttpRequest",
    "Sec-Ch-Ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

MIN_INTERVAL = 8.3
MAX_INTERVAL = 9.0
USAGE_WARN_RATIO = 0.75
USAGE_DANGER_RATIO = 0.9
COOLDOWN_SECONDS = 120

# Khoảng cách giữa các lần chủ động login lại (không phải do lỗi, mà làm mới định kỳ)
LOGIN_REFRESH_MIN = 7 * 60   # 7 phút
LOGIN_REFRESH_MAX = 10 * 60  # 10 phút

# Danh sách 4 task
TASK_LIST = [
    "youtube_like_comment",
    "twitter_retweet",
    "website_visit",
    "telegram_react_latest"
]

# Cấu hình runtime (cho phép hot reload không cần restart Render)
runtime_config = {
    "INIT_DATA": os.environ.get("INIT_DATA", "").strip(),
    "DEVICE_ID": os.environ.get("DEVICE_ID", "").strip(),
    "TMA_SESSION": ""  # Lưu session token trong RAM
}
config_lock = threading.Lock()

# Trạng thái bot, để lộ ra qua route "/" cho dễ theo dõi từ xa
bot_state = {
    "started_at": time.time(),
    "last_loop": None,
    "last_status": "starting",
    "pending_reward": None,
    "vong_lap": 0,
    "rate_per_sec": 0.0,
    "request_count": None,
    "threshold_requests": None,
    "ban_level": 0,
    "last_login_refresh": None,
    "is_running": False,
    "task_status": "Chưa chạy",
}
state_lock = threading.Lock()
bot_thread = None
stop_event = threading.Event()

def get_device_id():
    """Lấy device_id từ runtime_config hoặc biến môi trường."""
    with config_lock:
        if runtime_config["DEVICE_ID"]:
            return runtime_config["DEVICE_ID"]
    dev_id = os.environ.get("DEVICE_ID", "").strip()
    if dev_id:
        return dev_id
    new_dev = f"dev-{uuid.uuid4()}"
    with config_lock:
        runtime_config["DEVICE_ID"] = new_dev
    return new_dev


def get_init_data():
    """Lấy initData từ runtime_config hoặc biến môi trường INIT_DATA."""
    with config_lock:
        if runtime_config["INIT_DATA"]:
            return runtime_config["INIT_DATA"]
    return os.environ.get("INIT_DATA", "").strip()

def get_tma_session():
    with config_lock:
        return runtime_config["TMA_SESSION"]

def set_tma_session(session_token):
    with config_lock:
        runtime_config["TMA_SESSION"] = session_token

def is_admin():
    return session.get("admin") is True

def parse_tg_id(init_data: str):
    parsed = urllib.parse.parse_qs(init_data)
    user_raw = parsed.get("user", [None])[0]
    if not user_raw:
        return None
    try:
        user_obj = json.loads(user_raw)
        return user_obj.get("id")
    except Exception:
        return None


def compute_adaptive_wait(abuse: dict, base_wait: float) -> float:
    threshold_requests = abuse.get("threshold_requests") or 0
    threshold_seconds = abuse.get("threshold_seconds") or 0
    request_count = abuse.get("request_count") or 0
    active_seconds = abuse.get("active_seconds") or 0

    if not threshold_requests or not threshold_seconds:
        return base_wait

    usage_ratio = request_count / threshold_requests
    time_ratio = (active_seconds / threshold_seconds) if threshold_seconds else 0
    overrun = usage_ratio - time_ratio

    if usage_ratio >= USAGE_DANGER_RATIO or overrun >= 0.15:
        print(f"  Gan cham nguong abuse (usage={usage_ratio:.2%}) -> nghi {COOLDOWN_SECONDS}s")
        return COOLDOWN_SECONDS
    elif usage_ratio >= USAGE_WARN_RATIO or overrun >= 0.08:
        extra = base_wait * 0.5
        print(f"  Usage ratio={usage_ratio:.2%} hoi cao -> gian them {extra:.1f}s")
        return base_wait + extra

    return base_wait


def login(session, init_data, device_id, tg_id):
    headers = dict(COMMON_HEADERS_TEMPLATE)
    headers["X-Telegram-Init-Data"] = init_data
    body = {
        "device_id": device_id,
        "initData": init_data,
        "request_id": str(uuid.uuid4()),
        "tg_id": str(tg_id) if tg_id is not None else "",
        "username": "",
    }
    params = {"action": "login", "t": str(int(time.time() * 1000))}
    resp = session.post(BASE_URL, headers=headers, params=params, json=body, timeout=10)
    print(f"[LOGIN] status={resp.status_code}")
    if resp.status_code != 200:
        print(f"  body={resp.text[:300]}")
        return False, None
    data = resp.json()

    # Bổ sung lưu TMA Session token khi Login thành công
    tma_token = resp.headers.get("x-atf-tma-session") or data.get("session_token") or data.get("tma_session")
    if tma_token:
        set_tma_session(tma_token)

    try:
        pending_reward = float(data.get("user", {}).get("pending_reward", 0))
    except (TypeError, ValueError):
        pending_reward = 0.0
    print(f"  level={data.get('new_level')} pending_reward={pending_reward}")
    return True, pending_reward


def activate_boost(session, init_data, device_id, tg_id, display_preview):
    headers = dict(COMMON_HEADERS_TEMPLATE)
    headers["X-Telegram-Init-Data"] = init_data
    
    # Kèm thêm TMA Session vào header nếu có
    tma_session = get_tma_session()
    if tma_session:
        headers["x-atf-tma-session"] = tma_session

    body = {
        "device_id": device_id,
        "display_preview": round(display_preview, 4),
        "initData": init_data,
        "request_id": str(uuid.uuid4()),
        "tg_id": str(tg_id) if tg_id is not None else "",
    }
    params = {"action": "activate_boost", "t": str(int(time.time() * 1000))}
    return session.post(BASE_URL, headers=headers, params=params, json=body, timeout=10)


def send_task_action(session, action, task_id):
    """Hàm gửi request start_task hoặc claim_task"""
    init_data = get_init_data()
    device_id = get_device_id()
    tg_id = parse_tg_id(init_data)

    headers = dict(COMMON_HEADERS_TEMPLATE)
    headers["X-Telegram-Init-Data"] = init_data
    
    tma_session = get_tma_session()
    if tma_session:
        headers["x-atf-tma-session"] = tma_session

    body = {
        "client_started_at": int(time.time()),
        "device_id": device_id,
        "initData": init_data,
        "request_id": str(uuid.uuid4()),
        "task_id": task_id,
        "tg_id": str(tg_id) if tg_id is not None else "",
    }
    params = {"action": action, "t": str(int(time.time() * 1000))}
    try:
        resp = session.post(BASE_URL, headers=headers, params=params, json=body, timeout=10)
        print(f"[TASK-{action.upper()}] task_id={task_id} status={resp.status_code}")
        return resp
    except Exception as e:
        print(f"[TASK-{action.upper()}] Lỗi request task {task_id}: {e}")
        return None


def task_loop(session):
    """Luồng phụ chạy tự động 4 task theo chu kỳ với kiểm tra response và retry/chờ hồi đầy đủ"""
    while not stop_event.is_set():
        # 1. Start 4 task
        for i, task_id in enumerate(TASK_LIST, start=1):
            if stop_event.is_set():
                return
            msg = f"Đang START task {i}/4: {task_id}"
            print(f"[AutoTask] {msg}")
            with state_lock:
                bot_state["task_status"] = msg
            
            send_task_action(session, "start_task", task_id)
            
            for _ in range(40): # Chờ 4 giây giữa các task
                if stop_event.is_set():
                    return
                time.sleep(0.1)

        # 2. Chờ 60 giây trước khi claim
        msg = "Đã start đủ 4 task, đang chờ 60 giây..."
        print(f"[AutoTask] {msg}")
        with state_lock:
            bot_state["task_status"] = msg

        for _ in range(600):
            if stop_event.is_set():
                return
            time.sleep(0.1)

        # 3. Claim 4 task và kiểm tra kết quả
        all_claimed_successfully = True

        for i, task_id in enumerate(TASK_LIST, start=1):
            if stop_event.is_set():
                return
            msg = f"Đang CLAIM task {i}/4: {task_id}"
            print(f"[AutoTask] {msg}")
            with state_lock:
                bot_state["task_status"] = msg

            resp = send_task_action(session, "claim_task", task_id)

            is_success = False
            if resp and resp.status_code == 200:
                try:
                    res_data = resp.json()
                    # Kiểm tra xem Server trả về status success hoặc có nhận reward không
                    if res_data.get("status") == "success" or res_data.get("reward") or res_data.get("success") is True:
                        is_success = True
                except Exception:
                    pass

            if not is_success:
                all_claimed_successfully = False
                print(f"[AutoTask] Task {task_id} claim THẤT BẠI!")

            for _ in range(40): # Chờ 4 giây giữa các task
                if stop_event.is_set():
                    return
                time.sleep(0.1)

        # 4. Phân nhánh thời gian chờ tùy thuộc vào kết quả Claim
        if all_claimed_successfully:
            # Thành công hết -> Nghỉ 2 tiếng (7200s)
            msg = "Đã claim 4 task thành công! Chờ hồi nhiệm vụ 2 tiếng..."
            print(f"[AutoTask] {msg}")
            with state_lock:
                bot_state["task_status"] = msg

            for _ in range(72100): # 7200s * 10
                if stop_event.is_set():
                    return
                time.sleep(0.1)
        else:
            # Thất bại ít nhất 1 task -> Thử lại sau 15 phút (900s)
            msg = "Có task claim thất bại. Sẽ thử lại sau 15 phút..."
            print(f"[AutoTask] {msg}")
            with state_lock:
                bot_state["task_status"] = msg

            for _ in range(9000): # 900s * 10
                if stop_event.is_set():
                    return
                time.sleep(0.1)


def login_refresh_loop(session, init_data, device_id, tg_id):
    """CHỈ làm mới login định kỳ 7-10 phút/lần."""
    while not stop_event.is_set():
        wait = random.uniform(LOGIN_REFRESH_MIN, LOGIN_REFRESH_MAX)
        for _ in range(int(wait)):
            if stop_event.is_set():
                return
            time.sleep(1)
        if stop_event.is_set():
            return
        try:
            current_init_data = get_init_data()
            current_device_id = get_device_id()
            current_tg_id = parse_tg_id(current_init_data)
            ok, _ = login(session, current_init_data, current_device_id, current_tg_id)
            with state_lock:
                if ok:
                    bot_state["last_login_refresh"] = time.time()
                else:
                    print("[LoginRefresh] Login định kỳ thất bại, sẽ thử lại ở chu kỳ sau.")
        except Exception as e:
            print(f"[LoginRefresh] Lỗi kết nối: {e}")


def bot_loop():
    with state_lock:
        bot_state["is_running"] = True
        bot_state["last_status"] = "Đang khởi chạy bot..."

    init_data = get_init_data()
    if not init_data:
        with state_lock:
            bot_state["last_status"] = "LỖI: thiếu biến môi trường INIT_DATA"
            bot_state["is_running"] = False
        print(bot_state["last_status"])
        return

    tg_id = parse_tg_id(init_data)
    device_id = get_device_id()
    print(f"Device ID: {device_id}")

    session = requests.Session()
    ok, pending_reward = login(session, init_data, device_id, tg_id)
    last_pending_reward = pending_reward
    last_reward_change_time = time.time()
    EPSILON = 1e-8
    if not ok:
        with state_lock:
            bot_state["last_status"] = "Login thất bại lúc khởi động"
            bot_state["is_running"] = False
        return
    if pending_reward is None:
        pending_reward = 0.0

    threading.Thread(
        target=login_refresh_loop,
        args=(session, init_data, device_id, tg_id),
        daemon=True,
    ).start()

    # Khởi chạy luồng Auto Task song song
    threading.Thread(
        target=task_loop,
        args=(session,),
        daemon=True,
    ).start()

    vong_lap = 1
    failed_status_count = 0
    last_abuse = {}
    try:
        while not stop_event.is_set():
            current_init_data = get_init_data()
            current_device_id = get_device_id()
            current_tg_id = parse_tg_id(current_init_data)

            try:
                resp = activate_boost(session, current_init_data, current_device_id, current_tg_id, pending_reward)
                if resp.status_code == 200:
                    data = resp.json()
                    res_status = data.get("status")

                    if res_status != "success":
                        failed_status_count += 1
                        msg = f"Server trả về status '{res_status}' (Lần {failed_status_count}/20)"
                        print(f"[Lượt {vong_lap}] {msg}")
                        
                        if failed_status_count >= 20:
                            stop_msg = f"Server không trả về 'success' 20 lần liên tiếp ('{res_status}'), bot đã dừng."
                            with state_lock:
                                bot_state["last_status"] = stop_msg
                            print(f"[Lượt {vong_lap}] {stop_msg}")
                            break
                    else:
                        failed_status_count = 0

                    abuse = data.get("abuse_watch", {})
                    last_abuse = abuse
                    try:
                         new_pending = float(data.get("pending_reward", pending_reward))
                    except (TypeError, ValueError):
                         new_pending = pending_reward

                    if abs(new_pending - last_pending_reward) > EPSILON:
                        last_pending_reward = new_pending
                        last_reward_change_time = time.time()

                    pending_reward = new_pending

                    if time.time() - last_reward_change_time > 240:
                        with state_lock:
                            bot_state["last_status"] = (
                                "Pending reward không thay đổi quá 240 giây, bot đã dừng."
                            )
                        print(bot_state["last_status"])
                        break

                    ban_level = abuse.get("temporary_ban_level", 0)
                    if ban_level and ban_level > 0:
                        with state_lock:
                            bot_state["last_status"] = f"BỊ BAN TẠM THỜI level={ban_level}, bot đã dừng"
                            bot_state["ban_level"] = ban_level
                        print(bot_state["last_status"])
                        break

                    status_line = (f"[Lượt {vong_lap}] status={res_status} pending_reward={pending_reward} "
                                    f"req={abuse.get('request_count')}/{abuse.get('threshold_requests')}")
                    print(status_line)

                    now = time.time()
                    prev_loop_time = bot_state.get("last_loop")
                    prev_pending = bot_state.get("pending_reward")
                    rate = 0.0
                    if prev_loop_time and prev_pending is not None and now > prev_loop_time:
                        delta = pending_reward - prev_pending
                        elapsed = now - prev_loop_time
                        if elapsed > 0 and delta >= 0:
                            rate = delta / elapsed

                    bot_state.update({
                        "last_status": status_line,
                        "pending_reward": pending_reward,
                        "vong_lap": vong_lap,
                        "last_loop": now,
                        "rate_per_sec": rate,
                        "request_count": abuse.get("request_count"),
                        "threshold_requests": abuse.get("threshold_requests"),
                        "ban_level": 0,
                    })

                # Xử lý khi bị lỗi 401/403 hoặc server yêu cầu session (Re-login và tiếp tục vòng lặp thay vì dừng bot)
                elif resp.status_code in [401, 403] or "tma_session_required" in resp.text:
                    print(f"[Lượt {vong_lap}] Session hết hạn ({resp.status_code}) - Re-login lại...")
                    set_tma_session("")
                    ok, pending_reward = login(session, current_init_data, current_device_id, current_tg_id)
                    if not ok:
                        print(f"[Lượt {vong_lap}] Re-login thất bại, tạm chờ 10s...")
                        time.sleep(10)
                else:
                    print(f"[Lượt {vong_lap}] Mã lỗi: {resp.status_code} - {resp.text[:200]}")

            except Exception as e:
                print(f"Lỗi kết nối: {e}")

            base_wait = random.uniform(MIN_INTERVAL, MAX_INTERVAL)
            cho = compute_adaptive_wait(last_abuse, base_wait)
            
            for _ in range(int(cho * 10)):
                if stop_event.is_set():
                    break
                time.sleep(0.1)
            vong_lap += 1
    finally:
        with state_lock:
            bot_state["is_running"] = False
            if stop_event.is_set():
                bot_state["last_status"] = "Đã dừng bot khẩn cấp (Emergency Stop)."


def start_bot_thread():
    global bot_thread, stop_event
    with state_lock:
        if bot_state["is_running"]:
            return False, "Bot đang chạy."
        stop_event.clear()
        bot_thread = threading.Thread(target=bot_loop, daemon=True)
        bot_thread.start()
        return True, "Khởi động bot thành công."


def stop_bot_thread():
    global stop_event
    with state_lock:
        if not bot_state["is_running"]:
            return False, "Bot đã dừng từ trước."
        stop_event.set()
        bot_state["last_status"] = "Đang phát lệnh dừng bot..."
        return True, "Đã gửi lệnh dừng bot."


start_bot_thread()


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ATF · Boost Monitor</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=IBM+Plex+Mono:wght@400;500;600&family=Inter:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #14100c;
    --panel: #1e1712;
    --panel-line: #33291d;
    --gold: #e8a33d;
    --gold-bright: #f5c468;
    --text: #f2e9dd;
    --muted: #9c8f7c;
    --success: #4caf7d;
    --warn: #d98f2b;
    --danger: #e1543d;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    min-height: 100vh;
    background: var(--bg);
    color: var(--text);
    font-family: 'Inter', sans-serif;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 24px;
  }
  .card {
    width: 100%;
    max-width: 480px;
    background: var(--panel);
    border: 1px solid var(--panel-line);
    border-radius: 18px;
    padding: 32px 28px;
  }
  .eyebrow-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 28px;
  }
  .eyebrow {
    font-family: 'Space Grotesk', sans-serif;
    font-weight: 700;
    font-size: 15px;
    letter-spacing: 0.14em;
    color: var(--muted);
  }
  .eyebrow span { color: var(--gold); }
  .pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.06em;
    padding: 5px 10px;
    border-radius: 999px;
    background: rgba(76, 175, 125, 0.12);
    color: var(--success);
  }
  .pill.danger { background: rgba(225, 84, 61, 0.14); color: var(--danger); }
  .pill.stopped { background: rgba(156, 143, 124, 0.14); color: var(--muted); }
  .pill .dot {
    width: 6px; height: 6px; border-radius: 50%;
    background: currentColor;
  }
  .pill.alive .dot { animation: pulse 1.6s ease-in-out infinite; }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.35; }
  }
  .reward-block { text-align: center; margin-bottom: 8px; }
  .reward-number {
    font-family: 'IBM Plex Mono', monospace;
    font-weight: 600;
    font-size: 48px;
    color: var(--gold-bright);
    letter-spacing: -0.01em;
    line-height: 1;
  }
  .reward-label {
    font-size: 12px;
    color: var(--muted);
    letter-spacing: 0.1em;
    text-transform: uppercase;
    margin-top: 8px;
  }
  .quota-wrap { margin: 28px 0 24px; }
  .quota-labels {
    display: flex;
    justify-content: space-between;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 12px;
    color: var(--muted);
    margin-bottom: 6px;
  }
  .quota-bar {
    height: 6px;
    background: #0d0a07;
    border-radius: 999px;
    overflow: hidden;
  }
  .quota-fill {
    height: 100%;
    background: var(--success);
    border-radius: 999px;
    transition: width 0.6s ease, background 0.6s ease;
    width: 0%;
  }
  .stats-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 14px;
  }
  .stat {
    background: #16110c;
    border: 1px solid var(--panel-line);
    border-radius: 12px;
    padding: 14px 16px;
  }
  .stat-label {
    font-size: 11px;
    color: var(--muted);
    letter-spacing: 0.08em;
    text-transform: uppercase;
    margin-bottom: 6px;
  }
  .stat-value {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 18px;
    font-weight: 500;
    color: var(--text);
  }
  .footer {
    margin-top: 24px;
    text-align: center;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
    color: var(--muted);
  }
  .status-line {
    margin-top: 6px;
    font-size: 12px;
    color: var(--muted);
    text-align: center;
    word-break: break-word;
  }
</style>
</head>
<body>
  <div class="card">

    <div class="eyebrow-row">
      <div class="eyebrow">ATF<span>·</span>BOOST MONITOR</div>

      <div class="pill alive" id="statusPill">
        <span class="dot"></span>
        <span id="statusText">LIVE</span>
      </div>
    </div>

    <div class="reward-block">
      <div class="reward-number" id="rewardNumber">0.0000</div>
      <div class="reward-label">Pending Reward · ATF</div>
    </div>

    <div class="quota-wrap">

      <div class="quota-labels">
        <span>Request Quota</span>
        <span id="quotaText">0 / 300</span>
      </div>

      <div class="quota-bar">
        <div class="quota-fill" id="quotaFill"></div>
      </div>

    </div>

    <div class="stats-grid">

      <div class="stat">
        <div class="stat-label">Vòng lặp</div>
        <div class="stat-value" id="vongLap">–</div>
      </div>

      <div class="stat">
        <div class="stat-label">Uptime</div>
        <div class="stat-value" id="uptime">–</div>
      </div>

    </div>

    <div class="footer">
      cập nhật <span id="lastCheck">–</span> giây trước
    </div>

    <div class="status-line" id="statusLine"></div>
    <div class="status-line" id="taskStatusLine" style="color: var(--gold); margin-top: 4px;"></div>

    <hr style="margin:25px 0;border:0;border-top:1px solid #33291d;">

    <div id="adminPanel">

      <!-- LOGIN -->

      <div id="loginBox">

        <div style="font-size:18px;font-weight:bold;margin-bottom:12px;">
          🔒 Admin
        </div>

        <input
          id="adminPassword"
          type="password"
          placeholder="Admin Password"
          style="
            width:100%;
            padding:10px;
            border-radius:8px;
            border:1px solid #444;
            background:#111;
            color:white;
            margin-bottom:10px;
          "
        >

        <button
          onclick="loginAdmin()"
          style="
            width:100%;
            padding:10px;
            border:none;
            border-radius:8px;
            background:#e8a33d;
            color:black;
            font-weight:bold;
            cursor:pointer;
          "
        >
          Đăng nhập
        </button>

      </div>

      <!-- ADMIN -->

      <div id="adminBox" style="display:none;">

        <div style="
          font-size:18px;
          font-weight:bold;
          margin-bottom:18px;
          color:#4caf7d;
        ">
          🟢 Admin Online
        </div>

        <button
          onclick="window.open('https://web.telegram.org/k/#@ATF_AIRDROP_bot', '_blank')"
          style="
            width:100%;
            padding:10px;
            border:none;
            border-radius:8px;
            background:#0088cc;
            color:white;
            font-weight:bold;
            cursor:pointer;
            margin-bottom:15px;
          "
        >
          ✈️ Mở Telegram Web
        </button>

        <div style="margin-bottom:6px;">
          Device ID
        </div>

        <input
          id="deviceInput"
          type="text"
          style="
            width:100%;
            padding:10px;
            border-radius:8px;
            border:1px solid #444;
            background:#111;
            color:white;
            margin-bottom:15px;
          "
        >

        <div style="margin-bottom:6px;">
          Init Data
        </div>

        <textarea
          id="initInput"
          rows="6"
          style="
            width:100%;
            padding:10px;
            border-radius:8px;
            border:1px solid #444;
            background:#111;
            color:white;
            resize:vertical;
            margin-bottom:15px;
          "
        ></textarea>

        <button
          id="saveBtn"
          onclick="saveConfig()"
          style="
            width:100%;
            padding:10px;
            border:none;
            border-radius:8px;
            background:#e8a33d;
            color:black;
            font-weight:bold;
            cursor:pointer;
            margin-bottom:10px;
          "
        >
          💾 Save & Hot Reload
        </button>

        <button
          id="saveForeverBtn"
          onclick="saveForever()"
          style="
            width:100%;
            padding:10px;
            border:none;
            border-radius:8px;
            background:#3b82f6;
            color:white;
            font-weight:bold;
            cursor:pointer;
            margin-bottom:10px;
          "
        >
          💾 Save Forever (Render)
        </button>

        <button
          id="startBtn"
          onclick="startBot()"
          style="
            width:100%;
            padding:10px;
            border:none;
            border-radius:8px;
            background:#4caf7d;
            color:white;
            font-weight:bold;
            cursor:pointer;
            margin-bottom:10px;
          "
        >
          🟢 Start Bot
        </button>

        <button
          id="stopBtn"
          onclick="stopBot()"
          style="
            width:100%;
            padding:10px;
            border:none;
            border-radius:8px;
            background:#e1543d;
            color:white;
            font-weight:bold;
            cursor:pointer;
            margin-bottom:10px;
          "
        >
          🔴 Emergency Stop
        </button>

        <button
          onclick="logoutAdmin()"
          style="
            width:100%;
            padding:10px;
            border:none;
            border-radius:8px;
            background:#555;
            color:white;
            font-weight:bold;
            cursor:pointer;
          "
        >
          🚪 Logout
        </button>

      </div>

    </div>

  </div>

<script>
  let displayedReward = 0;
  let ratePerSec = 0;
  let lastServerUpdate = Date.now();

  function fmtUptime(sec) {
    if (sec == null) return "–";
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = Math.floor(sec % 60);
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
  }

  async function poll() {
    try {
      const res = await fetch('/api/status');
      const data = await res.json();

      displayedReward = data.pending_reward ?? displayedReward;
      ratePerSec = data.rate_per_sec ?? 0;
      lastServerUpdate = Date.now();

      document.getElementById('vongLap').textContent = data.vong_lap ?? '–';
      document.getElementById('uptime').textContent = fmtUptime(data.uptime_seconds);
      document.getElementById('statusLine').textContent = data.bot_last_status ?? '';
      document.getElementById('taskStatusLine').textContent = data.task_status ? `Task: ${data.task_status}` : '';

      const reqCount = data.request_count ?? 0;
      const threshold = data.threshold_requests ?? 300;
      const pct = Math.min(100, (reqCount / threshold) * 100);

      document.getElementById('quotaText').textContent =
        `${reqCount} / ${threshold}`;

      const fill = document.getElementById('quotaFill');

      fill.style.width = pct + '%';

      fill.style.background =
        pct >= 90
          ? 'var(--danger)'
          : (pct >= 75
              ? 'var(--warn)'
              : 'var(--success)');

      const pill = document.getElementById('statusPill');
      const pillText = document.getElementById('statusText');

      if (data.ban_level && data.ban_level > 0) {
        pill.classList.remove('alive', 'stopped');
        pill.classList.add('danger');
        pillText.textContent = 'BANNED';
      } else if (!data.is_running) {
        pill.classList.remove('alive', 'danger');
        pill.classList.add('stopped');
        pillText.textContent = 'STOPPED';
      } else {
        pill.classList.add('alive');
        pill.classList.remove('danger', 'stopped');
        pillText.textContent = 'LIVE';
      }

    } catch (e) {
      document.getElementById('statusText').textContent = 'OFFLINE';
    }
  }

  async function checkAdminStatus() {
    try {
      const res = await fetch('/api/admin/status');
      const data = await res.json();
      if (data.logged_in) {
        document.getElementById("loginBox").style.display = "none";
        document.getElementById("adminBox").style.display = "block";
        document.getElementById("deviceInput").value = data.device_id || "";
        document.getElementById("initInput").value = data.init_data || "";
      } else {
        document.getElementById("loginBox").style.display = "block";
        document.getElementById("adminBox").style.display = "none";
      }
    } catch(e) {}
  }

  async function loginAdmin() {
    const password = document.getElementById("adminPassword").value;
    const res = await fetch("/api/admin/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: password })
    });

    if (res.ok) {
      alert("Đăng nhập thành công");
      checkAdminStatus();
    } else {
      alert("Sai mật khẩu");
    }
  }

  async function logoutAdmin() {
    await fetch("/api/admin/logout", { method: "POST" });
    checkAdminStatus();
  }

  async function saveConfig() {
    const device_id = document.getElementById("deviceInput").value;
    const init_data = document.getElementById("initInput").value;
    const res = await fetch("/api/admin/save-config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ device_id, init_data })
    });
    const data = await res.json();
    alert(data.message || (res.ok ? "Lưu thành công!" : "Thất bại"));
  }

  async function saveForever() {
    const device_id = document.getElementById("deviceInput").value;
    const init_data = document.getElementById("initInput").value;
    const btn = document.getElementById("saveForeverBtn");
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = "⏳ Saving to Render...";

    try {
      const res = await fetch("/api/admin/save-render", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ device_id, init_data })
      });
      const data = await res.json();
      alert(data.message || (res.ok ? "Lưu Render thành công!" : "Lỗi"));
    } catch(e) {
      alert("Lỗi kết nối server: " + e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = originalText;
    }
  }

  async function startBot() {
    const res = await fetch("/api/admin/start", { method: "POST" });
    const data = await res.json();
    alert(data.message);
    poll();
  }

  async function stopBot() {
    const res = await fetch("/api/admin/stop", { method: "POST" });
    const data = await res.json();
    alert(data.message);
    poll();
  }

  function tick() {
    const elapsed = (Date.now() - lastServerUpdate) / 1000;
    const projected = displayedReward + ratePerSec * elapsed;

    document.getElementById('rewardNumber').textContent = projected.toFixed(4);
    document.getElementById('lastCheck').textContent = elapsed.toFixed(0);

    requestAnimationFrame(tick);
  }

  poll();
  checkAdminStatus();
  setInterval(poll, 5000);
  requestAnimationFrame(tick);
</script>
</body>
</html>
"""


@app.route("/")
def home():
    """Dashboard giao diện đẹp, tự cập nhật số liệu."""
    return DASHBOARD_HTML


@app.route("/api/status")
def api_status():
    """API JSON để dashboard fetch, đồng thời UptimeRobot có thể ping route này hoặc route /"""
    with state_lock:
        last_refresh = bot_state.get("last_login_refresh")
        snapshot = dict(bot_state)
    return {
        "status": "alive",
        "bot_last_status": snapshot["last_status"],
        "pending_reward": snapshot["pending_reward"],
        "rate_per_sec": snapshot["rate_per_sec"],
        "vong_lap": snapshot["vong_lap"],
        "request_count": snapshot["request_count"],
        "threshold_requests": snapshot["threshold_requests"],
        "ban_level": snapshot["ban_level"],
        "is_running": snapshot["is_running"],
        "task_status": snapshot.get("task_status", ""),
        "seconds_since_login_refresh": (time.time() - last_refresh) if last_refresh else None,
        "uptime_seconds": int(time.time() - snapshot["started_at"]),
    }

@app.post("/api/admin/login")
def admin_login():
    data = request.get_json(force=True)

    password = data.get("password", "")

    if password != ADMIN_PASSWORD:
        return jsonify({
            "success": False,
            "message": "Sai mật khẩu"
        }), 401

    session["admin"] = True

    return jsonify({
        "success": True
    })

@app.post("/api/admin/logout")
def admin_logout():
    session.clear()

    return jsonify({
        "success": True
    })

@app.get("/api/admin/status")
def admin_status():
    return jsonify({
        "logged_in": is_admin(),
        "device_id": get_device_id(),
        "init_data": get_init_data()
    })

@app.post("/api/admin/save-config")
def admin_save_config():
    if not is_admin():
        return jsonify({"success": False, "message": "Chưa đăng nhập admin"}), 403
    data = request.get_json(force=True) or {}
    new_device_id = data.get("device_id", "").strip()
    new_init_data = data.get("init_data", "").strip()

    with config_lock:
        if new_device_id:
            runtime_config["DEVICE_ID"] = new_device_id
        if new_init_data:
            runtime_config["INIT_DATA"] = new_init_data
            runtime_config["TMA_SESSION"] = ""  # Reset session cũ khi đổi init_data mới

    return jsonify({
        "success": True,
        "message": "Đã hot reload cấu hình thành công (không cần restart Render)!"
    })

@app.route("/api/external/update-config", methods=["POST", "OPTIONS"])
def external_update_config():
    """API cho Userscripts/Extension gửi initData tự động qua POST"""
    # Xử lý preflight request của CORS
    if request.method == "OPTIONS":
        response = jsonify({"status": "ok"})
        response.headers.add("Access-Control-Allow-Origin", "*")
        response.headers.add("Access-Control-Allow-Headers", "Content-Type")
        response.headers.add("Access-Control-Allow-Methods", "POST, OPTIONS")
        return response, 200

    data = request.get_json(force=True) or {}
    password = data.get("password", "")
    
    if password != ADMIN_PASSWORD:
        resp = jsonify({"success": False, "message": "Sai Mật khẩu Admin"})
        resp.headers.add("Access-Control-Allow-Origin", "*")
        return resp, 401
        
    new_device_id = data.get("device_id", "").strip()
    new_init_data = data.get("init_data", "").strip()

    if not new_init_data:
        resp = jsonify({"success": False, "message": "Thiếu initData"})
        resp.headers.add("Access-Control-Allow-Origin", "*")
        return resp, 400

    with config_lock:
        if new_device_id:
            runtime_config["DEVICE_ID"] = new_device_id
        runtime_config["INIT_DATA"] = new_init_data
        runtime_config["TMA_SESSION"] = ""  # Reset session cũ

    print(f"[AUTO-HOOK] Tự động đẩy initData mới thành công!")
    resp = jsonify({"success": True, "message": "Đã tự động sync cấu hình thành công!"})
    resp.headers.add("Access-Control-Allow-Origin", "*")
    return resp, 200

@app.post("/api/admin/save-render")
def admin_save_render():
    if not is_admin():
        return jsonify({"success": False, "message": "Chưa đăng nhập admin"}), 403

    if not RENDER_API_KEY or not RENDER_SERVICE_ID:
        return jsonify({
            "success": False,
            "message": "LỖI: Chưa cấu hình RENDER_API_KEY hoặc RENDER_SERVICE_ID trong môi trường!"
        }), 400

    data = request.get_json(force=True) or {}
    new_device_id = data.get("device_id", "").strip()
    new_init_data = data.get("init_data", "").strip()

    render_headers = {
        "Authorization": f"Bearer {RENDER_API_KEY}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

    env_url = f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/env-vars"

    try:
        get_resp = requests.get(env_url, headers=render_headers, timeout=15)
        if get_resp.status_code != 200:
            return jsonify({
                "success": False,
                "message": f"Render API Get Env Failed [{get_resp.status_code}]: {get_resp.text}"
            }), get_resp.status_code

        current_vars = get_resp.json()
        env_dict = {}

        if isinstance(current_vars, list):
            for item in current_vars:
                ev = item.get("envVar", item)
                if "key" in ev and "value" in ev:
                    env_dict[ev["key"]] = ev["value"]

        if new_init_data:
            env_dict["INIT_DATA"] = new_init_data
        if new_device_id:
            env_dict["DEVICE_ID"] = new_device_id

        payload = [{"key": k, "value": v} for k, v in env_dict.items()]

        put_resp = requests.put(env_url, headers=render_headers, json=payload, timeout=15)
        if put_resp.status_code not in (200, 201):
            return jsonify({
                "success": False,
                "message": f"Render API Update Failed [{put_resp.status_code}]: {put_resp.text}"
            }), put_resp.status_code

    except Exception as e:
        return jsonify({
            "success": False,
            "message": f"Lỗi kết nối tới Render API: {str(e)}"
        }), 500

    with config_lock:
        if new_device_id:
            runtime_config["DEVICE_ID"] = new_device_id
        if new_init_data:
            runtime_config["INIT_DATA"] = new_init_data
            runtime_config["TMA_SESSION"] = ""

    return jsonify({
        "success": True,
        "message": "Đã lưu vĩnh viễn vào Render Environment Variables và hot-reload thành công!"
    })

@app.post("/api/admin/start")
def admin_start_bot():
    if not is_admin():
        return jsonify({"success": False, "message": "Chưa đăng nhập admin"}), 403
    ok, msg = start_bot_thread()
    return jsonify({"success": ok, "message": msg})

@app.post("/api/admin/stop")
def admin_stop_bot():
    if not is_admin():
        return jsonify({"success": False, "message": "Chưa đăng nhập admin"}), 403
    ok, msg = stop_bot_thread()
    return jsonify({"success": ok, "message": msg})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
