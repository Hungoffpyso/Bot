#!/usr/bin/env python3
"""
Telegram Auto Message Scheduler
Tự động gửi tin nhắn định kỳ từ tài khoản Telegram tới bot.
"""

import asyncio
import json
import logging
import os
import platform
import re
import socket
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    UserNotParticipantError,
    UsernameNotOccupiedError,
    UsernameInvalidError,
)

# Các lỗi mạng cần bắt để Auto Reconnect
NETWORK_ERRORS = (
    ConnectionError,
    OSError,
    TimeoutError,
    asyncio.TimeoutError,
    ConnectionResetError,
    ConnectionAbortedError,
    ConnectionRefusedError,
    BrokenPipeError,
)

# ==================== ĐƯỜNG DẪN ====================
BASE_DIR = Path(__file__).parent.resolve()
CONFIG_PATH = BASE_DIR / "config.json"
SESSION_DIR = BASE_DIR / "session"
DB_PATH = BASE_DIR / "database" / "data.db"
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "app.log"

# Tạo thư mục nếu chưa có
SESSION_DIR.mkdir(exist_ok=True)
(DB_PATH.parent).mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# ==================== LOGGING ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("TelegramScheduler")


# ==================== DATABASE ====================
def init_db():
    """Khởi tạo bảng settings và logs."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            bot TEXT NOT NULL,
            message TEXT NOT NULL,
            interval_hours INTEGER NOT NULL DEFAULT 24,
            last_run TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT NOT NULL,
            bot TEXT,
            message TEXT,
            status TEXT NOT NULL,
            error TEXT
        )
    """)

    conn.commit()
    conn.close()


def save_settings(bot: str, message: str, interval_hours: int, last_run: str | None = None):
    """Lưu / cập nhật settings."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO settings (id, bot, message, interval_hours, last_run)
        VALUES (1, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            bot = excluded.bot,
            message = excluded.message,
            interval_hours = excluded.interval_hours,
            last_run = COALESCE(excluded.last_run, settings.last_run)
    """, (bot, message, interval_hours, last_run))
    conn.commit()
    conn.close()


def update_last_run(last_run: str):
    """Cập nhật thời gian chạy gần nhất."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    cursor = conn.cursor()
    cursor.execute("UPDATE settings SET last_run = ? WHERE id = 1", (last_run,))
    conn.commit()
    conn.close()


def get_last_run() -> str | None:
    """Lấy last_run từ DB."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    cursor = conn.cursor()
    cursor.execute("SELECT last_run FROM settings WHERE id = 1")
    row = cursor.fetchone()
    conn.close()
    return row[0] if row and row[0] else None


def add_log(bot: str, message: str, status: str, error: str | None = None):
    """Ghi nhật ký gửi tin."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    cursor = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "INSERT INTO logs (time, bot, message, status, error) VALUES (?, ?, ?, ?, ?)",
        (now, bot, message, status, error),
    )
    conn.commit()
    conn.close()


# ==================== CONFIG ====================
def load_config() -> dict:
    """Đọc config.json."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Không tìm thấy file config: {CONFIG_PATH}")

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

    required = ["api_id", "api_hash", "bot", "message"]
    for key in required:
        if key not in config or not config[key]:
            raise ValueError(f"Thiếu hoặc rỗng trường bắt buộc trong config.json: {key}")

    config.setdefault("interval_hours", 24)
    return config


# ==================== TELEGRAM CLIENT ====================
async def create_client(api_id: int, api_hash: str) -> TelegramClient:
    """Tạo và đăng nhập TelegramClient (có retry khi database is locked)."""
    session_path = str(SESSION_DIR / "user")
    client = TelegramClient(session_path, api_id, api_hash)

    # Retry connect khi gặp "database is locked" (thường xảy ra trên Termux / restart nhanh)
    max_retries = 5
    for attempt in range(1, max_retries + 1):
        try:
            await client.connect()
            break
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e).lower():
                wait = attempt * 3  # 3s, 6s, 9s, 12s, 15s
                logger.warning(
                    f"Session database is locked (lần {attempt}/{max_retries}). "
                    f"Đợi {wait}s rồi thử lại..."
                )
                await asyncio.sleep(wait)
                if attempt == max_retries:
                    raise
            else:
                raise
        except Exception:
            raise

    if not await client.is_user_authorized():
        logger.info("Chưa đăng nhập. Bắt đầu quá trình đăng nhập...")
        phone = input("Nhập số điện thoại (kèm mã quốc gia, ví dụ +849xxxxxxxx): ").strip()

        try:
            await client.send_code_request(phone)
        except PhoneNumberInvalidError:
            logger.error("Số điện thoại không hợp lệ.")
            raise

        code = input("Nhập mã OTP nhận được từ Telegram: ").strip()

        try:
            await client.sign_in(phone, code)
        except SessionPasswordNeededError:
            password = input("Tài khoản bật 2FA. Nhập mật khẩu 2FA: ").strip()
            await client.sign_in(password=password)
        except PhoneCodeInvalidError:
            logger.error("Mã OTP không đúng.")
            raise

        logger.info("Đăng nhập thành công. Session đã được lưu.")
    else:
        logger.info("Đã có session hợp lệ. Bỏ qua đăng nhập.")

    me = await client.get_me()
    logger.info(f"Đang chạy với tài khoản: {me.first_name} (@{me.username or 'N/A'})")
    return client


# ==================== INTERNET CHECK ====================
def _ping_host(host: str = "8.8.8.8", timeout: int = 3) -> tuple[bool, float | None]:
    """
    Thực hiện ping cụ thể tới host.
    Trả về (success, latency_ms).
    Hỗ trợ Linux / Termux / Windows.
    """
    system = platform.system().lower()

    try:
        if system == "windows":
            # Windows: ping -n 1 -w timeout_ms
            cmd = ["ping", "-n", "1", "-w", str(timeout * 1000), host]
        else:
            # Linux / Termux / macOS: ping -c 1 -W timeout_sec
            cmd = ["ping", "-c", "1", "-W", str(timeout), host]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout + 2,
        )

        if result.returncode != 0:
            return False, None

        # Parse latency từ output
        # Linux/Termux: time=12.3 ms  hoặc time=12.3ms
        # Windows: time=12ms  hoặc time<1ms
        output = result.stdout + result.stderr
        match = re.search(r"time[=<>](\d+(?:\.\d+)?)\s*ms", output, re.IGNORECASE)
        latency = float(match.group(1)) if match else None
        return True, latency

    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False, None


def is_internet_available(hosts: list[str] | None = None) -> tuple[bool, str | None, float | None]:
    """
    Kiểm tra Internet bằng ping cụ thể.
    Thử lần lượt các host tin cậy.
    Trả về (có mạng?, host thành công, latency_ms).
    """
    if hosts is None:
        hosts = ["8.8.8.8", "1.1.1.1", "8.8.4.4"]

    for host in hosts:
        ok, latency = _ping_host(host)
        if ok:
            return True, host, latency

    # Fallback: thử TCP connect nếu ping bị chặn
    try:
        socket.setdefaulttimeout(3)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.connect(("8.8.8.8", 53))
        return True, "8.8.8.8 (TCP)", None
    except OSError:
        pass

    return False, None, None


async def wait_for_internet(check_interval: int = 10):
    """
    Chờ đến khi có kết nối Internet trở lại.
    Hiển thị kết quả ping cụ thể khi có mạng.
    """
    ok, host, latency = is_internet_available()
    if ok:
        if latency is not None:
            logger.debug(f"Internet OK | {host} | {latency:.1f} ms")
        return

    logger.warning("Mất kết nối Internet. Đang chờ kết nối lại...")

    while True:
        ok, host, latency = is_internet_available()
        if ok:
            if latency is not None:
                logger.info(f"✅ Đã có kết nối Internet trở lại | Ping {host}: {latency:.1f} ms")
            else:
                logger.info(f"✅ Đã có kết nối Internet trở lại | {host}")
            return

        logger.info("⏳ Chưa có Internet (ping thất bại)... thử lại sau %d giây", check_interval)
        await asyncio.sleep(check_interval)


async def ensure_connected(client: TelegramClient):
    """
    Đảm bảo client vẫn đang kết nối.
    Nếu đã ngắt → chờ Internet rồi kết nối lại (Auto Reconnect).
    """
    if client.is_connected():
        return

    logger.warning("Client đã ngắt kết nối. Đang thực hiện Auto Reconnect...")
    await wait_for_internet()

    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise ConnectionError("Session hết hạn hoặc chưa được ủy quyền sau khi reconnect")
        logger.info("✅ Auto Reconnect thành công.")
    except Exception as e:
        logger.error(f"Auto Reconnect thất bại: {e}")
        raise


# ==================== COUNTDOWN ====================
async def countdown_sleep(total_seconds: float, update_every: int = 60):
    """
    Ngủ với bộ đếm thời gian còn lại.
    Cập nhật log định kỳ (mặc định mỗi 60 giây).
    """
    if total_seconds <= 0:
        return

    remaining = total_seconds
    while remaining > 0:
        hours = int(remaining // 3600)
        minutes = int((remaining % 3600) // 60)
        seconds = int(remaining % 60)

        if hours > 0:
            time_str = f"{hours}h {minutes:02d}m {seconds:02d}s"
        elif minutes > 0:
            time_str = f"{minutes}m {seconds:02d}s"
        else:
            time_str = f"{seconds}s"

        logger.info(f"⏳ Còn lại: {time_str}")

        sleep_time = min(update_every, remaining)
        await asyncio.sleep(sleep_time)
        remaining -= sleep_time

async def send_message(client: TelegramClient, bot: str, message: str) -> tuple[bool, str | None]:
    """
    Gửi tin nhắn tới bot.
    Trả về (success, error_message)
    """
    # Chờ Internet + đảm bảo client đang kết nối
    await wait_for_internet()
    await ensure_connected(client)

    try:
        # Đảm bảo bot bắt đầu bằng @
        if not bot.startswith("@"):
            bot = f"@{bot}"

        entity = await client.get_entity(bot)
        await client.send_message(entity, message)

        logger.info(f"Đã gửi thành công tới {bot}: {message}")
        return True, None

    except FloodWaitError as e:
        wait = e.seconds + 5
        logger.warning(f"FloodWait: phải chờ {wait} giây.")
        await countdown_sleep(wait, update_every=30)
        # Thử lại một lần
        try:
            await wait_for_internet()
            await ensure_connected(client)
            entity = await client.get_entity(bot)
            await client.send_message(entity, message)
            logger.info(f"Gửi lại thành công sau FloodWait tới {bot}")
            return True, None
        except Exception as retry_err:
            return False, f"FloodWait retry failed: {retry_err}"

    except (UsernameNotOccupiedError, UsernameInvalidError):
        err = f"Bot không tồn tại hoặc username không hợp lệ: {bot}"
        logger.error(err)
        return False, err

    except NETWORK_ERRORS as e:
        err = f"Lỗi mạng ({type(e).__name__}): {e}"
        logger.error(err)
        await wait_for_internet()
        # Cố gắng reconnect ngay
        try:
            await ensure_connected(client)
        except Exception:
            pass
        return False, err

    except Exception as e:
        err = str(e)
        # Một số lỗi Telethon có thể chứa từ khóa mạng
        err_lower = err.lower()
        if any(k in err_lower for k in ("timeout", "network", "connection", "read timed out", "server closed")):
            logger.error(f"Lỗi mạng (phát hiện qua message): {err}")
            await wait_for_internet()
            try:
                await ensure_connected(client)
            except Exception:
                pass
            return False, f"Network-related: {err}"

        logger.error(f"Lỗi khi gửi tin: {err}")
        return False, err


# ==================== MAIN LOOP ====================
async def scheduler_loop(client: TelegramClient, config: dict):
    """Vòng lặp chính: gửi tin theo lịch."""
    bot = config["bot"]
    message = config["message"]
    interval_hours = int(config["interval_hours"])
    interval_seconds = interval_hours * 3600

    # Đồng bộ settings vào DB
    save_settings(bot, message, interval_hours)

    logger.info(f"Bắt đầu scheduler | Bot: {bot} | Interval: {interval_hours}h")
    logger.info(f"Nội dung: {message}")

    while True:
        try:
            # Đảm bảo có Internet + client đang kết nối trước mỗi vòng lặp
            await wait_for_internet()
            await ensure_connected(client)

            # Kiểm tra xem có nên gửi ngay không (dựa trên last_run)
            last_run_str = get_last_run()

            if last_run_str:
                try:
                    last_run = datetime.strptime(last_run_str, "%Y-%m-%d %H:%M:%S")
                    next_run = last_run + timedelta(hours=interval_hours)
                    now = datetime.now()
                    if now < next_run:
                        wait_seconds = (next_run - now).total_seconds()
                        logger.info(
                            f"Chưa đến giờ. Lần gửi tiếp theo: {next_run.strftime('%Y-%m-%d %H:%M:%S')}"
                        )
                        await countdown_sleep(wait_seconds)
                        continue
                except ValueError:
                    pass  # parse lỗi thì gửi luôn

            # Gửi tin
            logger.info("Đang gửi tin nhắn...")
            success, error = await send_message(client, bot, message)

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if success:
                add_log(bot, message, "Success")
                update_last_run(now_str)
                logger.info(f"Gửi thành công lúc {now_str}")
            else:
                add_log(bot, message, "Failed", error)
                logger.error(f"Gửi thất bại: {error}")

            # Ngủ đến lần tiếp theo (có bộ đếm)
            logger.info(f"Bắt đầu chờ {interval_hours} giờ đến lần gửi tiếp theo...")
            await countdown_sleep(interval_seconds)

        except asyncio.CancelledError:
            logger.info("Scheduler bị hủy.")
            break
        except NETWORK_ERRORS as e:
            logger.error(f"Lỗi mạng trong vòng lặp ({type(e).__name__}): {e}")
            await wait_for_internet()
            try:
                await ensure_connected(client)
                logger.info("Đã reconnect. Tiếp tục vòng lặp...")
            except Exception as reconnect_err:
                logger.error(f"Reconnect thất bại: {reconnect_err}. Sẽ thử lại sau 15 giây...")
                await asyncio.sleep(15)
        except Exception as e:
            err_lower = str(e).lower()
            if any(k in err_lower for k in ("timeout", "network", "connection", "read timed out", "server closed")):
                logger.error(f"Lỗi mạng (phát hiện qua message) trong vòng lặp: {e}")
                await wait_for_internet()
                try:
                    await ensure_connected(client)
                except Exception:
                    await asyncio.sleep(15)
            else:
                logger.exception(f"Lỗi không mong đợi trong vòng lặp: {e}")
                logger.info("Thử lại sau 5 phút...")
                await countdown_sleep(300)


async def main():
    logger.info("=" * 50)
    logger.info("Telegram Auto Message Scheduler khởi động")
    logger.info("=" * 50)

    config = load_config()
    init_db()
    api_id = int(config["api_id"])
    api_hash = str(config["api_hash"])

    # Vòng lặp vô hạn – nếu client chết do mạng thì tự khởi động lại
    while True:
        client = None
        try:
            # Kiểm tra Internet trước khi làm việc
            await wait_for_internet()

            logger.info("Đang tạo / kết nối Telegram client...")
            client = await create_client(api_id, api_hash)

            async with client:
                await scheduler_loop(client, config)

        except KeyboardInterrupt:
            logger.info("Người dùng dừng chương trình (Ctrl+C).")
            break

        except NETWORK_ERRORS as e:
            logger.error(f"Lỗi mạng nghiêm trọng ({type(e).__name__}): {e}")
            logger.info("Đợi 10 giây rồi tự khởi động lại toàn bộ client...")
            await wait_for_internet()
            await asyncio.sleep(10)

        except sqlite3.OperationalError as e:
            # Xử lý đặc biệt lỗi "database is locked" của Telethon session
            if "database is locked" in str(e).lower():
                logger.error("Session database is locked. Đợi 20 giây để giải phóng file...")
                await asyncio.sleep(20)
            else:
                logger.exception(f"Lỗi SQLite: {e}")
                await asyncio.sleep(15)

        except Exception as e:
            err_lower = str(e).lower()
            if any(k in err_lower for k in ("timeout", "network", "connection", "read timed out", "server closed", "disconnect")):
                logger.error(f"Lỗi mạng (phát hiện qua message): {e}")
                logger.info("Đợi 10 giây rồi tự khởi động lại toàn bộ client...")
                await wait_for_internet()
                await asyncio.sleep(10)
            elif "database is locked" in err_lower:
                logger.error("Session database is locked (bắt qua Exception). Đợi 20 giây...")
                await asyncio.sleep(20)
            else:
                logger.exception(f"Lỗi nghiêm trọng không phải mạng: {e}")
                logger.info("Đợi 30 giây rồi thử khởi động lại...")
                await asyncio.sleep(30)

        finally:
            # Luôn cố gắng đóng client sạch sẽ trước khi restart
            if client is not None:
                try:
                    if client.is_connected():
                        await client.disconnect()
                        logger.info("Đã disconnect client.")
                except Exception as disc_err:
                    logger.debug(f"Lỗi khi disconnect: {disc_err}")
                # Đợi thêm một chút để SQLite session file được giải phóng hoàn toàn
                await asyncio.sleep(3)

            logger.info("Chuẩn bị vòng lặp khởi động lại...")


if __name__ == "__main__":
    asyncio.run(main())
