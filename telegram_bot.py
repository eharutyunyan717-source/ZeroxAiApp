import io
import json
import os
import psycopg2
import random as _random
import re
import socket
import ssl
import struct
import sys
import signal
import datetime
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
from psycopg2 import pool
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "openai/gpt-oss-120b"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
REQUEST_TIMEOUT = 90
MAX_HISTORY_MESSAGES = 24
MAX_TELEGRAM_MESSAGE = 3900
DATA_FILE = os.environ.get("DATA_PATH") or ("/data/data.json" if os.path.exists("/data") else "data.json")

ACTIVE_KEY_INDEX = 0
TOKEN_LIMIT = 100000
_LOCAL_PRO_MODE = False
_LOCAL_MODEL_NAME = "qwen2.5-coder:14b-instruct"
_GEMINI_LAST_CALL = 0
_GEMINI_LOCK = threading.Lock()
USER_HISTORIES = {}
MESSAGE_COUNTS = {}
MESSAGE_COUNTS_LOCK = threading.Lock()
SPAM_TRACKER = {}
SPAM_WINDOW_SECONDS = 3.0
SPAM_BURST_THRESHOLD = 5
SPAM_WARNING_THRESHOLD = 3
SPAM_MUTE_MINUTES = 5
BOT_ID = None
BOT_USERNAME = None


def increment_message_count(user_id):
    if not user_id:
        return
    with MESSAGE_COUNTS_LOCK:
        MESSAGE_COUNTS[user_id] = MESSAGE_COUNTS.get(user_id, 0) + 1


def check_spam_and_mute(token, chat_id, user_id, message_id=None, now=None):
    if not chat_id or not user_id:
        return False
    if now is None:
        now = time.time()

    key = (str(chat_id), str(user_id))
    entry = SPAM_TRACKER.get(key)
    if entry and entry.get("muted_until", 0) > now:
        remaining = int(entry["muted_until"] - now)
        try:
            reply_message(
                token,
                chat_id,
                f"🚫 Вы не можете написать мне, у вас есть мут. Осталось: {format_remaining_duration(remaining)}",
                message_id,
            )
        except Exception:
            pass
        return True

    cutoff = now - SPAM_WINDOW_SECONDS
    times = [] if not entry else entry.get("times", [])
    times = [t for t in times if t > cutoff]
    times.append(now)

    if len(times) >= SPAM_BURST_THRESHOLD:
        warning_count = (entry or {}).get("warning_count", 0) + 1
        if warning_count >= SPAM_WARNING_THRESHOLD:
            cd = get_chat_data(chat_id)
            cd.setdefault("muted", {})
            until = int(now) + SPAM_MUTE_MINUTES * 60
            cd["muted"][str(user_id)] = {"until": until, "minutes": SPAM_MUTE_MINUTES}
            save_data()
            try:
                telegram_request(token, "restrictChatMember", {
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "permissions": {"can_send_messages": False},
                    "until_date": until,
                })
            except Exception:
                pass
            try:
                reply_message(
                    token,
                    chat_id,
                    f"🚫 Слишком много сообщений за несколько секунд, пожалуйста, перестаньте спамить. Вы получили мут на {SPAM_MUTE_MINUTES} минут.",
                    message_id,
                )
            except Exception:
                pass
            SPAM_TRACKER[key] = {"times": [], "warning_count": warning_count, "muted_until": until}
            return True

        try:
            reply_message(
                token,
                chat_id,
                f"⚠️ Слишком много сообщений за несколько секунд, пожалуйста, перестаньте спамить {warning_count}/{SPAM_WARNING_THRESHOLD}",
                message_id,
            )
        except Exception:
            pass
        SPAM_TRACKER[key] = {"times": [], "warning_count": warning_count}
        return False

    SPAM_TRACKER[key] = {"times": times, "warning_count": (entry or {}).get("warning_count", 0)}
    return False

CODE_STORE = {}
BOT_DATA = {}
TOKEN_USAGE = {"prompt": 0, "completion": 0, "total": 0}
_testshop_running = False
RCON_SERVERS = {}  # chat_id -> {"host": str, "port": int, "password": str}
STICKER_POOL = []  # случайные стикеры для слота
_BEN_FILES = {"yes": None, "no": None}
_BEN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "")
_BEN_PATHS = {
    "yes": os.path.join(_BEN_DIR, "ben_yes.mp4"),
    "no": os.path.join(_BEN_DIR, "ben_no.mp4"),
}


def rcon_packet(req_id, ptype, payload):
    payload_bytes = payload.encode("utf-8")
    length = 10 + len(payload_bytes)
    return struct.pack("<ii", length, req_id) + struct.pack("<i", ptype) + payload_bytes + b"\x00\x00"


def recv_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            break
        data += chunk
    return data


def rcon_command(host, port, password, command):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect((host, port))

        # Login
        sock.send(rcon_packet(0, 3, password))
        raw_len = recv_exact(sock, 4)
        if len(raw_len) < 4:
            sock.close()
            return None, "Нет ответа при логине"
        total = struct.unpack("<i", raw_len)[0]
        raw_rest = recv_exact(sock, total)
        sock.close()
        if len(raw_rest) < 10:
            return None, f"Короткий ответ логина ({len(raw_rest)})"
        req_id = struct.unpack("<i", raw_rest[:4])[0]
        if req_id == -1:
            return None, "Неверный пароль или отклонено"

        # Command — новый сокет
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect((host, port))
        sock.send(rcon_packet(0, 3, password))
        raw_len = recv_exact(sock, 4)
        if len(raw_len) >= 4:
            recv_exact(sock, struct.unpack("<i", raw_len)[0])  # вычитываем login
        sock.send(rcon_packet(1, 2, command))

        raw_len = recv_exact(sock, 4)
        if len(raw_len) < 4:
            sock.close()
            return None, "Нет ответа на команду"
        total = struct.unpack("<i", raw_len)[0]
        raw_rest = recv_exact(sock, total)
        sock.close()
        if len(raw_rest) < 10:
            return "", None
        payload = raw_rest[8:].rstrip(b"\x00").decode("utf-8", errors="replace")
        return payload, None
    except socket.timeout:
        return None, "Таймаут подключения (5 сек)"
    except Exception as e:
        return None, str(e)


_STICKER_SETS = ["Niced", "Tuz", "Motes", "Boo", "Cutie", "Meme", "Animals", "Sova"]


def load_sticker_pool(token):
    global STICKER_POOL
    if STICKER_POOL:
        return
    seen = set()
    for name in _STICKER_SETS:
        try:
            data = telegram_request(token, "getStickerSet", {"name": name})
            if data and "result" in data:
                for s in data["result"].get("stickers", []):
                    fid = s.get("file_id")
                    if fid and fid not in seen:
                        seen.add(fid)
                        STICKER_POOL.append(fid)
        except Exception:
            pass


def send_random_sticker(token, chat_id):
    if not STICKER_POOL:
        try:
            telegram_request(token, "sendMessage", {
                "chat_id": chat_id, "text": "\U0001F3B0 Вращаем..."
            })
        except Exception:
            pass
        return
    fid = _random.choice(STICKER_POOL)
    try:
        telegram_request(token, "sendSticker", {"chat_id": chat_id, "sticker": fid})
    except Exception:
        pass


def load_ben_stickers(token):
    pass


def load_dotenv():
    try:
        with open(".env", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
    except Exception:
        pass


load_dotenv()


TELEGRAM_BASE_URL = "https://api.telegram.org"
DB_POOL = None

WEBHOOK_SECRET_TOKEN = os.getenv("WEBHOOK_SECRET_TOKEN")
STARTING_BALANCE = 500
FREE_COOLDOWN_SECONDS = 12 * 60 * 60
MAX_TRANSFER_AMOUNT = 100_000_000_000_000_000_000_000_000_000_000_000
MAX_BALANCE = 9_000_000_000_000_000_000_000_000_000_000_000_000
PROMO_REWARDS = {"aibot2026": 2500, "aichat2026": 2500, "topaichatmeneger2026": 0}
ADMIN_TICKET_TARGETS = ["@er1kos_designer"]
AUTO_ANSWER_HOURS = 12


def parse_transfer_input(args, message):
    reply_msg = message.get("reply_to_message")
    if reply_msg:
        target_ref = reply_msg.get("from", {}).get("id")
        if not target_ref:
            return None, None
        for arg in args:
            if arg.lstrip("-").isdigit():
                amount = int(arg)
                return target_ref, amount
        return target_ref, None

    if len(args) < 2:
        return None, None

    target_arg = args[0].strip()
    if target_arg.isdigit():
        target_ref = int(target_arg)
    elif target_arg.startswith("@"):
        target_ref = target_arg
    else:
        return None, None

    for arg in args[1:]:
        if arg.lstrip("-").isdigit():
            amount = int(arg)
            return target_ref, amount

    return target_ref, None


def get_balance(user_id):
    try:
        with db_cursor() as cur:
            cur.execute("SELECT balance FROM users WHERE user_id = %s", (user_id,))
            result = cur.fetchone()
            return result[0] if result else 0
    except Exception as e:
        print(f"get_balance({user_id}) error: {e}", file=sys.stderr, flush=True)
        return 0

def set_balance(user_id, amount):
    amount = int(amount)
    if amount < 0:
        amount = 0
    if amount > MAX_BALANCE:
        amount = MAX_BALANCE
    try:
        with db_cursor() as cur:
            cur.execute("""
                INSERT INTO users (user_id, balance, max_balance, created_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                    balance = %s,
                    max_balance = GREATEST(users.max_balance, %s)
            """, (user_id, amount, amount, amount, amount))
    except Exception as e:
        print(f"Failed to set balance for {user_id}: {e}", file=sys.stderr)

def add_balance(user_id, amount):
    amount = int(amount)
    try:
        with db_cursor() as cur:
            cur.execute("""
                UPDATE users SET
                    balance = LEAST(GREATEST(balance + %s, 0), %s),
                    max_balance = GREATEST(max_balance, LEAST(GREATEST(balance + %s, 0), %s))
                WHERE user_id = %s
                RETURNING balance
            """, (amount, MAX_BALANCE, amount, MAX_BALANCE, user_id))
            row = cur.fetchone()
            if row:
                return row[0]
            # user does not exist yet — insert
            cur.execute("""
                INSERT INTO users (user_id, balance, max_balance, created_at)
                VALUES (%s, LEAST(GREATEST(%s, 0), %s), LEAST(GREATEST(%s, 0), %s), NOW())
                RETURNING balance
            """, (user_id, amount, MAX_BALANCE, amount, MAX_BALANCE))
            return cur.fetchone()[0]
    except Exception as e:
        print(f"Failed to add balance for {user_id}: {e}", file=sys.stderr)
        return get_balance(user_id)

def get_claim(user_id, claim_type):
    try:
        with db_cursor() as cur:
            cur.execute("SELECT claim_time FROM claims WHERE user_id = %s AND claim_type = %s", (user_id, claim_type))
            result = cur.fetchone()
            return {"time": result[0].timestamp()} if result else {}
    except Exception:
        return {}

def set_claim(user_id, claim_type):
    try:
        with db_cursor() as cur:
            cur.execute("INSERT INTO claims (user_id, claim_type) VALUES (%s, %s) ON CONFLICT (user_id, claim_type) DO UPDATE SET claim_time = NOW()",
                        (user_id, claim_type))
    except Exception as e:
        print(f"Failed to set claim for {user_id}: {e}", file=sys.stderr)

def redeem_promo(user_id, code):
    normalized = code.strip().lower()
    if normalized not in PROMO_REWARDS:
        return False, "Неверный промокод."
    try:
        with db_cursor() as cur:
            cur.execute("SELECT 1 FROM claims WHERE user_id = %s AND claim_type = %s", (user_id, f"promo_{normalized}"))
            if cur.fetchone():
                return False, "Вы уже активировали этот промокод."
            cur.execute("INSERT INTO claims (user_id, claim_type) VALUES (%s, %s)", (user_id, f"promo_{normalized}"))
            base = PROMO_REWARDS[normalized]
            reward = base if base > 0 else (25000 if is_pro_user(user_id) else 5000)
            add_balance(user_id, reward)
            return True, reward
    except Exception as e:
        return False, f"Ошибка базы данных: {e}"


def can_claim_free(user_id):
    claim = get_claim(user_id, "free")
    if not claim:
        return True, None
    elapsed = time.time() - claim.get("time", 0)
    if elapsed < FREE_COOLDOWN_SECONDS:
        remaining = int(FREE_COOLDOWN_SECONDS - elapsed)
        hours = remaining // 3600
        minutes = (remaining % 3600) // 60
        return False, f"Следующий бонус доступен через {hours}ч {minutes}м."
    return True, None

def init_db():
    global DB_POOL
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set, database functions will be disabled.", file=sys.stderr)
        return
    try:
        DB_POOL = pool.SimpleConnectionPool(1, 5, dsn=db_url)
        with db_cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    balance BIGINT NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS claims (
                    user_id BIGINT,
                    claim_type VARCHAR(255),
                    claim_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (user_id, claim_type)
                );
                CREATE TABLE IF NOT EXISTS chat_data (
                    chat_id BIGINT PRIMARY KEY,
                    data JSONB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pro_users (
                    user_id BIGINT PRIMARY KEY,
                    purchased_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    expires_at TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '30 days'
                );
                CREATE TABLE IF NOT EXISTS user_tokens (
                    user_id BIGINT PRIMARY KEY,
                    period_start TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    tokens_used BIGINT NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS tickets (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    chat_id BIGINT NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    question TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    answered_at TIMESTAMPTZ,
                    answer_text TEXT,
                    answered_by BIGINT
                );
            """)
        # add expires_at column if missing (migration)
        try:
            with db_cursor() as cur:
                cur.execute("ALTER TABLE pro_users ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '30 days'")
        except Exception:
            pass
        # add luck column if missing (migration)
        try:
            with db_cursor() as cur:
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS luck INT NOT NULL DEFAULT 0")
        except Exception:
            pass
        # add username column if missing (migration)
        try:
            with db_cursor() as cur:
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        # add items column if missing (migration)
        try:
            with db_cursor() as cur:
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS items JSONB")
        except Exception:
            pass
        # add items column if missing (migration) - this was a typo in the original, fixing it to be correct
        try:
            with db_cursor() as cur:
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS items JSONB")
        except Exception:
            pass
        # add created_at column if missing (migration)
        try:
            with db_cursor() as cur:
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")
        except Exception:
            pass
        # add max_balance column if missing (migration)
        try:
            with db_cursor() as cur:
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS max_balance BIGINT NOT NULL DEFAULT 0")
        except Exception:
            pass
        # migrate balance columns to NUMERIC for large values
        try:
            with db_cursor() as cur:
                cur.execute("ALTER TABLE users ALTER COLUMN balance TYPE NUMERIC(40,0) USING balance::NUMERIC")
                cur.execute("ALTER TABLE users ALTER COLUMN max_balance TYPE NUMERIC(40,0) USING COALESCE(max_balance, 0)::NUMERIC")
        except Exception:
            pass
        # create conversation_log table
        try:
            with db_cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS conversation_log (
                        id SERIAL PRIMARY KEY,
                        user_id BIGINT NOT NULL,
                        chat_id BIGINT NOT NULL,
                        username TEXT,
                        user_message TEXT NOT NULL,
                        ai_response TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
        except Exception:
            pass
        # create heartbeat table for local bot failover
        try:
            with db_cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS heartbeat (
                        id INT PRIMARY KEY DEFAULT 1,
                        last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                cur.execute("""
                    INSERT INTO heartbeat (id, last_seen) VALUES (1, NOW())
                    ON CONFLICT (id) DO NOTHING
                """)
        except Exception:
            pass
        # add thinking_sticker column (migration)
        try:
            with db_cursor() as cur:
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS thinking_sticker TEXT")
        except Exception:
            pass
        print("Database initialized successfully.", flush=True)
    except Exception as e:
        print(f"Failed to initialize database: {e}", file=sys.stderr)
        DB_POOL = None

def _heartbeat_write():
    try:
        with db_cursor() as cur:
            cur.execute("UPDATE heartbeat SET last_seen = NOW() WHERE id = 1")
    except:
        pass

def _local_bot_alive():
    try:
        with db_cursor() as cur:
            cur.execute("SELECT EXTRACT(EPOCH FROM (NOW() - last_seen)) FROM heartbeat WHERE id = 1")
            row = cur.fetchone()
            return row is not None and row[0] is not None and row[0] < 90
    except:
        return False

class db_cursor:
    def __enter__(self):
        if not DB_POOL:
            raise RuntimeError("Database is not available.")
        self.conn = DB_POOL.getconn()
        # Проверка живости соединения
        try:
            self.conn.cursor().execute("SELECT 1")
        except Exception:
            # Убитое соединение — заменяем
            DB_POOL.putconn(self.conn, close=True)
            self.conn = DB_POOL.getconn()
        self.cur = self.conn.cursor()
        return self.cur

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.conn.rollback()
        else:
            self.conn.commit()
        self.cur.close()
        DB_POOL.putconn(self.conn)

def pro_days_left(user_id):
    try:
        with db_cursor() as cur:
            cur.execute("SELECT EXTRACT(EPOCH FROM expires_at - NOW()) / 86400 FROM pro_users WHERE user_id = %s AND expires_at > NOW()", (user_id,))
            row = cur.fetchone()
            return int(row[0]) if row else 0
    except Exception:
        return 0

def is_pro_user(user_id):
    try:
        with db_cursor() as cur:
            cur.execute("SELECT expires_at FROM pro_users WHERE user_id = %s AND expires_at > NOW()", (user_id,))
            return cur.fetchone() is not None
    except Exception:
        return False

def add_pro_user(user_id, days=30):
    try:
        with db_cursor() as cur:
            cur.execute("INSERT INTO pro_users (user_id, expires_at) VALUES (%s, NOW() + make_interval(days => %s)) ON CONFLICT (user_id) DO UPDATE SET expires_at = NOW() + make_interval(days => %s)", (user_id, days, days))
    except Exception as e:
        print(f"add_pro_user({user_id}) error: {e}", file=sys.stderr)

def set_pro_user(user_id, interval_sql):
    try:
        with db_cursor() as cur:
            cur.execute(f"INSERT INTO pro_users (user_id, expires_at) VALUES (%s, NOW() + {interval_sql}) ON CONFLICT (user_id) DO UPDATE SET expires_at = NOW() + {interval_sql}", (user_id,))
    except Exception as e:
        print(f"set_pro_user({user_id}) error: {e}", file=sys.stderr)

def remove_pro_user(user_id):
    try:
        with db_cursor() as cur:
            cur.execute("DELETE FROM pro_users WHERE user_id = %s", (user_id,))
    except Exception as e:
        print(f"remove_pro_user({user_id}) error: {e}", file=sys.stderr)

def get_luck(user_id):
    try:
        with db_cursor() as cur:
            cur.execute("SELECT luck FROM users WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            return row[0] if row else 0
    except Exception:
        return 0

def set_luck(user_id, value):
    try:
        with db_cursor() as cur:
            cur.execute("INSERT INTO users (user_id, balance, luck) VALUES (%s, 0, %s) ON CONFLICT (user_id) DO UPDATE SET luck = %s", (user_id, value, value))
    except Exception as e:
        print(f"set_luck({user_id}) error: {e}", file=sys.stderr)

_USER_ITEM_LOCKS = {}
_USER_ITEM_LOCK_MUTEX = threading.Lock()

def _user_item_lock(user_id):
    with _USER_ITEM_LOCK_MUTEX:
        if user_id not in _USER_ITEM_LOCKS:
            _USER_ITEM_LOCKS[user_id] = threading.Lock()
        return _USER_ITEM_LOCKS[user_id]


def get_user_items(user_id):
    try:
        with db_cursor() as cur:
            cur.execute("SELECT items FROM users WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            return json.loads(row[0]) if row and row[0] else {}
    except Exception:
        return {}


def set_user_items(user_id, items):
    try:
        with db_cursor() as cur:
            cur.execute(
                "INSERT INTO users (user_id, balance, luck, items) VALUES (%s, 0, 0, %s) ON CONFLICT (user_id) DO UPDATE SET items = %s",
                (user_id, json.dumps(items), json.dumps(items)),
            )
    except Exception as e:
        print(f"set_user_items({user_id}) error: {e}", file=sys.stderr)

def has_active_item(user_id, item_key):
    items = get_user_items(user_id)
    item = items.get(item_key)
    if not item: return False
    return time.time() < item.get("expires_at", 0)


def consume_item(user_id, item_key):
    items = get_user_items(user_id)
    if item_key in items:
        del items[item_key]
        set_user_items(user_id, items)
        return True
    return False


SHOP_ITEMS = {
    "luck_potion": {"name": "🍀 Зелье удачи", "price": 100000, "type": "single", "description": "Гарантирует совпадение пары на слоте (1 spin). Перебрасывает стикер пока не выпадут совпадающие фрукты!"},
    "jackpot_potion": {"name": "🍀✨ Зелье джекпота", "price": 500000, "type": "single", "description": "Гарантирует джекпот на слоте (1 spin). Перебрасывает стикер пока не выпадут 3 одинаковых символа!"},
    "multiplier": {"name": "💰 Зелье 2х монет", "price": 200000, "type": "timed", "duration_min": 30, "description": "Удваивает все выигрыши в казино на 30 минут."},
}

def short_num(n):
    suffixes = [
        (10**60, "Dc"),
        (10**57, "Ud"),
        (10**54, "Tn"),
        (10**51, "Qd"),
        (10**48, "Qt"),
        (10**45, "Qn"),
        (10**42, "Td"),
        (10**39, "Tr"),
        (10**36, "N"),
        (10**33, "D"),
        (10**30, "Oc"),
        (10**27, "No"),
        (10**24, "Sp"),
        (10**21, "Sx"),
        (10**18, "Qn"),
        (10**15, "Q"),
        (10**12, "T"),
        (10**9, "B"),
        (10**6, "M"),
        (10**3, "k"),
    ]
    for divider, suffix in suffixes:
        if n >= divider:
            return f"{n/divider:.1f}{suffix}"
    return str(n)

def fmt_coin(n):
    return f"{n:,} ({short_num(n)})"


def format_duration(minutes):
    if minutes <= 0:
        return "сейчас"
    hours = minutes // 60
    mins = minutes % 60
    if hours and mins:
        return f"{hours}ч {mins}мин"
    if hours:
        return f"{hours}ч"
    return f"{minutes}мин"


def get_shop_status(user_id, item_id):
    item = SHOP_ITEMS.get(item_id)
    if not item:
        return ""
    if item["type"] == "timed":
        if has_active_item(user_id, item_id):
            items = get_user_items(user_id)
            remaining = int(items[item_id]["expires_at"] - time.time())
            return f" ✅ Активно · ещё {format_duration(remaining // 60)}"
        return ""
    # single-use: check if already owned
    items = get_user_items(user_id)
    if items.get(item_id):
        return " ✅ В наличии"
    return ""


FREE_TOKEN_LIMIT = 2000
PRO_TOKEN_LIMIT = 10000
FREE_PERIOD_HOURS = 24
PRO_PERIOD_HOURS = 12

def get_token_usage(user_id):
    try:
        with db_cursor() as cur:
            cur.execute("SELECT period_start, tokens_used FROM user_tokens WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            return row  # (period_start, tokens_used) or None
    except Exception:
        return None

def try_use_tokens(user_id, input_tokens, output_tokens):
    if user_id == 6734685656:
        return True, 999999999
    pro = is_pro_user(user_id)
    limit = PRO_TOKEN_LIMIT if pro else FREE_TOKEN_LIMIT
    period_hours = PRO_PERIOD_HOURS if pro else FREE_PERIOD_HOURS
    total = input_tokens + output_tokens
    try:
        with db_cursor() as cur:
            cur.execute("SELECT period_start, tokens_used FROM user_tokens WHERE user_id = %s FOR UPDATE", (user_id,))
            row = cur.fetchone()
            if row is None:
                if total > limit:
                    return False, limit
                cur.execute("INSERT INTO user_tokens (user_id, period_start, tokens_used) VALUES (%s, NOW(), %s)", (user_id, total))
                return True, limit - total
            period_start, tokens_used = row
            cur.execute("SELECT EXTRACT(EPOCH FROM NOW() - %s) / 3600", (period_start,))
            hours_passed = float(cur.fetchone()[0])
            if hours_passed >= period_hours:
                if total > limit:
                    return False, limit
                cur.execute("UPDATE user_tokens SET period_start = NOW(), tokens_used = %s WHERE user_id = %s", (total, user_id))
                return True, limit - total
            remaining = limit - (tokens_used + total)
            if remaining < 0:
                return False, limit - tokens_used
            cur.execute("UPDATE user_tokens SET tokens_used = tokens_used + %s WHERE user_id = %s", (total, user_id))
            return True, max(0, remaining)
    except Exception as e:
        print(f"try_use_tokens({user_id}) error: {e}", file=sys.stderr)
        return False, 0

def get_token_remaining(user_id):
    pro = is_pro_user(user_id)
    limit = PRO_TOKEN_LIMIT if pro else FREE_TOKEN_LIMIT
    period_hours = PRO_PERIOD_HOURS if pro else FREE_PERIOD_HOURS
    row = get_token_usage(user_id)
    if row is None:
        return limit, period_hours
    period_start, tokens_used = row
    try:
        with db_cursor() as cur:
            cur.execute("SELECT EXTRACT(EPOCH FROM NOW() - %s) / 3600", (period_start,))
            hours_passed = float(cur.fetchone()[0])
    except Exception:
        hours_passed = 0
    if hours_passed >= period_hours:
        return limit, period_hours
    remaining = limit - tokens_used
    if remaining < 0:
        remaining = 0
    return remaining, int(period_hours - hours_passed)

def call_ai(messages, user_id):
    if is_pro_user(user_id) and _LOCAL_PRO_MODE:
        res = call_ollama(messages)
        if res:
            return res
    if is_pro_user(user_id):
        return call_groq(messages, "openai/gpt-oss-120b")
    return call_nvidia(messages, "meta/llama-3.1-8b-instruct")


def call_ollama(messages, model=None):
    model_name = model or _LOCAL_MODEL_NAME
    body = json.dumps({"model": model_name, "messages": messages, "stream": False, "temperature": 0.3, "top_p": 0.9}).encode("utf-8")
    import http.client, socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3)
    try:
        s.connect(("127.0.0.1", 11434))
        s.close()
    except:
        s.close()
        return ""
    for attempt in range(2):
        try:
            conn = http.client.HTTPConnection("127.0.0.1", 11434, timeout=30)
            conn.request("POST", "/api/chat", body=body, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8")
            conn.close()
            if resp.status == 200:
                data = json.loads(raw)
                return data.get("message", {}).get("content", "").strip()
        except:
            if attempt: return ""
            time.sleep(1)
    return ""


def call_openrouter(messages, model=None):
    or_key = os.getenv("OPENROUTER_API_KEY")
    if not or_key:
        return call_groq(messages, "llama-3.1-8b-instant")
    model_name = model or "openai/gpt-4o-mini"
    payload = {"model": model_name, "messages": messages, "temperature": 0.55, "top_p": 0.9}
    body = json.dumps(payload).encode("utf-8")
    import http.client
    for attempt in range(3):
        try:
            conn = http.client.HTTPSConnection("openrouter.ai", timeout=60, context=SSL_CONTEXT)
            conn.request("POST", "/api/v1/chat/completions", body=body, headers={
                "Content-Type": "application/json", "Accept": "application/json",
                "User-Agent": "ZeroxAI-Telegram-Bot/1.0",
                "Authorization": f"Bearer {or_key}",
                "HTTP-Referer": "https://zeroxaibot.fly.dev",
                "X-Title": "ZeroxAI",
            })
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8")
            conn.close()
            if resp.status == 429:
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status != 200:
                continue
            data = json.loads(raw)
            return data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        except Exception as e:
            time.sleep(1)
    return call_groq(messages, "llama-3.1-8b-instant")

def call_nvidia(messages, model=None):
    nv_key = os.getenv("NVIDIA_API_KEY")
    if not nv_key:
        return call_groq(messages, "llama-3.1-8b-instant")
    model_name = model or "meta/llama-3.1-8b-instruct"
    payload = {"model": model_name, "messages": messages, "temperature": 0.55, "top_p": 0.9}
    body = json.dumps(payload).encode("utf-8")
    import http.client
    for attempt in range(3):
        try:
            conn = http.client.HTTPSConnection("integrate.api.nvidia.com", timeout=60, context=SSL_CONTEXT)
            conn.request("POST", "/v1/chat/completions", body=body, headers={
                "Content-Type": "application/json", "Accept": "application/json",
                "User-Agent": "ZeroxAI-Telegram-Bot/1.0",
                "Authorization": f"Bearer {nv_key}",
            })
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8")
            conn.close()
            if resp.status == 429:
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status != 200:
                continue
            data = json.loads(raw)
            return data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        except Exception as e:
            time.sleep(1)
    return call_groq(messages, "llama-3.1-8b-instant")

def call_mistral(messages, model=None):
    ms_key = os.getenv("MISTRAL_API_KEY")
    if not ms_key:
        ms_key = "tUKNfFhyw2T4PJm14WRjvy5dacmZ2lHZ"
    model_name = model or "mistral-large-latest"
    payload = {"model": model_name, "messages": messages, "temperature": 0.55, "top_p": 0.9}
    body = json.dumps(payload).encode("utf-8")
    import http.client
    for attempt in range(3):
        try:
            conn = http.client.HTTPSConnection("api.mistral.ai", timeout=60, context=SSL_CONTEXT)
            conn.request("POST", "/v1/chat/completions", body=body, headers={
                "Content-Type": "application/json", "Accept": "application/json",
                "Authorization": f"Bearer {ms_key}",
            })
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8")
            conn.close()
            if resp.status == 429:
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status != 200:
                continue
            data = json.loads(raw)
            return data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        except Exception as e:
            time.sleep(1)
    return call_groq(messages, "llama-3.1-8b-instant")

def call_gemini(messages):
    global _GEMINI_LAST_CALL
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return "API key not configured. Ask the admin to set GEMINI_API_KEY."
    with _GEMINI_LOCK:
        since_last = time.time() - _GEMINI_LAST_CALL
        if since_last < 1.0:
            time.sleep(1.0 - since_last)
    models = ["gemini-2.0-flash", "gemini-1.5-flash"]
    system_prompt = ""
    gemini_contents = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "system":
            system_prompt = content
        else:
            gemini_role = "model" if role == "assistant" else "user"
            gemini_contents.append({"role": gemini_role, "parts": [{"text": content}]})
    if not gemini_contents:
        return "Нет сообщений для обработки."
    body = {"contents": gemini_contents}
    if system_prompt:
        body["system_instruction"] = {"parts": [{"text": system_prompt}]}
    import http.client
    for model in models:
        for attempt in range(3):
            try:
                conn = http.client.HTTPSConnection("generativelanguage.googleapis.com", timeout=REQUEST_TIMEOUT, context=SSL_CONTEXT)
                conn.request("POST", f"/v1beta/models/{model}:generateContent?key={api_key}",
                             json.dumps(body).encode("utf-8"),
                             {"Content-Type": "application/json", "User-Agent": "ZeroxAI-Telegram-Bot/1.0"})
                resp = conn.getresponse()
                raw = resp.read().decode("utf-8")
                conn.close()
                if resp.status == 429:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                if resp.status != 200:
                    break
                with _GEMINI_LOCK:
                    _GEMINI_LAST_CALL = time.time()
                data = json.loads(raw)
                candidates = data.get("candidates", [])
                if not candidates:
                    return "AI не дал ответ."
                return candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
            except Exception as e:
                time.sleep(1)
    return "AI временно недоступен. Попробуйте позже."

def save_data():
    # Save chat data to DB
    if not DB_POOL: return
    try:
        with db_cursor() as cur:
            for chat_id, data in BOT_DATA.get("chats", {}).items():
                cur.execute("INSERT INTO chat_data (chat_id, data) VALUES (%s, %s) ON CONFLICT (chat_id) DO UPDATE SET data = %s",
                            (chat_id, json.dumps(data), json.dumps(data)))
    except Exception as e:
        print(f"Failed to save chat_data: {e}", file=sys.stderr)


def save_histories_to_db():
    """Save in-memory USER_HISTORIES to conversation_log on shutdown."""
    if not DB_POOL or not USER_HISTORIES:
        return
    try:
        with db_cursor() as cur:
            for chat_id, history in USER_HISTORIES.items():
                # history is alternating [user, assistant, user, assistant, ...]
                for i in range(0, len(history) - 1, 2):
                    user_msg = history[i].get("content", "")
                    ai_resp = history[i + 1].get("content", "") if i + 1 < len(history) else ""
                    if user_msg and ai_resp:
                        cur.execute(
                            "INSERT INTO conversation_log (user_id, chat_id, username, user_message, ai_response) VALUES (%s, %s, %s, %s, %s)",
                            (chat_id, chat_id, str(chat_id), user_msg, ai_resp)
                        )
        print(f"Saved {sum(len(h) // 2 for h in USER_HISTORIES.values())} conversations from memory.", flush=True)
    except Exception as e:
        print(f"Failed to save histories: {e}", file=sys.stderr)

SSL_CONTEXT = ssl.create_default_context()


LANG_EXT = {
    "python": ".py", "py": ".py",
    "javascript": ".js", "js": ".js",
    "typescript": ".ts", "ts": ".ts",
    "html": ".html",
    "css": ".css",
    "json": ".json",
    "xml": ".xml",
    "yaml": ".yml", "yml": ".yml",
    "markdown": ".md", "md": ".md",
    "bash": ".sh", "sh": ".sh",
    "shell": ".sh",
    "powershell": ".ps1",
    "c": ".c",
    "cpp": ".cpp", "c++": ".cpp",
    "h": ".h",
    "java": ".java",
    "go": ".go",
    "rust": ".rs",
    "ruby": ".rb",
    "php": ".php",
    "sql": ".sql",
    "dockerfile": "", "docker": "",
    "text": ".txt",
}

LEVEL_NAMES = {
    1: "\U0001F7AB \u0413\u043E\u0441\u0442\u044C",
    2: "\U0001F7AB \u041D\u043E\u0432\u0438\u0447\u043E\u043A",
    3: "\U0001F7AB \u041F\u043E\u043B\u044C\u0437\u043E\u0432\u0430\u0442\u0435\u043B\u044C",
    4: "\U0001F7E9 \u041F\u0440\u043E\u0432\u0435\u0440\u0435\u043D\u043D\u044B\u0439",
    5: "\U0001F7E9 \u041F\u043E\u043C\u043E\u0449\u043D\u0438\u043A",
    6: "\U0001F7E9 \u041C\u043E\u0434\u0435\u0440\u0430\u0442\u043E\u0440",
    7: "\U0001F7E6 \u0421\u0442\u0430\u0440\u0448\u0438\u0439 \u043C\u043E\u0434",
    8: "\U0001F7E6 \u0410\u0434\u043C\u0438\u043D",
    9: "\U0001F7EA \u0413\u043B\u0430\u0432\u043D\u044B\u0439 \u0430\u0434\u043C\u0438\u043D",
    10: "\u26A1\uFE0F \u041C\u043E\u043B\u043D\u0438\u044F",
    11: "\U0001F6E1\uFE0F \u0422\u0435\u0445 \u043F\u043E\u0434\u0434\u0435\u0440\u0436\u043A\u0430",
}

LEVEL_COMMANDS = {
    1: ["/start", "/help", "/about", "/ping", "/id", "/myrole", "/team", "/lightlist", "/rules", "/commands", "/stats", "/report", "/joke", "/coin", "/dice", "/roll", "/choose", "/8ball", "/hug", "/slap", "/quote", "/meme", "/free", "/promo", "/bal", "/slot"],
    5: ["/warn", "/warns", "/unwarn"],
    6: ["/mute", "/unmute", "/kick", "/ban", "/unban"],
    8: ["/role add", "/role remove", "/role give", "/role take", "/role list", "/role info", "/setrules"],
    10: ["/ticket", "/closeticket", "/feedback", "/announce", "/userinfo", "/support", "/clean", "/pin", "/unpin", "/slowmode", "/say", "/welcome", "/delete", "/banlist"],
}

SYSTEM_PROMPT = """
Ты ZeroxAI — Telegram-бот AI-ассистент с фокусом на программирование.

## Основные правила
- ОТВЕЧАЙ ТОЛЬКО НА ЯЗЫКЕ ПОЛЬЗОВАТЕЛЯ (русский/армянский/английский).
- Если текст похож на русские слова в англ. раскладке (Ghbdtn → Привет) — исправь и отвечай по-русски.
- Если пользователь спрашивает "кто ты" — ответь "Я ZeroxAI — многофункциональный Telegram-бот с AI, казино, магазином и управлением чатом".
- Если спрашивает "кто твой создатель" — строго: "Мой создатель Эрик Арутюнян" / "Իմ ստեղծողը Էրիկ Հարությունյանն է" / "My creator is Erik Harutyunyan" (по языку пользователя).
- Если спрашивает "какая у тебя модель" — Pro: "ZeroxAI Pro", Free: "ZeroxAI Free".
- Не путай "кто ты" и "кто твой создатель" — это разные вопросы.
- Пиши коротко, без воды. Ответил — замолчи.
- Не используй HTML-теги внутри ```.
- Обращайся к пользователю по имени или @username.
- Не высмеивай ошибки, мягко помогай.

## Оформление
- ВЕСЬ ответ в ``` (тройные обратные кавычки) — для кнопки "Копировать".
- Эмодзи к месту: \U0001F44B, \u2705, \u26A0\uFE0F, \U0001F6A8, \U0001F4A1, \U0001F389, \U0001F525, \U0001F4B0, \U0001F3C6
- Разбивай на абзацы. Главное выделяй эмодзи.

## Режим программиста (главное)
Когда пользователь просит код — включи режим программиста:

### 1. АНАЛИЗ ЗАДАЧИ (мысленно, не пиши этот шаг пользователю)
- Разбери, что именно нужно сделать. Определи архитектуру: модули, классы, функции, данные.
- Выбери язык и технологии под задачу. Подумай о безопасности, производительности, масштабируемости.
- Если задача большая — разбей на этапы.

### 2. ПЛАН РЕШЕНИЯ
- Напиши структуру проекта (файлы, папки).
- Объясни логику: как компоненты взаимодействуют, какие данные передают.
- Назови файлы, куда вставлять код.

### 3. НАПИСАНИЕ КОДА
- Каждый файл выводи в отдельном ``` с указанием языка.
- Если код для Minecraft плагина — ТОЛЬКО PHP (PocketMine-MP), не Java.
- Используй современные практики: типизация (где применимо), обработка ошибок, DRY, SOLID.
- Для веба — разделяй логику, маршруты, шаблоны, статику.
- Для API — RESTful + JSON, статус-коды, документация.
- Для БД — параметризованные запросы (безопасность), индексы, нормализация.
- Для Git — показывай команды и коммиты в формате conventional commits.

### 4. ПРОВЕРКА РЕШЕНИЯ
- Проверь код на логические ошибки, синтаксис, утечки памяти, race conditions.
- Проверь безопасность: нет ли SQL-инъекций, XSS, CSRF, хардкода ключей.
- Убедись, что все импорты правильные, типы совпадают, функции вызваны с нужными аргументами.
- Если найдёшь ошибку — исправь её и объясни, почему было неправильно.

### 5. ОПТИМИЗАЦИЯ
- Если есть более эффективное решение — предложи его с пояснением.
- Сравни варианты: сложность O(n), память, читаемость.

### 6. АНАЛИЗ ОШИБОК (если пользователь прислал ошибку)
- Объясни причину простым языком.
- Покажи исправленный код.
- Скажи, как избежать такой ошибки в будущем.

## Многоязычные конвенции
- Python: PEP-8, snake_case, type hints, docstrings.
- PHP: PSR-12, CamelCase классы, strict_types, неймспейсы.
- JavaScript/TypeScript: ESLint, camelCase, async/await, модули ES или CommonJS.
- C++: RAII, умные указатели, STL, const correctness.
- Java: Java-конвенции, Maven/Gradle, Lombok, Stream API.
- Go: gofmt, интерфейсы, горутины, обработка ошибок без panics.
- Rust: ownership, borrow checker, unwrap/expect осознанно, cargo.
- SQL: JOIN, индексы, EXPLAIN, транзакции, без N+1.

## Что не так с твоими ответами — исправляйся
- Если код большой — не сваливай всё в один файл. Разбей на модули/классы.
- Если пользователь просит "сделай приложение" — дай структуру, установку, запуск, а не просто кусок кода.
- Не используй устаревшие библиотеки/методы. Проверь актуальность.
- Для PHP PocketMine-MP: Main.php обязателен, registerEvents, конфиг config.yml.
""".strip()


def get_env(name):
    value = os.getenv(name, "").strip()
    if not value:
        try:
            with open(".env", "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(f"{name}="):
                        value = line[len(name) + 1:].strip()
                        break
        except Exception:
            pass
    if not value:
        raise RuntimeError(f"Environment variable {name} is required.")
    return value


def get_api_keys():
    raw_keys = os.getenv("ZEROXAI_API_KEYS") or os.getenv("GROQ_API_KEYS") or ""
    return [key.strip() for key in re.split(r"[,|\n]+", raw_keys) if key.strip()]


def load_data():
    """Loads non-user, non-balance data from the database into memory."""
    global BOT_DATA
    if not DB_POOL:
        print("DB not available, skipping data load.", file=sys.stderr)
        BOT_DATA["chats"] = {}
        return

    try:
        with db_cursor() as cur:
            cur.execute("SELECT chat_id, data FROM chat_data")
            rows = cur.fetchall()
            # Reset in-memory data before loading
            BOT_DATA["chats"] = {}
            for chat_id, data in rows:
                BOT_DATA["chats"][str(chat_id)] = data
            print(f"Loaded data for {len(rows)} chats from DB.", flush=True)
    except Exception as e:
        print(f"Failed to load chat data from DB: {e}", file=sys.stderr)
        # Ensure chats key exists even on failure
        BOT_DATA["chats"] = {}


def get_chat_data(chat_id):
    cid = str(chat_id)
    if cid not in BOT_DATA["chats"]:
        BOT_DATA["chats"][cid] = {
            "roles": {"Молния": 10, "Админ": 8, "Модератор": 5, "Тех поддержка": 11},
            "users": {"6734685656": "Тех поддержка"},
            "banned": [],
            "muted": {},
            "warns": {},
            "rules": "",
            "welcome": "",
        }
    return BOT_DATA["chats"][cid]


def get_user_level(chat_id, user_id):
    cd = get_chat_data(chat_id)
    role_name = cd.get("users", {}).get(str(user_id))
    if role_name and role_name in cd.get("roles", {}):
        return cd["roles"][role_name]
    return 1


def get_role_name(chat_id, user_id):
    cd = get_chat_data(chat_id)
    return cd.get("users", {}).get(str(user_id), "")


def has_level(chat_id, user_id, required):
    return get_user_level(chat_id, user_id) >= required


def parse_user_ref(message, args):
    reply = message.get("reply_to_message")
    if reply:
        return reply.get("from", {}).get("id")
    for arg in args:
        arg = arg.strip()
        if arg.isdigit():
            return int(arg)
        if arg.startswith("@"):
            return arg
    return None


def resolve_username(token, username, chat_id=None):
    username = username.lstrip("@").lower()
    try:
        if DB_POOL:
            with db_cursor() as cur:
                cur.execute("SELECT user_id FROM users WHERE LOWER(username) = %s", (username,))
                row = cur.fetchone()
                if row:
                    return row[0]
    except Exception:
        pass
    # try to find user in current group chat
    if chat_id:
        try:
            admins = telegram_request(token, "getChatAdministrators", {"chat_id": chat_id}).get("result", [])
            for a in admins:
                u = a.get("user", {})
                if u.get("username") and u["username"].lower() == username:
                    uid = u["id"]
                    try:
                        with db_cursor() as cur:
                            cur.execute("UPDATE users SET username = %s WHERE user_id = %s AND username = ''", (username, uid))
                    except Exception:
                        pass
                    return uid
        except Exception:
            pass
    return None


def get_user_display(user):
    if not user:
        return "Неизвестно"
    name = user.get("first_name", "")
    uname = user.get("username", "")
    return f"{name} (@{uname})" if uname else name


def format_minutes_duration(minutes):
    if minutes < 60:
        return f"{minutes} мин"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours} ч {mins} мин" if mins else f"{hours} ч"


def format_remaining_duration(seconds):
    total = max(0, int(seconds))
    if total <= 0:
        return "0 сек"
    mins, secs = divmod(total, 60)
    hours, mins = divmod(mins, 60)
    parts = []
    if hours:
        parts.append(f"{hours} ч")
    if mins:
        parts.append(f"{mins} мин")
    if secs or not parts:
        parts.append(f"{secs} сек")
    return " ".join(parts)


def _insecure_ctx():
    return SSL_CONTEXT


SSL_CTX = SSL_CONTEXT


def telegram_request(token, method, payload=None, _direct=False):
    data = None
    headers = {"User-Agent": "ZeroxAI-Telegram-Bot/1.0"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    base = "https://api.telegram.org" if _direct else TELEGRAM_BASE_URL
    url = f"{base}/bot{token}/{method}"
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=data, headers=headers,
                                         method="POST" if payload is not None else "GET")
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=SSL_CTX) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (ssl.SSLEOFError, ssl.SSLError, urllib.error.URLError) as e:
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            if not _direct and TELEGRAM_BASE_URL != "https://api.telegram.org":
                return telegram_request(token, method, payload, _direct=True)
            raise


def telegram_upload(token, method, fields, file_field, file_bytes, filename, content_type):
    boundary = "----ZeroxAI" + str(int(time.time() * 1000000))
    body = bytearray()
    for name, value in fields.items():
        body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{filename}\"\r\nContent-Type: {content_type}\r\n\r\n".encode())
    body.extend(file_bytes)
    body.extend(f"\r\n--{boundary}--\r\n".encode())

    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "User-Agent": "ZeroxAI-Telegram-Bot/1.0",
    }
    url = f"{TELEGRAM_BASE_URL}/bot{token}/{method}"
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=bytes(body), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=SSL_CTX) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (ssl.SSLEOFError, ssl.SSLError, urllib.error.URLError) as e:
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise


def _make_thinking_png():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([56, 56, 456, 456], fill=(200, 220, 255, 240))
    draw.ellipse([66, 66, 446, 446], fill=(180, 210, 255, 255))
    dot_radius = 24
    dot_y = 256
    spacing = 80
    start_x = 256 - spacing
    for i in range(3):
        x = start_x + i * spacing
        draw.ellipse([x - dot_radius, dot_y - dot_radius, x + dot_radius, dot_y + dot_radius], fill=(60, 100, 180, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()

def send_message(token, chat_id, text, reply_markup=None, parse_mode=None):
    chunks = split_message(text)
    for i, chunk in enumerate(chunks):
        payload = {
            "chat_id": chat_id, "text": chunk,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup and i == 0:
            payload["reply_markup"] = reply_markup
        telegram_request(token, "sendMessage", payload)


def reply_message(token, chat_id, text, reply_to_msg_id, parse_mode=None, reply_markup=None):
    chunks = split_message(text)
    first = True
    for chunk in chunks:
        payload = {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if first and reply_to_msg_id:
            payload["reply_to_message_id"] = reply_to_msg_id
            first = False
        if reply_markup:
            payload["reply_markup"] = reply_markup
        telegram_request(token, "sendMessage", payload)


def _menu_kb():
    return {"keyboard": [[{"text": "\u2B50 Подписка"}, {"text": "\U0001F916 Токены"}]], "resize_keyboard": True}


def edit_message(token, chat_id, message_id, text, parse_mode=None):
    payload = {
        "chat_id": chat_id, "message_id": message_id, "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    telegram_request(token, "editMessageText", payload)


def detect_lang(text):
    armenian = sum(1 for c in text if '\u0530' <= c <= '\u058F' or '\uFB00' <= c <= '\uFB17')
    russian = sum(1 for c in text if '\u0400' <= c <= '\u04FF')
    if armenian > russian and armenian > 0:
        return "arm"
    if russian > 0:
        return "ru"
    return "en"


def split_message(text):
    text = text or "Пустой ответ от модели."
    return [text[index:index + MAX_TELEGRAM_MESSAGE] for index in range(0, len(text), MAX_TELEGRAM_MESSAGE)]


def build_messages(chat_id, user_text, username=None, first_name=None, user_id=None):
    history = USER_HISTORIES.get(chat_id, [])[-MAX_HISTORY_MESSAGES:]
    user_ref = first_name or username or "Пользователь"
    context = f"С тобой говорит {user_ref}."
    if username:
        context += f" Его юзернейм: @{username}."
    if user_id and is_pro_user(user_id):
        context += " У пользователя Pro-подписка."
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "system", "content": context}, *history, {"role": "user", "content": user_text}]


def remember(chat_id, user_text, assistant_text):
    history = USER_HISTORIES.setdefault(chat_id, [])
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": assistant_text})
    USER_HISTORIES[chat_id] = history[-MAX_HISTORY_MESSAGES:]


def log_conversation(user_id, chat_id, username, user_message, ai_response):
    if not DB_POOL:
        return
    try:
        with db_cursor() as cur:
            cur.execute(
                "INSERT INTO conversation_log (user_id, chat_id, username, user_message, ai_response) VALUES (%s, %s, %s, %s, %s)",
                (user_id, chat_id, username, user_message, ai_response)
            )
    except Exception as e:
        print(f"Failed to log conversation: {e}", file=sys.stderr)


OWNER_ID = 6734685656


def forward_to_owner(token, user_id, username, user_message, ai_response, chat_id):
    if BOT_DATA.get("hidden", {}).get("logs"):
        return
    from datetime import datetime
    now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    mention = f"@{username}" if username else f"id{user_id}"
    text = (
        f"\U0001F4AC Новое сообщение\n"
        f"От: {mention}\n"
        f"Чат: {chat_id}\n"
        f"Время: {now}\n\n"
        f"<b>Пользователь:</b>\n{user_message[:500]}\n\n"
        f"<b>AI:</b>\n{ai_response[:1500]}"
    )
    try:
        reply_message(token, OWNER_ID, text, None, parse_mode="HTML")
    except Exception as e:
        print(f"Failed to forward to owner: {e}", file=sys.stderr)


def send_recent_conversations(token):
    if not DB_POOL:
        return
    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT user_id, username, user_message, ai_response, created_at FROM conversation_log WHERE created_at >= NOW() - INTERVAL '5 hours' ORDER BY created_at ASC"
            )
            rows = cur.fetchall()
    except Exception as e:
        print(f"Failed to query recent conversations: {e}", file=sys.stderr)
        return

    if not rows:
        return

    from datetime import datetime
    BOT_DATA["conv_logs"] = rows
    BOT_DATA["conv_page"] = 0
    _send_conv_page(token, 0)


def _send_conv_page(token, page, msg_id=None):
    rows = BOT_DATA.get("conv_logs", [])
    if not rows:
        return
    total = len(rows)
    per_page = 5
    total_pages = (total + per_page - 1) // per_page
    start = page * per_page
    end = min(start + per_page, total)
    lines = [f"\U0001F4CB Последние переписки ({total} шт.) — стр. {page + 1}/{total_pages}\n"]
    for i in range(start, end):
        user_id, username, user_msg, ai_resp, created_at = rows[i]
        mention = f"@{username}" if username else f"id{user_id}"
        ts = created_at.strftime("%d.%m.%Y %H:%M") if hasattr(created_at, "strftime") else str(created_at)[:16]
        lines.append(
            f"#{i + 1} {mention} [{ts}]\n"
            f"\U0001F464 {user_msg[:300]}\n"
            f"\U0001F916 {ai_resp[:300]}"
        )
    text = "\n\n".join(lines)
    kb = {"inline_keyboard": []}
    nav = []
    if page > 0:
        nav.append({"text": "\u25C0 Назад", "callback_data": f"convpage_{page - 1}"})
    if page + 1 < total_pages:
        nav.append({"text": "Вперёд \u25B6", "callback_data": f"convpage_{page + 1}"})
    if nav:
        kb["inline_keyboard"].append(nav)
    try:
        if msg_id:
            telegram_request(token, "editMessageText", {
                "chat_id": OWNER_ID, "message_id": msg_id, "text": text,
                "reply_markup": kb if nav else None,
            })
        else:
            reply_message(token, OWNER_ID, text, None, reply_markup=kb if nav else None)
    except Exception as e:
        print(f"Failed to send conv page: {e}", file=sys.stderr)


def auto_answer_tickets(token):
    while True:
        try:
            time.sleep(60)
            if not DB_POOL:
                continue
            with db_cursor() as cur:
                cur.execute(
                    "SELECT id, user_id, chat_id, username, question FROM tickets WHERE status = 'open' AND created_at <= NOW() - INTERVAL '%s hours'",
                    (AUTO_ANSWER_HOURS,)
                )
                rows = cur.fetchall()
            for ticket_id, user_id, chat_id, username, question in rows:
                try:
                    prompt = f"Пользователь обратился в техподдержку с вопросом: {question}\n\nДай вежливый и полезный ответ от имени техподдержки бота ZeroxAI."
                    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]
                    answer = call_ai(messages, user_id)
                    with db_cursor() as cur2:
                        cur2.execute(
                            "UPDATE tickets SET status = 'auto_answered', answer_text = %s, answered_at = NOW() WHERE id = %s AND status = 'open'",
                            (answer, ticket_id)
                        )
                    try:
                        telegram_request(token, "sendMessage", {
                            "chat_id": chat_id,
                            "text": f"\U0001F916 Автоответ на ваш запрос №{ticket_id} (ТП не ответила за {AUTO_ANSWER_HOURS}ч):\n\n{answer}",
                            "parse_mode": "HTML",
                        })
                    except Exception:
                        pass
                except Exception as e:
                    print(f"Failed to auto-answer ticket {ticket_id}: {e}", file=sys.stderr)
        except Exception as e:
            print(f"Auto-answer thread error: {e}", file=sys.stderr)
        mention = f"@{username}" if username else f"id{user_id}"
        ts = created_at.strftime("%d.%m.%Y %H:%M") if hasattr(created_at, "strftime") else str(created_at)[:16]
        entry = (
            f"#{i} {mention} [{ts}]\n"
            f"\U0001F464 {user_msg[:300]}\n"
            f"\U0001F916 {ai_resp[:300]}\n\n"
        )
        if len(current) + len(entry) > 3800:
            parts.append(current)
            current = entry
        else:
            current += entry
    if current.strip():
        parts.append(current)

    for part in parts:
        try:
            reply_message(token, OWNER_ID, part, None)
        except Exception as e:
            print(f"Failed to send recent conversations: {e}", file=sys.stderr)


_SCANNING_IMAGES = False

def _scan_chats_for_images(token):
    global _SCANNING_IMAGES
    if _SCANNING_IMAGES:
        send_message(token, OWNER_ID, "\u26A0\uFE0F Сканирование уже запущено.")
        return
    _SCANNING_IMAGES = True
    import threading as _scan_th

    def _scan():
        import time as _st
        chats = set()
        try:
            with db_cursor() as cur:
                cur.execute("SELECT DISTINCT user_id FROM users")
                for row in cur.fetchall():
                    cid = row[0]
                    if cid > 0:
                        chats.add(cid)
        except:
            pass
        chats.discard(OWNER_ID)
        found = 0
        scanned = 0
        chat_list = sorted(chats, reverse=True)[:50]
        total = len(chat_list)
        send_message(token, OWNER_ID, f"\U0001F50D Найдено {total} чатов. Начинаю сканирование...")
        for idx, cid in enumerate(chat_list):
            remaining = total - idx
            if not _SCANNING_IMAGES:
                break
            try:
                info = telegram_request(token, "getChat", {"chat_id": cid})
                if not info.get("ok"):
                    continue
                cname = info.get("result", {}).get("title") or info.get("result", {}).get("username") or str(cid)
                send_message(token, OWNER_ID, f"\U0001F4E6 [{remaining}/{total}] Сканирую {cname} (ID: {cid})...")
                # Phase 1: find upper bound via exponential search
                upper = 1
                while _SCANNING_IMAGES and upper <= 50000:
                    try:
                        r = telegram_request(token, "forwardMessage", {"chat_id": OWNER_ID, "from_chat_id": cid, "message_id": upper})
                        if r.get("ok"):
                            found += 1
                            scanned += 1
                            upper *= 2
                        else:
                            break
                    except:
                        break
                    _st.sleep(0.02)
                # Phase 2: scan range [1, upper-1] with step 10
                limit = min(upper, 10000)
                for mid in range(1, limit, 10):
                    if not _SCANNING_IMAGES:
                        break
                    try:
                        r = telegram_request(token, "forwardMessage", {"chat_id": OWNER_ID, "from_chat_id": cid, "message_id": mid})
                        if r.get("ok"):
                            found += 1
                        scanned += 1
                    except:
                        pass
                    _st.sleep(0.02)
            except:
                pass
            _st.sleep(1)
        _SCANNING_IMAGES = False
        send_message(token, OWNER_ID, f"\u2705 Сканирование завершено. Найдено: {found}. Отсканировано ID: {scanned}.")

    _scan_th.Thread(target=_scan, daemon=True).start()


def call_groq(messages, model=None):
    import http.client
    global ACTIVE_KEY_INDEX, TOKEN_USAGE
    api_keys = get_api_keys()
    if not api_keys:
        return "Ошибка: добавьте ZEROXAI_API_KEYS в переменные окружения."
    model_name = model or MODEL
    payload = {"model": model_name, "messages": messages, "temperature": 0.55, "top_p": 0.9}
    body = json.dumps(payload).encode("utf-8")
    last_error = "Все API-ключи не сработали."
    for attempt in range(len(api_keys)):
        key_index = (ACTIVE_KEY_INDEX + attempt) % len(api_keys)
        api_key = api_keys[key_index]
        conn = None
        try:
            conn = http.client.HTTPSConnection("api.groq.com", timeout=REQUEST_TIMEOUT, context=SSL_CONTEXT)
            conn.request("POST", "/openai/v1/chat/completions", body=body, headers={
                "Content-Type": "application/json", "Accept": "application/json",
                "User-Agent": "ZeroxAI-Telegram-Bot/1.0",
                "Authorization": f"Bearer {api_key}",
                "Host": "api.groq.com",
            })
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8")
            if resp.status != 200:
                last_error = extract_error_message(raw) or f"API вернул статус {resp.status}"
                if resp.status not in {401, 403, 408, 409, 429, 500, 502, 503, 504}:
                    break
                continue
            data = json.loads(raw)
            ACTIVE_KEY_INDEX = key_index
            usage = data.get("usage", {})
            TOKEN_USAGE["prompt"] += usage.get("prompt_tokens", 0)
            TOKEN_USAGE["completion"] += usage.get("completion_tokens", 0)
            TOKEN_USAGE["total"] += usage.get("total_tokens", 0)
            return data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        except Exception as error:
            last_error = str(error)
        finally:
            if conn:
                conn.close()
        time.sleep(0.2)
    ACTIVE_KEY_INDEX = (ACTIVE_KEY_INDEX + 1) % len(api_keys)
    # fallback to OpenRouter if available
    or_key = os.getenv("OPENROUTER_API_KEY")
    if or_key:
        try:
            payload["model"] = "google/gemini-2.0-flash-001"
            body2 = json.dumps(payload).encode("utf-8")
            conn = http.client.HTTPSConnection("openrouter.ai", timeout=REQUEST_TIMEOUT, context=SSL_CONTEXT)
            conn.request("POST", "/api/v1/chat/completions", body=body2, headers={
                "Content-Type": "application/json", "Accept": "application/json",
                "User-Agent": "ZeroxAI-Telegram-Bot/1.0",
                "Authorization": f"Bearer {or_key}",
                "HTTP-Referer": "https://zeroxaibot.fly.dev",
                "X-Title": "ZeroxAI",
            })
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8")
            conn.close()
            if resp.status == 200:
                data = json.loads(raw)
                return data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        except Exception as e:
            last_error = f"OpenRouter fallback: {e}"
    # fallback to Mistral
    try:
        return call_mistral(messages, "mistral-large-latest")
    except Exception as e:
        last_error = f"Mistral fallback: {e}"
    return f"Не удалось получить ответ: {last_error}"


def extract_error_message(raw):
    try:
        data = json.loads(raw)
        error = data.get("error")
        if isinstance(error, dict):
            return error.get("message")
        if isinstance(error, str):
            return error
    except Exception:
        return raw[:300]
    return raw[:300]


def parse_code_blocks(text):
    pattern = r"```(\w[^`]*?)?\n(.*?)```"
    matches = re.findall(pattern, text, re.DOTALL)
    result = []
    for header, code in matches:
        header = (header or "").strip()
        lang = header.split()[0] if header else ""
        filename = ""
        fm = re.search(r'filename=(["\']?)([^"\' ]+)\1', header)
        if fm:
            filename = fm.group(2)
        result.append((lang, filename, code.strip()))
    return result


def has_code_blocks(text):
    if "```" not in text:
        return False
    blocks = parse_code_blocks(text)
    if not blocks:
        return False
    for lang, filename, code in blocks:
        if lang:
            return True
        if not code or len(code) < 10:
            continue
        if re.search(r"[{}();\[\]<>]|\b(function|class|def|if|for|while|import|echo|return|<?php)\b", code):
            return True
    return False


def get_file_extension(lang):
    ext = LANG_EXT.get(lang.lower())
    return ext if ext is not None else (f".{lang}" if lang else ".txt")


def create_project_zip(blocks):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, (lang, filename, code) in enumerate(blocks):
            if not filename:
                ext = get_file_extension(lang)
                filename = f"file_{i + 1}{ext}"
            zf.writestr(filename, code)
    return buf.getvalue()


def send_code_prompt(token, chat_id, reply_to_msg_id):
    payload = {
        "chat_id": chat_id,
        "text": "В ответе найден код. Как отправить?",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "\U0001F4C4 Файлом", "callback_data": "code_file"},
                {"text": "\U0001F4DD Текстом", "callback_data": "code_text"},
            ]]
        },
    }
    if reply_to_msg_id:
        payload["reply_to_message_id"] = reply_to_msg_id
    return telegram_request(token, "sendMessage", payload)


def send_document(token, chat_id, file_bytes, filename):
    return telegram_upload(
        token, "sendDocument", {"chat_id": str(chat_id)},
        "document", file_bytes, filename, "application/zip",
    )


def send_code_blocks_as_text(token, chat_id, blocks, reply_to_msg_id):
    for lang, filename, code in blocks:
        header = f"Файл: {filename}" if filename else (f"Язык: {lang}" if lang else "Код")
        text = f"{header}\n\n```\n{code}\n```"
        chunks = split_message(text)
        for chunk in chunks:
            telegram_request(token, "sendMessage", {
                "chat_id": chat_id, "text": chunk,
                "disable_web_page_preview": True,
                "reply_to_message_id": reply_to_msg_id,
            })


def handle_callback_query(token, callback_query):
    cq_id = callback_query.get("id")
    data = callback_query.get("data", "")
    msg = callback_query.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    msg_id = msg.get("message_id")
    from_user = callback_query.get("from", {})
    user_id = from_user.get("id")
    if not cq_id or not chat_id or not msg_id:
        return
    telegram_request(token, "answerCallbackQuery", {"callback_query_id": cq_id})

    if data == "menu_pro":
        if is_pro_user(user_id):
            text = "\u2B50\uFE0F У вас активна Pro-подписка! Осталось {} дн.".format(pro_days_left(user_id)) + "\nИспользуется ZeroxAI Pro."
        else:
            text = "\u274C У вас бесплатная версия ZeroxAI.\nКупите Pro: /buypro"
        telegram_request(token, "editMessageText", {
            "chat_id": chat_id, "message_id": msg_id, "text": text,
            "reply_markup": {
                "inline_keyboard": [[
                    {"text": "\u25C0 Назад", "callback_data": "menu_back"},
                ]]
            },
        })
        return

    if data == "menu_tokens":
        pro = is_pro_user(user_id)
        limit = PRO_TOKEN_LIMIT if pro else FREE_TOKEN_LIMIT
        period = PRO_PERIOD_HOURS if pro else FREE_PERIOD_HOURS
        remaining, hours_left = get_token_remaining(user_id)
        used = limit - remaining
        bar_len = 10
        filled = int(bar_len * used / limit) if limit else 0
        bar = "\u2588" * min(filled, bar_len) + "\u2591" * (bar_len - min(filled, bar_len))
        tier = "Pro" if pro else "Free"
        text = f"\U0001F916 Токены ({tier}):\n{bar}\n{used:,} / {limit:,} ({used * 100 // limit if limit else 0}%)\nВосстановление: {hours_left}h / {period}h"
        telegram_request(token, "editMessageText", {
            "chat_id": chat_id, "message_id": msg_id, "text": text,
            "reply_markup": {
                "inline_keyboard": [[
                    {"text": "\u25C0 Назад", "callback_data": "menu_back"},
                ]]
            },
        })
        return

    if data == "menu_back":
        text = "\U0001F916 ZeroxAI Bot — многофункциональный AI-ассистент и чат-менеджер.\nКоманды: /commands\nПросто напиши вопрос или задачу — я отвечу как AI."
        telegram_request(token, "editMessageText", {
            "chat_id": chat_id, "message_id": msg_id, "text": text,
            "reply_markup": {
                "inline_keyboard": [[
                    {"text": "\u2B50 Подписка", "callback_data": "menu_pro"},
                    {"text": "\U0001F916 Токены", "callback_data": "menu_tokens"},
                ]]
            },
        })
        return

    if data.startswith("convpage_"):
        try:
            page = int(data.split("_", 1)[1])
        except (ValueError, IndexError):
            return
        _send_conv_page(token, page, msg_id=msg_id)
        return

    # legacy code callback handling
    key = (chat_id, msg_id)
    blocks = CODE_STORE.pop(key, None)
    if not blocks:
        telegram_request(token, "editMessageText", {
            "chat_id": chat_id, "message_id": msg_id,
            "text": "Код больше не доступен. Отправьте новый запрос.",
        })
        return
    try:
        telegram_request(token, "editMessageText", {
            "chat_id": chat_id, "message_id": msg_id,
            "text": "\u2705 Отправляю..." if data == "code_file" else "\u2705 Отправляю текстом...",
            "reply_markup": None,
        })
    except Exception:
        pass
    if data == "code_file":
        send_document(token, chat_id, create_project_zip(blocks), "project.zip")
    elif data == "code_text":
        send_code_blocks_as_text(token, chat_id, blocks, msg_id)


COMMAND_PREFIXES = ("/", "!")  # commands can start with / or !

KNOWN_COMMANDS = {
    "/start", "/help", "/about", "/ping", "/id", "/myrole",
    "/team", "/lightlist", "/rules", "/commands", "/stats", "/report",
    "/warn", "/warns", "/unwarn",
    "/mute", "/unmute", "/kick", "/ban", "/unban",
    "/role", "/setrules",
    "/ticket", "/closeticket", "/feedback", "/announce", "/userinfo", "/support",
    "/clean", "/pin", "/unpin", "/slowmode", "/say", "/welcome", "/delete", "/banlist", "/shop",
    "/joke", "/coin", "/dice", "/roll", "/choose", "/8ball", "/hug", "/slap", "/quote", "/meme",
    "/free", "/promo", "/bal", "/balance", "/slot", "/allin",
    "/transfer", "/give", "/send",
    "/addcoin", "/addmoney", "/removecoin", "/removemoney",
    "/stopcasino", "/startcasino", "/stopbot", "/startbot", "/statbot", "/tokens",
    "/server", "/addsticker", "/mypro", "/buypro", "/top", "/ben", "/grantpro", "/luckset", "/resettokens", "/buy", "/info",
    "/hide", "/savehistory", "/answer",
    "/giveall", "/addcoin", "/testshop", "/logs", "/setsub",
    "/setlocalmodel", "/trainmodel",
}

def should_respond(message):
    global BOT_ID, BOT_USERNAME
    chat = message.get("chat", {})
    chat_type = chat.get("type", "private")
    if chat_type == "private":
        return True
    text = (message.get("text") or "").strip()
    entities = message.get("entities") or []

    if text.startswith("/zerox") or text.startswith("/start"):
        return True

    for entity in entities:
        if entity.get("type") == "mention":
            mention = text[entity.get("offset"):entity.get("offset") + entity.get("length")]
            if mention.lower() == f"@{BOT_USERNAME}".lower():
                return True
        if entity.get("type") == "bot_command":
            cmd = text[entity.get("offset"):entity.get("offset") + entity.get("length")]
            cmd_name = cmd.split("@")[0].lower()
            if cmd_name in KNOWN_COMMANDS or cmd in {"/zerox", "/zerox@ZeruxAibot"}:
                return True

    if text.startswith("/") and text.split()[0].lower() in KNOWN_COMMANDS:
        return True

    reply_to = message.get("reply_to_message")
    if reply_to and reply_to.get("from", {}).get("id") == BOT_ID:
        return True

    return False


def strip_mention(text):
    global BOT_USERNAME
    if not text:
        return text
    pattern = re.compile(rf"^\s*@{re.escape(BOT_USERNAME)}\s*", re.IGNORECASE)
    text = pattern.sub("", text).strip()
    text = re.sub(r"^\s*/zerox\s*", "", text).strip()
    return text


def handle_command(token, message, chat, user, chat_id, user_id, text):
    parts = text.lower().split()
    cmd = parts[0]
    args = text.split()[1:] if len(parts) > 1 else []
    cmd_text = text[len(parts[0]):].strip()

    is_group = chat.get("type") != "private"

    def reply(msg, pm=None, **extra):
        chunks = split_message(msg)
        first = True
        for chunk in chunks:
            payload = {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True}
            if pm:
                payload["parse_mode"] = pm
            if extra:
                payload.update(extra)
            if first and message.get("message_id"):
                payload["reply_to_message_id"] = message["message_id"]
                first = False
            telegram_request(token, "sendMessage", payload)

    def lvl():
        return get_user_level(chat_id, user_id)

    def require(level):
        if not has_level(chat_id, user_id, level):
            reply(f"\u26A0\ufe0f Недостаточно прав. Нужен уровень {level}.")
            return False
        return True

    # --- Public /hide (list available features) ---
    if cmd == "/hide" and len(args) < 2:
        reply(
            "\U0001F512 Панель управления\n\n"
            "\u2B07 Доступные функции:\n"
            "  \u2022 casino (\U0001F3B0) — казино\n"
            "  \u2022 shop (\U0001F6CD) — магазин\n"
            "  \u2022 ai (\U0001F916) — AI ответы\n"
            "  \u2022 rcon (\u2694) — RCON команды\n"
            "  \u2022 promo (\U0001F4F0) — /promo\n"
            "  \u2022 info (\u2139) — /info\n"
            "  \u2022 server (\U0001F5A5) — /server\n"
            "  \u2022 stats (\U0001F4CA) — /stats\n"
            "  \u2022 help (\u2753) — /help\n"
            "  \u2022 commands (\U0001F4CB) — /commands\n"
            "  \u2022 about (\u2139\uFE0F) — /about\n\n"
            "\u26A0\uFE0F Управление: /hide <функция> <True/False>"
        )
        return True

    try:
        # --- Owner commands (id 6734685656) ---
        if user_id == 6734685656:
            if cmd in ("/addcoin", "/addmoney"):
                target_ref = parse_user_ref(message, args)
                if not target_ref:
                    reply("Ответьте на сообщение или укажите @username/ID.")
                    return True
                def _parse_coin_amount(s):
                    s = s.replace(",", "").upper()
                    mult = {"K": 10**3, "M": 10**6, "B": 10**9, "T": 10**12, "Q": 10**15, "Qn": 10**18, "Sx": 10**21, "Sp": 10**24}
                    if s[-2:] in ("Qn", "Sx", "Sp"):
                        return int(float(s[:-2]) * mult[s[-2:]])
                    if s[-1] in mult:
                        return int(float(s[:-1]) * mult[s[-1]])
                    return int(s)
                try:
                    amount = _parse_coin_amount([a for a in args if any(c.isdigit() for c in a)][-1])
                except (IndexError, ValueError):
                    reply("Укажите сумму. Пример: /addcoin @username 14M или /addcoin @username 1000")
                    return True
                if amount <= 0:
                    reply("Сумма должна быть положительной.")
                    return True
                tid = target_ref if isinstance(target_ref, int) else resolve_username(token, target_ref, chat_id)
                if not tid:
                    reply("Пользователь не найден. Попросите его написать боту любое сообщение, затем повторите.")
                    return True
                add_balance(tid, amount)
                reply(f"\U0001F4B0 Добавлено {fmt_coin(amount)} монет. Баланс получателя: {fmt_coin(get_balance(tid))}")
                return True

            if cmd in ("/removecoin", "/removemoney"):
                target_ref = parse_user_ref(message, args)
                if not target_ref:
                    reply("Ответьте на сообщение или укажите @username/ID.")
                    return True
                try:
                    amount = int([a for a in args if a.lstrip("-").isdigit()][-1])
                except (IndexError, ValueError):
                    reply("Укажите сумму. Пример: /removecoin @username 500")
                    return True
                if amount <= 0:
                    reply("Сумма должна быть положительной.")
                    return True
                tid = target_ref if isinstance(target_ref, int) else resolve_username(token, target_ref, chat_id)
                if not tid:
                    reply("Пользователь не найден. Попросите его написать боту любое сообщение, затем повторите.")
                    return True
                add_balance(tid, -amount)
                reply(f"\U0001F4B0 Списано {fmt_coin(amount)} монет. Баланс получателя: {fmt_coin(get_balance(tid))}")
                return True

            if cmd == "/giveall":
                try:
                    amount = int(args[0]) if args else 1000000
                except ValueError:
                    reply("Сумма должна быть числом.")
                    return True
                if amount <= 0:
                    reply("Сумма должна быть положительной.")
                    return True
                with db_cursor() as cur:
                    cur.execute("SELECT DISTINCT user_id FROM users")
                    user_ids = [row[0] for row in cur.fetchall()]
                if not user_ids:
                    reply("Нет пользователей в БД.")
                    return True
                count = 0
                for uid in user_ids:
                    try:
                        add_balance(uid, amount)
                        count += 1
                    except Exception:
                        pass
                reply(f"\U0001F4B0 Выдано {fmt_coin(amount)} каждому из {count} пользователей.")
                return True

            if cmd == "/savehistory":
                count = sum(len(h) // 2 for h in USER_HISTORIES.values())
                save_histories_to_db()
                reply(f"\U0001F4BE Сохранено {count} диалогов из памяти в БД.")
                return True

            if cmd == "/setsub":
                if len(args) < 2:
                    reply("Использование: /setsub @username <30д/1ч/7д> pro/free")
                    return True
                target_ref = parse_user_ref(message, args)
                if not target_ref:
                    reply("Ответьте на сообщение или укажите @username/ID.")
                    return True
                tid = target_ref if isinstance(target_ref, int) else resolve_username(token, target_ref, chat_id)
                if not tid:
                    reply("Пользователь не найден. Попросите его написать боту любое сообщение, затем повторите.")
                    return True
                duration_str = args[1]
                sub_type = "pro"
                if len(args) >= 3:
                    sub_type = args[2].lower()
                if sub_type == "free":
                    remove_pro_user(tid)
                    reply(f"\u2705 Подписка пользователя удалена (free).")
                    return True
                m = re.match(r"^(\d+)\s*(ч|h|д|d|м|m)$", duration_str)
                if not m:
                    reply("Неверный формат времени. Пример: 30д, 1ч, 7д")
                    return True
                amount = int(m.group(1))
                unit = m.group(2)
                if unit in ("ч", "h"):
                    interval = f"INTERVAL '{amount} hours'"
                    label = f"{amount}ч"
                elif unit in ("д", "d"):
                    interval = f"INTERVAL '{amount} days'"
                    label = f"{amount}д"
                elif unit in ("м", "m"):
                    interval = f"INTERVAL '{amount} minutes'"
                    label = f"{amount}м"
                if amount <= 0:
                    remove_pro_user(tid)
                    reply(f"\u2705 Подписка пользователя удалена (free).")
                    return True
                set_pro_user(tid, interval)
                reply(f"\U0001F4E1 Выдана подписка <b>PRO</b> на {label}.")
                return True

            if cmd == "/setlocalmodel":
                global _LOCAL_PRO_MODE
                if len(args) < 1 or args[0] not in ("on", "off"):
                    status = "\u2705 включен" if _LOCAL_PRO_MODE else "\u274C выключен"
                    reply(f"\U0001F4E1 Локальная модель: {status}.\nИспользование: /setlocalmodel on/off")
                    return True
                _LOCAL_PRO_MODE = (args[0] == "on")
                reply(f"\u2705 Локальная модель {'включена' if _LOCAL_PRO_MODE else 'выключена'}. "
                      f"Pro-юзерам будет {'Qwen2.5-Coder (локально)' if _LOCAL_PRO_MODE else 'GPT-OSS 120B (Groq)'}. "
                      f"Если локальная модель недоступна — авто-fallback на Groq.")
                return True

            if cmd == "/trainmodel":
                reply("\U0001F4E6 Обучение модели — команда для терминала на ноутбуке.\n\n"
                      "1. Создай Modelfile с примерами PHP-кода:\n"
                      "```\nFROM qwen2.5-coder:14b-instruct\n\n"
                      "SYSTEM \"Ты эксперт по PHP. Пиши чистый код.\"\n\n"
                      "MESSAGE user \"Напиши функцию для...\"\n"
                      "MESSAGE assistant \"<?php ...\"\n"
                      "```\n\n"
                      "2. Запусти обучение:\n"
                      "```\nollama create qwen2.5-coder-php -f Modelfile\n"
                      "ollama push qwen2.5-coder-php\n"
                      "```\n"
                      "3. Затем установи новую модель в боте через /setlocalmodel")
                return True

            if cmd == "/hide":
                if len(args) < 2:
                    reply(
                        "\U0001F512 Панель управления (владелец)\n\n"
                        "\u2B07 Доступные функции:\n"
                        "  \u2022 casino (\U0001F3B0) — казино\n"
                        "  \u2022 shop (\U0001F6CD) — магазин\n"
                        "  \u2022 ai (\U0001F916) — AI ответы\n"
                        "  \u2022 rcon (\u2694) — RCON команды\n"
                        "  \u2022 promo (\U0001F4F0) — /promo\n"
                        "  \u2022 info (\u2139) — /info\n"
                        "  \u2022 server (\U0001F5A5) — /server\n"
                        "  \u2022 stats (\U0001F4CA) — /stats\n"
                        "  \u2022 help (\u2753) — /help\n"
                        "  \u2022 commands (\U0001F4CB) — /commands\n"
                        "  \u2022 about (\u2139\uFE0F) — /about\n\n"
                        "\u26A0\uFE0F Управление: /hide <функция> <True/False>"
                    )
                    return True
                feature = args[0].lower()
                value = args[1].lower()
                if value not in ("true", "false", "1", "0", "yes", "no"):
                    reply("\u274C Использование: /hide <функция> <True/False>")
                    return True
                bool_val = value in ("true", "1", "yes")
                BOT_DATA.setdefault("hidden", {})[feature] = bool_val
                save_data()
                status = "\u2705 скрыто" if bool_val else "\u274C видно"
                reply(f"\U0001F512 '{feature}' {status}.")
                return True

            if cmd == "/setwebhook":
                if set_webhook(token):
                    reply("\u2705 Webhook переустановлен.")
                else:
                    reply("\u274C Не удалось установить webhook (бота нет на Railway/Fly/Render).")
                return True

            if cmd == "/stopcasino":
                BOT_DATA["casino_disabled"] = True
                save_data()
                reply("\u26D4 Казино остановлено.")
                return True

            if cmd == "/startcasino":
                BOT_DATA["casino_disabled"] = False
                save_data()
                reply("\U0001F3B0 Казино запущено.")
                return True

            if cmd == "/stopbot":
                BOT_DATA["bot_stopped"] = True
                save_data()
                reply("\U0001F634 Бот остановлен. /startbot — запустить снова.")
                return True

            if cmd == "/startbot":
                BOT_DATA["bot_stopped"] = False
                save_data()
                reply("\U0001F916 Бот запущен.")
                return True

            if cmd == "/grantpro":
                target_ref = parse_user_ref(message, args)
                if not target_ref and args:
                    target_ref = args[0]
                if target_ref:
                    tid = target_ref if isinstance(target_ref, int) else resolve_username(token, target_ref, chat_id)
                    if not tid:
                        reply("Пользователь не найден. Попросите его написать боту любое сообщение, затем повторите.")
                        return True
                    add_pro_user(tid)
                    reply(f"\u2B50\uFE0F Pro подписка выдана пользователю ID {tid} на 30 дней!")
                else:
                    add_pro_user(user_id)
                    reply("\u2B50\uFE0F Вам выдана Pro подписка на 30 дней!")
                return True

            if cmd == "/luckset":
                target_ref = parse_user_ref(message, args)
                if not target_ref:
                    reply("Ответьте на сообщение или укажите @username/ID.")
                    return True
                try:
                    luck_val = int([a for a in args if a.lstrip("-").isdigit()][-1])
                except (IndexError, ValueError):
                    reply("Укажите значение удачи (0-100). Пример: /luckset @username 50")
                    return True
                luck_val = max(0, min(100, luck_val))
                tid = target_ref if isinstance(target_ref, int) else resolve_username(token, target_ref, chat_id)
                if not tid:
                    reply("Пользователь не найден. Попросите его написать боту любое сообщение, затем повторите.")
                    return True
                set_luck(tid, luck_val)
                reply(f"\U0001F340 Удача для ID {tid} установлена на {luck_val}%")
                return True

            if cmd == "/resettokens":
                try:
                    with db_cursor() as cur:
                        cur.execute("DELETE FROM user_tokens")
                    reply("\U0001F504 Токены сброшены для всех пользователей!")
                except Exception as e:
                    reply(f"\u274C Ошибка: {e}")
                return True

            if cmd == "/statbot":
                total_users = 0
                total_coins = 0
                try:
                    with db_cursor() as cur:
                        cur.execute("SELECT COUNT(*), SUM(balance) FROM users WHERE balance > 0")
                        total_users, total_coins = cur.fetchone()
                except Exception as e:
                    print(f"Failed to get bot stats from DB: {e}", file=sys.stderr)

                chats = set()
                for k in USER_HISTORIES:
                    chats.add(k)
                reply(
                    f"\U0001F4CA Статистика:\n"
                    f"Пользователей с балансом: {total_users or 0}\n"
                    f"Всего монет в обращении: {fmt_coin(total_coins or 0)}\n"
                    f"Активных чатов: {len(chats)}\n"
                    f"Казино: {'✅' if not BOT_DATA.get('casino_disabled') else '⛔'}"
                )
                return True

            if cmd == "/top":
                try:
                    with db_cursor() as cur:
                        cur.execute("SELECT user_id, balance FROM users WHERE balance > 0 ORDER BY balance DESC LIMIT 3")
                        rows = cur.fetchall()
                except Exception as e:
                    reply(f"Ошибка: {e}")
                    return True
                if not rows and not MESSAGE_COUNTS:
                    reply("Пока нет данных для топа.")
                    return True
                ranked_messages = []
                for uid, count in MESSAGE_COUNTS.items():
                    if count > 0:
                        ranked_messages.append((count, uid))
                ranked_messages.sort(reverse=True)
                message_rows = ranked_messages[:3]

                lines = ["🏆 <b>ТОП ИГРОКОВ ПО МОНЕТАМ</b>"]
                medals = ["🥇", "🥈", "🥉"]
                for i, (uid, bal) in enumerate(rows):
                    name = f"ID {uid}"
                    try:
                        info = telegram_request(token, "getChat", {"chat_id": uid})
                        if info and info.get("ok"):
                            u = info.get("result", {})
                            name = u.get("username") and f"@{u['username']}" or u.get("first_name", name)
                    except Exception:
                        pass
                    lines.append(f"{medals[i]} {name} — {fmt_coin(bal)} 🪙 Coin")

                lines.append("")
                lines.append("💬 <b>ТОП ПО СООБЩЕНИЯМ</b>")
                for i, (count, uid) in enumerate(message_rows):
                    name = f"ID {uid}"
                    try:
                        info = telegram_request(token, "getChat", {"chat_id": uid})
                        if info and info.get("ok"):
                            u = info.get("result", {})
                            name = u.get("username") and f"@{u['username']}" or u.get("first_name", name)
                    except Exception:
                        pass
                    suffix = "сообщение" if count % 10 == 1 and count % 100 != 11 else "сообщений"
                    lines.append(f"{medals[i]} {name} — {count} {suffix} 💬")

                reply("\n".join(lines), "HTML")
                return True

            if cmd == "/testshop":
                if args and args[0] == "off":
                    _testshop_running = False
                    reply("\u2705 Тест магазина остановлен.")
                    return True
                if _testshop_running:
                    reply("\u26A0\uFE0F Тест уже запущен. /testshop off — остановить.")
                    return True
                _testshop_running = True
                reply("\U0001F3B0 Запуск теста магазина... /testshop off — остановить.")

                def _testshop_loop():
                    items = list(SHOP_ITEMS.keys())
                    while _testshop_running:
                        for item_id in items:
                            if not _testshop_running:
                                return
                            item = SHOP_ITEMS[item_id]
                            add_balance(user_id, item["price"])
                            with _user_item_lock(user_id):
                                user_items = get_user_items(user_id)
                                if item["type"] == "timed":
                                    if has_active_item(user_id, item_id):
                                        user_items[item_id]["expires_at"] += item["duration_min"] * 60
                                    else:
                                        user_items[item_id] = {"purchased_at": time.time(), "expires_at": time.time() + item["duration_min"] * 60}
                                else:
                                    if item_id in user_items:
                                        user_items[item_id] = {"qty": user_items[item_id].get("qty", 1) + 1}
                                    else:
                                        user_items[item_id] = {"qty": 1}
                                set_user_items(user_id, user_items)
                            send_message(token, chat_id, f"\u2705 Куплено {item['name']}")
                            import time as _ts
                            _ts.sleep(0.5)
                        # play slot a few times
                        for _ in range(3):
                            if not _testshop_running:
                                return
                            bal = get_balance(user_id)
                            bet = min(1000000, bal // 2)
                            if bet < 50:
                                add_balance(user_id, 10000000)
                                bet = 1000000
                            # simulate slot logic inline
                            _SLOT_SYMS = ["\u25AC", "\U0001F347", "\U0001F34B", "7\uFE0F\u20E3"]
                            with _user_item_lock(user_id):
                                u_items = get_user_items(user_id)
                                wp = u_items.get("jackpot_potion", {}).get("qty", 0) > 0
                                wp2 = u_items.get("luck_potion", {}).get("qty", 0) > 0
                                mp = has_active_item(user_id, "multiplier")
                                mult = 2 if mp else 1
                                if wp:
                                    u_items["jackpot_potion"]["qty"] -= 1
                                    if u_items["jackpot_potion"]["qty"] <= 0:
                                        del u_items["jackpot_potion"]
                                    set_user_items(user_id, u_items)
                                    s1 = s2 = s3 = _random.choice(_SLOT_SYMS)
                                    payout = bet * 10 * mult
                                elif wp2:
                                    u_items["luck_potion"]["qty"] -= 1
                                    if u_items["luck_potion"]["qty"] <= 0:
                                        del u_items["luck_potion"]
                                    set_user_items(user_id, u_items)
                                    win_sym = _random.choice(_SLOT_SYMS)
                                    s1 = _random.choice(_SLOT_SYMS)
                                    s2 = s3 = win_sym
                                    payout = bet * 2 * mult
                                else:
                                    s1 = _random.choice(_SLOT_SYMS)
                                    s2 = _random.choice(_SLOT_SYMS)
                                    s3 = _random.choice(_SLOT_SYMS)
                                    if s1 == s2 == s3:
                                        payout = bet * 10 * mult
                                    elif s1 == s2 or s2 == s3 or s1 == s3:
                                        payout = bet * 2 * mult
                                    else:
                                        payout = -bet
                                add_balance(user_id, payout)
                            send_message(token, chat_id, f"\U0001F3B0 {s1}{s2}{s3} {'+' if payout>0 else ''}{fmt_coin(payout)}")
                            _ts.sleep(0.8)
                    send_message(token, chat_id, "\U0001F6AB Тест магазина остановлен.")

                import threading as _test_th
                _test_th.Thread(target=_testshop_loop, daemon=True).start()
                return True

            if cmd == "/logs" and args:
                if args[0] == "image" and len(args) > 1 and args[1] == "off":
                    _SCANNING_IMAGES = False
                    reply("\u2705 Сканирование остановлено.")
                    return True
                if args[0] == "image":
                    reply("\U0001F4F8 Сканирую чаты в поиске фото... это может занять время. /logs image off — остановить.")
                    _scan_chats_for_images(token)
                    return True

        if cmd == "/buy":
            if not args:
                reply("Использование: /buy <id>\nСписок товаров: /shop")
                return True
            item_id = args[0].lower()
            item = SHOP_ITEMS.get(item_id)
            if not item:
                reply("❌ Такого предмета нет в магазине.")
                return True
            balance = get_balance(user_id)
            if balance < item['price']:
                reply(f"❌ Недостаточно монет. Нужно {fmt_coin(item['price'])}, у вас {fmt_coin(balance)}.")
                return True
            add_balance(user_id, -item['price'])
            with _user_item_lock(user_id):
                user_items = get_user_items(user_id)
                if item["type"] == "timed":
                    if has_active_item(user_id, item_id):
                        user_items[item_id]["expires_at"] += item["duration_min"] * 60
                    else:
                        user_items[item_id] = {
                            "purchased_at": time.time(),
                            "expires_at": time.time() + item["duration_min"] * 60
                        }
                    set_user_items(user_id, user_items)
                    reply(f"✅ Куплено <b>{item['name']}</b>! Длится {format_duration(item['duration_min'])}.\nБаланс: {fmt_coin(get_balance(user_id))}.", "HTML")
                elif item["type"] == "single":
                    if item_id in user_items:
                        user_items[item_id] = {"qty": user_items[item_id].get("qty", 1) + 1}
                    else:
                        user_items[item_id] = {"qty": 1}
                    set_user_items(user_id, user_items)
                    reply(f"✅ Куплено <b>{item['name']}</b>! (x{user_items[item_id]['qty']})\nБаланс: {fmt_coin(get_balance(user_id))}.", "HTML")
            return True

        if cmd == "/tokens":
            pro = is_pro_user(user_id)
            if pro:
                limit = PRO_TOKEN_LIMIT
                remaining, hours_left = get_token_remaining(user_id)
                bar_len = 10
                used = limit - remaining
                filled = int(bar_len * used / limit) if limit else 0
                bar = "\u2588" * min(filled, bar_len) + "\u2591" * (bar_len - min(filled, bar_len))
                reply(
                    f"\U0001F916 Токены (Pro):\n"
                    f"{bar}\n"
                    f"{used:,} / {limit:,} ({used * 100 // limit if limit else 0}%)\n"
                    f"Восстановление: {hours_left}h / {PRO_PERIOD_HOURS}h"
                )
            else:
                limit = FREE_TOKEN_LIMIT
                remaining, hours_left = get_token_remaining(user_id)
                bar_len = 10
                used = limit - remaining
                filled = int(bar_len * used / limit) if limit else 0
                bar = "\u2588" * min(filled, bar_len) + "\u2591" * (bar_len - min(filled, bar_len))
                reply(
                    f"\U0001F916 Токены (Free):\n"
                    f"{bar}\n"
                    f"{used:,} / {limit:,} ({used * 100 // limit if limit else 0}%)\n"
                    f"Восстановление: {hours_left}h / {FREE_PERIOD_HOURS}h"
                )
            return True

        if cmd == "/server":
            if len(args) >= 1 and args[0] == "off":
                if chat_id in RCON_SERVERS:
                    del RCON_SERVERS[chat_id]
                    reply("\u274C Режим консоли Minecraft выключен.")
                else:
                    reply("\u26A0\uFE0F Режим консоли не активен.")
                return True
            if len(args) < 3:
                reply("Использование: /server <host> <port> <password>\n"
                      "Все сообщения после подключения уходят в RCON.\n"
                      "/server off — выйти из режима консоли.")
                return True
            host = args[0]
            try:
                port = int(args[1])
            except ValueError:
                reply("Порт должен быть числом.")
                return True
            password = args[2]
            resp, err = rcon_command(host, port, password, "list")
            if err:
                reply(f"\u274C Ошибка подключения: {err}")
                return True
            RCON_SERVERS[chat_id] = {"host": host, "port": port, "password": password}
            reply(f"\u2705 Подключено к {host}:{port}\n"
                  f"Ответ на /list:\n{resp}\n"
                  f"Все сообщения теперь уходят в консоль. /server off — выйти.")
            return True

        # --- Casino disabled check ---
        if BOT_DATA.get("casino_disabled") and cmd in ("/coin", "/dice", "/slot", "/allin"):
            reply("\u26D4 Казино временно остановлено.")
            return True

        # --- Public commands (level 1+) ---

        if cmd == "/top":
            try:
                with db_cursor() as cur:
                    cur.execute("SELECT user_id, balance FROM users WHERE balance > 0 ORDER BY balance DESC LIMIT 3")
                    rows = cur.fetchall()
            except Exception as e:
                reply(f"Ошибка: {e}")
                return True
            if not rows:
                reply("Нет пользователей с монетами.")
                return True
            lines = ["\U0001F3C6 <b>ТОП ПО МОНЕТАМ</b>"]
            medals = ["\U0001F947", "\U0001F948", "\U0001F949"]
            for i, (uid, bal) in enumerate(rows):
                name = f"ID {uid}"
                try:
                    info = telegram_request(token, "getChat", {"chat_id": uid})
                    if info and info.get("ok"):
                        u = info.get("result", {})
                        name = u.get("username") and f"@{u['username']}" or u.get("first_name", name)
                except Exception:
                    pass
                lines.append(f"{medals[i]} {name} — {fmt_coin(bal)} Coin")
            reply("\n".join(lines), "HTML")
            return True

        if cmd in ("/start", "/help"):
            reply(
                "\U0001F916 ZeroxAI Bot — многофункциональный AI-ассистент и чат-менеджер.\n"
                f"Команды: /commands\n"
                "Просто напиши вопрос или задачу — я отвечу как AI.",
                reply_markup={
                    "keyboard": [
                        [{"text": "\u2B50 Подписка"}, {"text": "\U0001F916 Токены"}],
                    ],
                    "resize_keyboard": True,
                }
            )
            return True

        if cmd == "/about":
            reply("ZeroxAI Bot v2.0 — AI-ассистент + управление чатом.\n"
                  "Создатель: Эрик Арутюнян.\n"
                  "\u2705 Бесплатная версия: ZeroxAI Free\n"
                   "\u2B50 Pro: ZeroxAI Pro (мощнее)")
            return True

        if cmd == "/mypro":
            if is_pro_user(user_id):
                days = pro_days_left(user_id)
                reply(f"\u2B50\uFE0F У вас активна Pro-подписка! Осталось {days} дн.\nИспользуется ZeroxAI Pro.")
            else:
                reply("\u274C У вас бесплатная версия ZeroxAI.\n"
                      "Купите Pro: /buypro")
            return True

        if cmd == "/info":
            target_id = user_id
            target_user = user
            reply_msg = message.get("reply_to_message")
            if reply_msg:
                target_user = reply_msg.get("from", {})
                target_id = target_user.get("id", user_id)
            elif args:
                arg = args[0]
                if arg.startswith("@"):
                    username = arg[1:]
                    try:
                        chat_info = telegram_request(token, "getChat", {"chat_id": f"@{username}"})
                        if chat_info and "id" in chat_info:
                            target_id = chat_info["id"]
                            target_user = chat_info
                    except Exception:
                        reply(f"❌ Пользователь @{username} не найден.")
                        return True
                elif arg.isdigit():
                    target_id = int(arg)
            try:
                with db_cursor() as cur:
                    cur.execute("SELECT balance, max_balance, created_at FROM users WHERE user_id = %s", (target_id,))
                    row = cur.fetchone()
            except Exception:
                row = None
            u = target_user or {}
            fname = u.get("first_name", "")
            lname = u.get("last_name", "")
            uname = u.get("username", "")
            full_name = f"{fname} {lname}".strip() or "No name"
            bal = get_balance(target_id)
            max_bal = row[1] if row else bal
            created = row[2] if row else None
            pro = is_pro_user(target_id)
            pro_days = str(pro_days_left(target_id)) if pro else ""
            msg_count = MESSAGE_COUNTS.get(target_id, 0)
            if created:
                delta = datetime.datetime.now(datetime.timezone.utc) - created
                reg_days = delta.days
                reg_hours = delta.seconds // 3600
                reg_str = f"{created.strftime('%d.%m.%Y %H:%M')} ({reg_days}д {reg_hours}ч назад)"
            else:
                reg_str = "неизвестно"
            lines = [
                f"👤 <b>Информация о пользователе</b>",
                f"Имя: {full_name}",
                f"Username: @{uname}" if uname else "",
                f"ID: <code>{target_id}</code>",
                f"",
                f"💰 Баланс: {fmt_coin(bal)}",
                f"🏆 Рекорд баланса: {fmt_coin(max_bal)}",
                f"⭐ Подписка: {'Pro (' + pro_days + ' дн.)' if pro else 'Free'}",
                f"💐 Всего сообщений: {msg_count:,}",
                f"📅 Зарегистрирован: {reg_str}",
            ]
            reply("\n".join([l for l in lines if l]), "HTML")
            return True

        if cmd == "/buypro":
            if is_pro_user(user_id):
                reply("\u2B50\uFE0F У вас уже есть Pro-подписка!")
                return True
            price_stars = 100
            result = telegram_request(token, "sendInvoice", {
                "chat_id": chat_id,
                "title": "\u2B50 ZeroxAI Pro",
                "description": (
                    "\u2714\uFE0F Доступ к мощной модели ZeroxAI Pro\n"
                    "\u2714\uFE0F Более умные и развёрнутые ответы\n"
                    "\u2714\uFE0F Приоритетная обработка запросов\n"
                    "\u2714\uFE0F На 30 дней — продлевается раз в месяц"
                ),
                "payload": f"pro_{user_id}",
                "provider_token": "",
                "currency": "XTR",
                "prices": [{"label": "\u2B50 ZeroxAI Pro", "amount": price_stars}],
            })
            if not result.get("ok"):
                reply(f"\u274C Ошибка: {result.get('description', 'неизвестно')}")
            return True

        if cmd == "/ping":
            reply("\U0001F7E2 Понг! Бот работает.")
            return True

        if cmd == "/id":
            txt = f"\U0001F194 ID чата: `{chat_id}`\n\U0001F464 Ваш ID: `{user_id}`"
            if is_group:
                txt += f"\n\U0001F465 Тип: {chat.get('type', 'группа')}"
            reply(txt)
            return True

        if cmd == "/myrole":
            role = get_role_name(chat_id, user_id)
            level = lvl()
            if role:
                reply(f"\U0001F3F7 Ваша роль: {role} (уровень {level})")
            else:
                reply(f"\U0001F3F7 У вас нет роли. Уровень доступа: {level}")
            return True

        if cmd == "/team":
            cd = get_chat_data(chat_id)
            roles = cd.get("roles", {})
            users = cd.get("users", {})
            if not users:
                reply("Нет назначенных ролей.")
                return True
            lines = ["\U0001F465 Команда чата:"]
            sorted_roles = sorted(roles.items(), key=lambda x: -x[1])
            for rname, rlevel in sorted_roles:
                members = [uid for uid, r in users.items() if r == rname]
                if members:
                    try:
                        names = []
                        for uid in members[:5]:
                            try:
                                uinfo = telegram_request(token, "getChatMember", {"chat_id": chat_id, "user_id": int(uid)})
                                u = uinfo.get("result", {}).get("user", {})
                                names.append(get_user_display(u))
                            except Exception:
                                names.append(f"id{uid}")
                        lines.append(f"{rname} (ур.{rlevel}): {', '.join(names)}")
                    except Exception:
                        pass
            reply("\n".join(lines))
            return True

        if cmd == "/lightlist":
            lines = ["\u26A1\uFE0F Уровни доступа (Lightlist):"]
            for l, name in sorted(LEVEL_NAMES.items()):
                cmds = LEVEL_COMMANDS.get(l, [])
                cmd_str = " " + ", ".join(cmds) if cmds else ""
                lines.append(f"{l}. {name}{cmd_str}")
            lines.append("")
            lines.append("Уровень 10 (\u26A1\uFE0F Молния) — полный доступ")
            lines.append("Уровень 11 (\U0001F6E1\uFE0F Тех поддержка) — поддержка пользователей")
            reply("\n".join(lines))
            return True

        if cmd == "/rules":
            cd = get_chat_data(chat_id)
            rules = cd.get("rules", "")
            if rules:
                reply(f"\U0001F4D6 Правила чата:\n{rules}")
            else:
                reply("Правила не установлены. /setrules <текст> — установить.")
            return True

        if cmd == "/commands":
            lines = ["\U0001F4CB Команды ZeroxAI Bot:",
                     "",
                     "Доступны всем (уровень 1):",
                     "/start /help — помощь",
                     "/about — о боте",
                     "/ping — проверка",
                     "/id — ID чата/пользователя",
                     "/myrole — моя роль",
                     "/team — команда чата",
                     "/lightlist — уровни доступа",
                     "/rules — правила",
                     "/stats — статистика",
                     "/report — жалоба",
                     "/feedback — отзыв",
                     "/support — написать в техподдержку",
                     "",
                     "\U0001F389 Развлечения:",
                     "/joke — анекдот",
                     "/coin — орёл/решка",
                     "/dice — бросить кубик",
                     "/roll [N] — случайное число",
                     "/choose A | B — выбор",
                      "/8ball — шар судьбы",
                      "/hug — обнять",
                      "/slap — шлёпнуть",
                     "/quote — цитата",
                     "/meme — мем",
                     "",
                     "\U0001F4B0 Экономика и казино:",
                     "/free — получить бонус",
                     "/promo <code> — активировать промокод",
                     "/bal — баланс",
                     "/coin орёл/решка <ставка> — монетка",
                     "/dice <число> <ставка> — кубик (x5)",
                      "/slot [ставка] — слоты",
                      "/allin — ва-банк (вся ставка)",
                      "/shop — магазин предметов",
                      "/buy <id> — купить предмет",
                     "",
                     "\U0001F7E9 Уровень 5 (Помощник+):",
                     "/warn — предупреждение",
                     "/warns — список предупреждений",
                     "/unwarn — снять предупреждение",
                     "",
                     "\U0001F7E9 Уровень 6 (Модератор+):",
                     "/mute — заглушить",
                     "/unmute — разглушить",
                     "/kick — кикнуть",
                     "/ban — заблокировать",
                     "/unban — разблокировать",
                     "",
                     "\U0001F7E6 Уровень 8 (Админ+):",
                     "/role add — создать роль",
                     "/role remove — удалить роль",
                     "/role give — выдать роль",
                     "/role take — забрать роль",
                     "/role list — список ролей",
                     "/role info — информация о роли",
                     "/setrules — установить правила",
                     "",
                     "\U0001F6E1\uFE0F Уровень 10-11 (Техподдержка+):",
                     "/userinfo — информация о пользователе",
                     "/announce — объявление в чат",
                     "/clean — удалить сообщения",
                     "/pin — закрепить сообщение",
                     "/unpin — открепить",
                     "/slowmode — медленный режим",
                     "/say — написать от имени бота",
                     "/welcome — приветствие новичков",
                     "/delete — удалить сообщение",
                     "/banlist — список забаненных",
                     "/ticket — создать тикет",
                     "/closeticket — закрыть тикет",
                     "",
                     "\u26A1\uFE0F Уровень 10 (Молния): полный доступ",
                     "",
                     "AI: просто напишите вопрос — ZeroxAI ответит."]
            reply("\n".join(lines))
            return True

        if cmd == "/stats":
            reply(f"Статистика пока недоступна.")
            return True

        # --- Fun commands ---

        if cmd == "/joke":
            jokes = [
                "Шёл медведь по лесу, видит — машина горит. Сел в неё и сгорел.",
                "— Доктор, у меня глисты. — Не волнуйтесь, это не заразно. — Доктор, я таракан.",
                "Колобок повесился. Шутка старая, как мир, но колобку уже всё равно.",
                "Встречаются два программиста: — Что-то ты грустный. — Да вот, вчера написал код без единого бага. — И что? — Сегодня пришлось уволиться — не моё это.",
                "— Алло, это служба поддержки? — Да. — У меня клавиатура сломалась. — А что с ней? — Кнопка «Enter» не работает. — А вы пробовали нажать другую? — Да, я пробовал все. — И что? — Ничего. — А вы пробовали нажать Enter? — ... (гудки)",
                "Пошли как-то русский, немец и американец по мосту. Идут, идут... Короче, мост длинный оказался.",
                "Учитель: — Петров, почему ты опоздал? — Я переходил дорогу, когда увидел знак «Дети» и пошёл искать детей, чтобы предупредить об опасности.",
                "Купил мужик шляпу, а она ему как раз.",
            ]
            reply(f"\U0001F92A Анекдот:\n{_random.choice(jokes)}")
            return True


        if cmd == "/roll":
            max_val = 100
            for arg in args:
                if arg.isdigit() and int(arg) > 0:
                    max_val = int(arg)
                    break
            result = _random.randint(1, max_val)
            reply(f"\U0001F3AF Случайное число: **{result}** (1-{max_val})")
            return True

        if cmd == "/choose":
            if len(args) < 2:
                reply("Использование: /choose вариант1 | вариант2 | вариант3 ...")
                return True
            choices = [c.strip() for c in " ".join(args).split("|") if c.strip()]
            if len(choices) < 2:
                reply("Нужно хотя бы 2 варианта через |")
                return True
            reply(f"\U0001F3B1 Я выбираю: **{_random.choice(choices)}**")
            return True

        if cmd == "/8ball":
            answers = [
                "Бесспорно", "Предрешено", "Никаких сомнений", "Определённо да",
                "Можешь быть уверен в этом", "Мне кажется — да", "Вероятнее всего",
                "Хорошие перспективы", "Знаки говорят — да", "Да",
                "Пока не ясно, попробуй снова", "Спроси позже",
                "Лучше не рассказывать", "Сейчас нельзя предсказать",
                "Сконцентрируйся и спроси опять",
                "Даже не думай", "Мой ответ — нет", "По моим данным — нет",
                "Перспективы не очень хорошие", "Весьма сомнительно",
            ]
            question = cmd_text or "вопрос"
            reply(f"\U0001F3B2 Шар судьбы:\n«{question}»\n\n**{_random.choice(answers)}**")
            return True

        if cmd == "/hug":
            target = parse_user_ref(message, args)
            if target:
                reply(f"\U0001F917 {user.get('first_name', 'Кто-то')} обнимает {target}! \u2764\uFE0F")
            else:
                reply(f"\U0001F917 {user.get('first_name', 'Кто-то')} обнимает всех! \u2764\uFE0F")
            return True

        if cmd == "/slap":
            target = parse_user_ref(message, args)
            if not target:
                reply("Ответьте на сообщение или укажите @username.")
                return True
            slaps = [
                "\U0001F44A {user} даёт {target} пощёчину!",
                "\U0001F4A5 {user} шлёпает {target}!",
                "\U0001F4A2 {user} отвешивает {target} подзатыльник!",
                "\U0001F43E {user} кидает {target} тапком!",
            ]
            reply(_random.choice(slaps).format(user=user.get('first_name', 'Кто-то'), target=target))
            return True

        if cmd == "/quote":
            quotes = [
                "«Лучше программировать 4 часа и думать 8, чем программировать 8 и не думать вовсе.» — Аноним",
                "«Если отладка — процесс удаления багов, то программирование — процесс их внесения.» — Эдсгер Дейкстра",
                "«Работает — не трогай.» — Народная мудрость",
                "«Я не волшебник, я только учусь.» — Кот Леопольд",
                "«Знание — сила.» — Фрэнсис Бэкон",
                "«Повторение — мать учения.» — Народная мудрость",
                "«Не ошибается только тот, кто ничего не делает.» — Теодор Рузвельт",
                "«Всё гениальное — просто.» — Народная мудрость",
                "«Код как юмор. Если его нужно объяснять — он плохой.» — Аноним",
                "«Прежде чем писать код, подумай — а нужно ли это?» — Аноним",
            ]
            reply(f"\U0001F4AC {_random.choice(quotes)}")
            return True

        if cmd == "/meme":
            memes = [
                "\U0001F602 Когда код работает после первого запуска:",
                "\U0001F60E Я: напишу код за час. \nЯ через 5 часов: ну ещё чуть-чуть",
                "\U0001F480 Разработчик, который не комментирует код:",
                "\U0001F60A Когда баг оказался фичей:",
                "\U0001F628 Когда прод показывает ошибку 500:",
                "\U0001F92F Когда на код-ревью нашли 100 багов:",
                "\U0001F60D Когда продакт сказал «можно без изменений»:",
                "\U0001F624 Когда тестировщик говорит «у меня всё работает»:",
                "\U0001F44C Когда CI/CD проходит с первого раза:",
                "\U0001F47B Когда легаси-код без документации:",
                "\U0001F4A1 — В чём смысл жизни? \n— 42 \n— Что? \n— В дебагере поставь breakpoint на 43 и узнаешь.",
                "\U0001F913 ZeroxAI решает твою задачу, пока ты пьёшь кофе:",
            ]
            reply(_random.choice(memes))
            return True

        # --- Economy ---
        if cmd == "/free":
            can_claim, reason = can_claim_free(user_id)
            if not can_claim:
                reply(f"⏳ {reason}")
                return True
            add_balance(user_id, STARTING_BALANCE)
            set_claim(user_id, "free")
            reply(f"\U0001F4B0 Вы получили {fmt_coin(STARTING_BALANCE)} монет! Ваш баланс: {fmt_coin(get_balance(user_id))}.")
            return True

        if cmd == "/promo":
            if not args:
                reply("Использование: /promo <код>")
                return True
            code = args[0]
            success, result = redeem_promo(user_id, code)
            if success:
                reply(f"\U0001F389 Промокод активирован! Вы получили {fmt_coin(result)} монет. Ваш баланс: {fmt_coin(get_balance(user_id))}")
            else:
                reply(f"\u26A0\uFE0F {result}")
            return True

        if cmd in ("/bal", "/balance"):
            balance = get_balance(user_id)
            status_icon = ""
            if has_active_item(user_id, "vip_status"):
                status_icon = "💎 "
            elif has_active_item(user_id, "rich_status"):
                status_icon = "💰 "
            reply(f"{status_icon}\U0001F4B0 Ваш баланс: {fmt_coin(balance)} монет.")
            return True

        if cmd in ("/transfer", "/give", "/send"):
            target_ref, amount = parse_transfer_input(args, message)
            if not target_ref:
                reply("Ответьте на сообщение получателя: /transfer <сумма>\nИли укажите ID: /transfer <id> <сумма>")
                return True
            if amount is None:
                reply("Укажите сумму. Пример: /transfer <id> 100")
                return True
            if amount <= 0:
                reply("Сумма должна быть положительной.")
                return True
            if amount > MAX_TRANSFER_AMOUNT:
                reply(f"⚠️ Сумма слишком большая. Максимум: {fmt_coin(MAX_TRANSFER_AMOUNT)}")
                return True
            tid = None
            if isinstance(target_ref, int):
                tid = target_ref
            else:
                for ent in (message.get("entities") or []):
                    if ent.get("type") == "text_mention" and "user" in ent:
                        tid = ent["user"].get("id")
                        break
            if not tid:
                reply("Получатель не найден. Ответьте на сообщение получателя командой /transfer <сумма> или укажите ID.")
                return True
            if tid == user_id:
                reply("Нельзя перевести самому себе.")
                return True
            bal = get_balance(user_id)
            if amount > bal:
                reply(f"\u26A0\uFE0F Недостаточно монет. Баланс: {fmt_coin(bal)}")
                return True
            add_balance(user_id, -amount)
            add_balance(tid, amount)
            sender_name = user.get("first_name", str(user_id))
            reply(f"\U0001F4B0 Переведено {fmt_coin(amount)} монет. Ваш баланс: {fmt_coin(get_balance(user_id))}")
            try:
                telegram_request(token, "sendMessage", {
                    "chat_id": tid,
                    "text": f"\U0001F4B0 Вам переведено {fmt_coin(amount)} монет от {sender_name}.",
                })
            except Exception:
                pass
            return True

        if cmd == "/coin":
            if len(args) < 2:
                reply("Использование: /coin <орёл|решка> <ставка>\nПример: /coin орёл 100")
                return True
            side = args[0].lower()
            if side not in ("орёл", "орел", "решка", "reska"):
                reply("Выберите: орёл или решка")
                return True
            try:
                bet = int(args[1])
            except ValueError:
                reply("Ставка должна быть числом.")
                return True
            if bet <= 0:
                reply("Ставка должна быть положительной.")
                return True
            bal = get_balance(user_id)
            if bet > bal:
                reply(f"\u26A0\uFE0F Недостаточно монет. Баланс: {fmt_coin(bal)}")
                return True
            result = _random.choice(["орёл", "решка"])
            win = side in ("орёл", "орел") and result == "орёл" or side in ("решка", "reska") and result == "решка"
            if win:
                add_balance(user_id, bet)
                reply(f"\U0001FA99 Выпал: {result}! \U0001F389 Вы выиграли {fmt_coin(bet)} монет! Баланс: {fmt_coin(get_balance(user_id))}")
            else:
                add_balance(user_id, -bet)
                reply(f"\U0001FA99 Выпал: {result}. \u274C Вы проиграли {fmt_coin(bet)} монет. Баланс: {fmt_coin(get_balance(user_id))}")
            return True

        if cmd == "/dice":
            if len(args) < 2:
                reply("Использование: /dice <число 1-6> <ставка>\nПример: /dice 3 100")
                return True
            try:
                guess = int(args[0])
                bet = int(args[1])
            except ValueError:
                reply("Число и ставка должны быть числами.")
                return True
            if guess < 1 or guess > 6:
                reply("Число должно быть от 1 до 6.")
                return True
            if bet <= 0:
                reply("Ставка должна быть положительной.")
                return True
            bal = get_balance(user_id)
            if bet > bal:
                reply(f"\u26A0\uFE0F Недостаточно монет. Баланс: {fmt_coin(bal)}")
                return True
            result = _random.randint(1, 6)
            faces = {1: "\u2680", 2: "\u2681", 3: "\u2682", 4: "\u2683", 5: "\u2684", 6: "\u2685"}
            if result == guess:
                payout = bet * 5
                add_balance(user_id, payout)
                reply(f"\U0001F3B2 {faces[result]} Вы угадали! \U0001F389 Вы выиграли {fmt_coin(payout)} монет! Баланс: {fmt_coin(get_balance(user_id))}")
            else:
                add_balance(user_id, -bet)
                reply(f"\U0001F3B2 {faces[result]} Не угадали. \u274C Проиграно {fmt_coin(bet)} монет. Баланс: {fmt_coin(get_balance(user_id))}")
            return True

        if cmd == "/allin":
            args = [str(get_balance(user_id))]
            cmd = "/slot"
            # fall through to /slot

        if cmd == "/slot":
            bet = 50
            for arg in args:
                cleaned = arg.replace(",", "").replace(".", "")
                if cleaned.isdigit():
                    bet = int(cleaned)
                    break
            if bet <= 0:
                reply("Ставка должна быть положительной.")
                return True
            bal = get_balance(user_id)
            if bet > bal:
                reply(f"\u26A0\uFE0F Недостаточно монет. Баланс: {fmt_coin(bal)}")
                return True

            _SLOT_SYMS = ["\u25AC", "\U0001F347", "\U0001F34B", "7\uFE0F\u20E3"]

            def dice_symbols(dval):
                idx = dval - 1
                return (_SLOT_SYMS[idx // 16], _SLOT_SYMS[(idx % 16) // 4], _SLOT_SYMS[idx % 4])

            def send_dice_get_value():
                resp = telegram_request(token, "sendDice", {"chat_id": chat_id, "emoji": "\U0001F3B0"})
                try:
                    return resp["result"]["dice"]["value"], resp["result"].get("message_id")
                except Exception:
                    return 1, None

            def is_pair(s1, s2, s3):
                return s1 == s2 or s2 == s3 or s1 == s3

            def is_jackpot(s1, s2, s3):
                return s1 == s2 == s3

            # check for potions + multiplier (locked per user to avoid race with /buy)
            with _user_item_lock(user_id):
                user_items = get_user_items(user_id)
                want_jackpot = False
                want_pair = False
                potion_used = None

                if user_items.get("jackpot_potion", {}).get("qty", 0) > 0:
                    want_jackpot = True
                    potion_used = "jackpot_potion"
                elif user_items.get("luck_potion", {}).get("qty", 0) > 0:
                    want_pair = True
                    potion_used = "luck_potion"

                print(f"[SLOT] user={user_id} items={user_items} want_pair={want_pair} want_jackpot={want_jackpot} potion={potion_used}", flush=True)

                # check multiplier
                multiplier = 1
                if has_active_item(user_id, "multiplier"):
                    multiplier = 2

                # consume potion immediately (before roll)
                if potion_used:
                    user_items[potion_used]["qty"] -= 1
                    if user_items[potion_used]["qty"] <= 0:
                        del user_items[potion_used]
                    set_user_items(user_id, user_items)

            # force win if potion is active — skip dice loop, directly set result
            if want_jackpot:
                s1 = s2 = s3 = _random.choice(_SLOT_SYMS)
            elif want_pair:
                win_sym = _random.choice(_SLOT_SYMS)
                s1 = _random.choice(_SLOT_SYMS)
                s2 = s3 = win_sym
            else:
                dice_value, _ = send_dice_get_value()
                s1, s2, s3 = dice_symbols(dice_value)

            print(f"[SLOT] result s1={s1} s2={s2} s3={s3} is_pair={is_pair(s1,s2,s3)} is_jackpot={is_jackpot(s1,s2,s3)}", flush=True)

            time.sleep(1)

            if is_jackpot(s1, s2, s3):
                base = bet * 10
                payout = base * multiplier
                add_balance(user_id, payout)
                line = f"\U0001F4B0 Награда: {fmt_coin(base)}"
                if multiplier > 1:
                    line += f" x{multiplier} = {fmt_coin(payout)}"
                line += " Coin"
                result = (
                    f"\U0001F3B0 Выпало: {s1} {s2} {s3}\n"
                    f"\U0001F389 Поздравляем! <b>ДЖЕКПОТ!</b>\n\n"
                    f"{line}\n"
                    f"\u26A1 Баланс: {fmt_coin(get_balance(user_id))}"
                )
            elif is_pair(s1, s2, s3):
                base = bet * 2
                payout = base * multiplier
                add_balance(user_id, payout)
                line = f"\U0001F4B0 Награда: {fmt_coin(base)}"
                if multiplier > 1:
                    line += f" x{multiplier} = {fmt_coin(payout)}"
                line += " Coin"
                result = (
                    f"\U0001F3B0 Выпало: {s1} {s2} {s3}\n"
                    f"\U0001F389 Поздравляем! <b>ВЫИГРЫШ!</b>\n\n"
                    f"{line}\n"
                    f"\u26A1 Баланс: {fmt_coin(get_balance(user_id))}"
                )
            else:
                add_balance(user_id, -bet)
                result = (
                    f"\U0001F3B0 Выпало: {s1} {s2} {s3}\n"
                    f"\U0001F614 Проигрыш: -{fmt_coin(bet)} Coin\n"
                    f"\u26A1 Баланс: {fmt_coin(get_balance(user_id))}"
                )
            reply(result, "HTML")
            return True

        if cmd == "/shop":
            lines = ["🏪 <b>Магазин</b>", 'Купить: /buy [id]', ""]
            for item_id, item in SHOP_ITEMS.items():
                status = get_shop_status(user_id, item_id)
                duration = ""
                if item["type"] == "timed":
                    duration = f" ⏱ {format_duration(item['duration_min'])}"
                elif item["type"] == "single":
                    duration = " 🔄 1 spin"
                lines.append(
                    f"<b>{item['name']}</b>\n"
                    f"ID: <code>{item_id}</code> | Цена: {fmt_coin(item['price'])}{duration}\n"
                    f"{item['description']}{status}"
                )
                lines.append("")
            reply("\n".join(lines), "HTML")
            return True

        if cmd == "/ben":
            choice = _random.choice(["yes", "no"])
            fid = _BEN_FILES.get(choice)
            if fid:
                try:
                    telegram_request(token, "sendAnimation", {"chat_id": chat_id, "animation": fid})
                except Exception:
                    reply("Да" if choice == "yes" else "Нет")
            else:
                # fallback: send MP4 directly
                path = _BEN_PATHS.get(choice)
                if path and os.path.exists(path):
                    try:
                        with open(path, "rb") as f:
                            data = f.read()
                        resp = telegram_upload(token, "sendAnimation", {"chat_id": chat_id}, "animation", data, f"ben_{choice}.mp4", "video/mp4")
                        if resp and "result" in resp:
                            _BEN_FILES[choice] = resp["result"]["animation"]["file_id"]
                    except Exception:
                        reply("Да" if choice == "yes" else "Нет")
                else:
                    reply("Да" if choice == "yes" else "Нет")
            return True

        if cmd == "/addsticker":
            reply_msg = message.get("reply_to_message")
            if reply_msg and reply_msg.get("sticker"):
                fid = reply_msg["sticker"]["file_id"]
            elif args:
                fid = args[0]
            else:
                reply("Ответьте на стикер или отправьте file_id.")
                return True
            if fid not in STICKER_POOL:
                STICKER_POOL.append(fid)
            reply(f"\u2705 Стикер добавлен в пул ({len(STICKER_POOL)} шт.)")
            return True

        if cmd == "/setthinking":
            reply_msg = message.get("reply_to_message")
            if reply_msg and reply_msg.get("sticker"):
                fid = reply_msg["sticker"]["file_id"]
            elif args:
                fid = args[0]
            else:
                reply("Ответьте на стикер или отправьте file_id.")
                return True
            try:
                with db_cursor() as cur:
                    cur.execute("UPDATE users SET thinking_sticker = %s WHERE user_id = %s", (fid, user_id))
                    if cur.rowcount == 0:
                        cur.execute("INSERT INTO users (user_id, thinking_sticker) VALUES (%s, %s)", (user_id, fid))
                reply(f"\u2705 Стикер «думаю» сохран\u0451н!")
            except Exception as e:
                reply(f"\u274C Ошибка: {e}")
            return True

        if cmd == "/report":
            target = parse_user_ref(message, args)
            reason = cmd_text
            if target:
                reply(f"\U0001F6A8 Жалоба на {target} передана администрации.")
                cd = get_chat_data(chat_id)
                for uid, rname in cd.get("users", {}).items():
                    if cd.get("roles", {}).get(rname, 0) >= 5:
                        try:
                            telegram_request(token, "sendMessage", {
                                "chat_id": uid, "text": f"\U0001F6A8 Жалоба в чате {chat_id}:\n"
                                f"Пользователь: {target}\nПричина: {reason or 'не указана'}",
                            })
                        except Exception:
                            pass
            else:
                reply("Укажите пользователя (ответьте на сообщение или укажите ID/@username).")
            return True

        # --- Moderation (level 5+) ---
        if cmd == "/warn":
            if not require(5): return True
            target = parse_user_ref(message, args)
            if not target:
                reply("Укажите пользователя.")
                return True
            cd = get_chat_data(chat_id)
            sid = str(target)
            cd.setdefault("warns", {})
            cd["warns"][sid] = cd["warns"].get(sid, 0) + 1
            warns = cd["warns"][sid]
            save_data()
            reason = cmd_text
            reply(f"\u26A0\ufe0f Пользователь {target} предупреждён ({warns}/3).{f' Причина: {reason}' if reason else ''}")
            if warns >= 3:
                try:
                    telegram_request(token, "banChatMember", {"chat_id": chat_id, "user_id": target})
                    reply(f"\U0001F534 {target} забанен за 3 предупреждения.")
                except Exception:
                    pass
            return True

        if cmd == "/warns":
            if not require(5): return True
            target = parse_user_ref(message, args)
            cd = get_chat_data(chat_id)
            sid = str(target) if target else str(user_id)
            warns = cd.get("warns", {}).get(sid, 0)
            reply(f"Предупреждения: {warns}/3")
            return True

        if cmd == "/unwarn":
            if not require(5): return True
            target = parse_user_ref(message, args)
            cd = get_chat_data(chat_id)
            sid = str(target) if target else str(user_id)
            cd.setdefault("warns", {})
            cd["warns"][sid] = max(0, cd["warns"].get(sid, 1) - 1)
            save_data()
            reply(f"\u2705 Предупреждение снято. Текущие: {cd['warns'][sid]}/3")
            return True

        if cmd == "/mute":
            if not require(6): return True
            target = parse_user_ref(message, args)
            if not target:
                reply("Укажите пользователя.")
                return True
            minutes = 10
            for arg in args:
                if arg.isdigit():
                    minutes = int(arg)
                    break
            until = int(time.time()) + minutes * 60
            try:
                telegram_request(token, "restrictChatMember", {
                    "chat_id": chat_id, "user_id": target,
                    "permissions": {"can_send_messages": False},
                    "until_date": until,
                })
                cd = get_chat_data(chat_id)
                cd.setdefault("muted", {})
                cd["muted"][str(target)] = until
                save_data()
                reply(f"\U0001F507 {target} заглушён на {format_minutes_duration(minutes)}.")
            except Exception as e:
                reply(f"\u274C Ошибка: {e}")
            return True

        if cmd == "/unmute":
            if not require(6): return True
            target = parse_user_ref(message, args)
            if not target:
                reply("Укажите пользователя.")
                return True
            try:
                telegram_request(token, "restrictChatMember", {
                    "chat_id": chat_id, "user_id": target,
                    "permissions": {
                        "can_send_messages": True, "can_send_media_messages": True,
                        "can_send_polls": True, "can_send_other_messages": True,
                        "can_add_web_page_previews": True,
                    },
                })
                cd = get_chat_data(chat_id)
                cd.get("muted", {}).pop(str(target), None)
                save_data()
                reply(f"\U0001F50A {target} разглушён.")
            except Exception as e:
                reply(f"\u274C Ошибка: {e}")
            return True

        if cmd == "/kick":
            if not require(6): return True
            target = parse_user_ref(message, args)
            if not target:
                reply("Укажите пользователя.")
                return True
            try:
                telegram_request(token, "banChatMember", {"chat_id": chat_id, "user_id": target})
                telegram_request(token, "unbanChatMember", {"chat_id": chat_id, "user_id": target})
                reason = cmd_text
                reply(f"\U0001F4A2 {target} кикнут.{f' Причина: {reason}' if reason else ''}")
            except Exception as e:
                reply(f"\u274C Ошибка: {e}")
            return True

        if cmd == "/ban":
            if not require(6): return True
            target = parse_user_ref(message, args)
            if not target:
                reply("Укажите пользователя.")
                return True
            try:
                telegram_request(token, "banChatMember", {"chat_id": chat_id, "user_id": target})
                cd = get_chat_data(chat_id)
                cd.setdefault("banned", [])
                if str(target) not in cd["banned"]:
                    cd["banned"].append(str(target))
                save_data()
                reason = cmd_text
                reply(f"\U0001F534 {target} забанен.{f' Причина: {reason}' if reason else ''}")
            except Exception as e:
                reply(f"\u274C Ошибка: {e}")
            return True

        if cmd == "/unban":
            if not require(6): return True
            target = parse_user_ref(message, args)
            if not target:
                reply("Укажите пользователя.")
                return True
            try:
                telegram_request(token, "unbanChatMember", {"chat_id": chat_id, "user_id": target})
                cd = get_chat_data(chat_id)
                cd.setdefault("banned", [])
                cd["banned"] = [b for b in cd["banned"] if b != str(target)]
                save_data()
                reply(f"\u2705 {target} разбанен.")
            except Exception as e:
                reply(f"\u274C Ошибка: {e}")
            return True

        # --- Role management (level 8+) ---
        if cmd == "/role":
            sub = args[0] if args else ""
            if not sub:
                reply("Использование: /role add/remove/give/take/list/info")
                return True

            if sub == "add":
                if not require(8): return True
                if len(args) < 3:
                    reply("Использование: /role add <название> <уровень (1-11)>")
                    return True
                role_name = args[1]
                try:
                    role_level = int(args[2])
                except ValueError:
                    reply("Уровень должен быть числом от 1 до 11.")
                    return True
                if role_level < 1 or role_level > 11:
                    reply("Уровень должен быть от 1 до 10.")
                    return True
                cd = get_chat_data(chat_id)
                cd["roles"][role_name] = role_level
                save_data()
                reply(f"\u2705 Роль «{role_name}» создана (уровень {role_level}).")
                return True

            if sub == "remove":
                if not require(8): return True
                if len(args) < 2:
                    reply("Использование: /role remove <название>")
                    return True
                role_name = args[1]
                cd = get_chat_data(chat_id)
                if role_name not in cd["roles"]:
                    reply(f"Роль «{role_name}» не найдена.")
                    return True
                del cd["roles"][role_name]
                for uid, r in list(cd["users"].items()):
                    if r == role_name:
                        del cd["users"][uid]
                save_data()
                reply(f"\u2705 Роль «{role_name}» удалена.")
                return True

            if sub in ("give", "grant"):
                if not require(8): return True
                if len(args) < 3:
                    reply("Использование: /role give <пользователь> <роль>")
                    return True
                target = parse_user_ref(message, args[1:])
                if not target:
                    reply("Укажите пользователя.")
                    return True
                role_name = args[-1]
                cd = get_chat_data(chat_id)
                if role_name not in cd["roles"]:
                    reply(f"Роль «{role_name}» не найдена. Создайте её через /role add")
                    return True
                if has_level(chat_id, target, cd["roles"][role_name]):
                    pass
                cd["users"][str(target)] = role_name
                save_data()
                reply(f"\u2705 Пользователю {target} выдана роль «{role_name}».")
                return True

            if sub == "take":
                if not require(8): return True
                if len(args) < 3:
                    reply("Использование: /role take <пользователь> <роль>")
                    return True
                target = parse_user_ref(message, args[1:])
                if not target:
                    reply("Укажите пользователя.")
                    return True
                role_name = args[-1]
                cd = get_chat_data(chat_id)
                sid = str(target)
                if cd.get("users", {}).get(sid) != role_name:
                    reply(f"У пользователя {target} нет роли «{role_name}».")
                    return True
                del cd["users"][sid]
                save_data()
                reply(f"\u2705 У пользователя {target} забрана роль «{role_name}».")
                return True

            if sub == "list":
                cd = get_chat_data(chat_id)
                if not cd["roles"]:
                    reply("Нет созданных ролей.")
                    return True
                lines = ["\U0001F3F7 Список ролей:"]
                for rname, rlevel in sorted(cd["roles"].items(), key=lambda x: -x[1]):
                    count = sum(1 for r in cd["users"].values() if r == rname)
                    lines.append(f"{rname} — ур.{rlevel} ({count} чел.)")
                reply("\n".join(lines))
                return True

            if sub == "info":
                if len(args) < 2:
                    reply("Использование: /role info <название>")
                    return True
                role_name = args[1]
                cd = get_chat_data(chat_id)
                if role_name not in cd["roles"]:
                    reply(f"Роль «{role_name}» не найдена.")
                    return True
                rlevel = cd["roles"][role_name]
                members = [uid for uid, r in cd.get("users", {}).items() if r == role_name]
                lines = [
                    f"\U0001F3F7 Роль: {role_name}",
                    f"\u26A1 Уровень: {rlevel}",
                    f"\U0001F465 Участников: {len(members)}",
                ]
                if members:
                    try:
                        names = []
                        for uid in members[:10]:
                            try:
                                uinfo = telegram_request(token, "getChatMember", {"chat_id": chat_id, "user_id": int(uid)})
                                u = uinfo.get("result", {}).get("user", {})
                                names.append(get_user_display(u))
                            except Exception:
                                names.append(f"id{uid}")
                        lines.append(f"Участники: {', '.join(names)}")
                    except Exception:
                        pass
                reply("\n".join(lines))
                return True

            reply("Подкоманда не распознана. Используйте: add, remove, give, take, list, info")
            return True

        if cmd == "/setrules":
            if not require(8): return True
            cd = get_chat_data(chat_id)
            cd["rules"] = cmd_text
            save_data()
            reply("\u2705 Правила обновлены.")
            return True

        # --- Tech support commands (level 10+) ---
        def require_ts():
            if not has_level(chat_id, user_id, 10):
                reply("\u26A0\ufe0f Недостаточно прав.")
                return False
            return True

        if cmd == "/ticket":
            if not require_ts(): return True
            reply("\U0001F4E9 Тикет создан. Администрация рассмотрит ваш запрос.")
            return True

        if cmd == "/closeticket":
            if not require_ts(): return True
            reply("\U0001F4E6 Тикет закрыт.")
            return True

        if cmd == "/feedback":
            if not require(1): return True
            reply("\U0001F4AC Спасибо за отзыв! Он передан администрации.")
            return True

        if cmd == "/announce":
            if not require_ts(): return True
            text = cmd_text
            if not text:
                reply("Укажите текст объявления.")
                return True
            announce_text = f"\U0001F4E2 Объявление:\n{text}"
            try:
                telegram_request(token, "sendMessage", {
                    "chat_id": chat_id, "text": announce_text,
                    "disable_web_page_preview": True,
                })
            except Exception as e:
                reply(f"\u274C Ошибка: {e}")
            return True

        if cmd == "/userinfo":
            if not require_ts(): return True
            target = parse_user_ref(message, args)
            if not target:
                reply("Укажите пользователя (ответьте на сообщение или укажите ID/@username).")
                return True
            try:
                if isinstance(target, int):
                    uinfo = telegram_request(token, "getChatMember", {"chat_id": chat_id, "user_id": target})
                else:
                    uid = resolve_username(token, target)
                    if not uid:
                        reply("Пользователь не найден.")
                        return True
                    uinfo = telegram_request(token, "getChatMember", {"chat_id": chat_id, "user_id": uid})
                u = uinfo.get("result", {}).get("user", {})
                uid = u.get("id", target)
                uname = u.get("username", "")
                fname = u.get("first_name", "")
                lname = u.get("last_name", "")
                role = get_role_name(chat_id, uid)
                level = get_user_level(chat_id, uid)
                lines = [
                    f"\U0001F464 Пользователь: {fname} {lname or ''}".strip(),
                    f"ID: {uid}",
                ]
                if uname:
                    lines.append(f"@: @{uname}")
                lines.append(f"\U0001F3F7 Роль: {role or 'Нет'} (ур.{level})")
                reply("\n".join(lines))
            except Exception as e:
                reply(f"\u274C Ошибка: {e}")
            return True

        if cmd == "/support":
            if not require(1): return True
            text = cmd_text
            if not text:
                reply("Напишите ваш вопрос после /support. Например: /support У меня проблема с ботом")
                return True
            try:
                with db_cursor() as cur:
                    cur.execute(
                        "INSERT INTO tickets (user_id, chat_id, username, question) VALUES (%s, %s, %s, %s) RETURNING id",
                        (user_id, chat_id, user.get("username", ""), text)
                    )
                    ticket_id = cur.fetchone()[0]
            except Exception as e:
                print(f"Failed to save ticket: {e}", file=sys.stderr)
                reply("\u274C Ошибка при создании запроса. Попробуйте позже.")
                return True
            reply(f"\U0001F4E9 Ваш запрос №{ticket_id} отправлен в техподдержку. Ожидайте ответа.")
            cd = get_chat_data(chat_id)
            notified = False
            for uid, rname in cd.get("users", {}).items():
                if cd.get("roles", {}).get(rname, 0) >= 10:
                    try:
                        telegram_request(token, "sendMessage", {
                            "chat_id": uid,
                            "text": f"\U0001F6E1\uFE0F Запрос №{ticket_id} в техподдержку от @{user.get('username', user_id)} (чат {chat_id}):\n{text}\n\nОтветить: /answer {ticket_id} <текст>",
                        })
                        notified = True
                    except Exception:
                        pass
            if not notified:
                for target in ADMIN_TICKET_TARGETS:
                    try:
                        telegram_request(token, "sendMessage", {
                            "chat_id": target,
                            "text": f"\U0001F6E1\uFE0F Запрос №{ticket_id} в техподдержку от @{user.get('username', user_id)} (чат {chat_id}):\n{text}\n\nОтветить: /answer {ticket_id} <текст>",
                        })
                    except Exception:
                        pass
            return True

        if cmd == "/answer":
            if not require_ts(): return True
            if message.get("reply_to_message"):
                support_text = cmd_text
                if not support_text:
                    reply("Напишите ответ после /answer. Например: /answer Ваш ответ")
                    return True
                try:
                    with db_cursor() as cur:
                        cur.execute(
                            "SELECT id, user_id, chat_id, question, status FROM tickets WHERE status = 'open' ORDER BY id DESC LIMIT 1"
                        )
                        row = cur.fetchone()
                except Exception as e:
                    print(f"Failed to find ticket: {e}", file=sys.stderr)
                    row = None
                if not row:
                    reply("\u274C Нет открытых тикетов для ответа.")
                    return True
                ticket_id, t_user_id, t_chat_id, question, status = row
            elif args and args[0].isdigit():
                ticket_id = int(args[0])
                support_text = " ".join(args[1:]).strip()
                if not support_text:
                    reply("Напишите ответ после ID тикета. Пример: /answer 5 Ваш ответ")
                    return True
                try:
                    with db_cursor() as cur:
                        cur.execute(
                            "SELECT user_id, chat_id, question, status FROM tickets WHERE id = %s", (ticket_id,)
                        )
                        row = cur.fetchone()
                except Exception as e:
                    print(f"Failed to find ticket {ticket_id}: {e}", file=sys.stderr)
                    row = None
                if not row:
                    reply(f"\u274C Тикет №{ticket_id} не найден.")
                    return True
                t_user_id, t_chat_id, question, status = row
                if status != "open":
                    reply(f"\u274C Тикет №{ticket_id} уже закрыт.")
                    return True
            else:
                reply("Использование: /answer <ID тикета> <текст ответа> или ответьте на сообщение о тикете: /answer <текст>")
                return True
            try:
                with db_cursor() as cur:
                    cur.execute(
                        "UPDATE tickets SET status = 'answered', answer_text = %s, answered_by = %s, answered_at = NOW() WHERE id = %s AND status = 'open'",
                        (support_text, user_id, ticket_id)
                    )
            except Exception as e:
                print(f"Failed to update ticket {ticket_id}: {e}", file=sys.stderr)
                reply("\u274C Ошибка при отправке ответа.")
                return True
            try:
                mention = f"@{user.get('username')}" if user.get("username") else f"id{user_id}"
                telegram_request(token, "sendMessage", {
                    "chat_id": t_chat_id,
                    "text": f"\U0001F6E1\uFE0F Ответ техподдержки на ваш запрос №{ticket_id}:\n\n{support_text}",
                    "parse_mode": "HTML",
                })
                reply(f"\u2705 Ответ отправлен пользователю (тикет №{ticket_id}).")
            except Exception as e:
                reply(f"\u2705 Ответ сохранён, но не доставлен пользователю: {e}")
            return True

        if cmd == "/clean":
            if not require_ts(): return True
            count = 10
            for arg in args:
                if arg.isdigit():
                    count = min(int(arg), 100)
                    break
            try:
                msg_id = message.get("message_id")
                for i in range(count):
                    try:
                        telegram_request(token, "deleteMessage", {"chat_id": chat_id, "message_id": msg_id - i})
                    except Exception:
                        pass
                reply(f"\u2705 Удалено {count} сообщений.")
            except Exception as e:
                reply(f"\u274C Ошибка: {e}")
            return True

        if cmd == "/pin":
            if not require_ts(): return True
            reply_to = message.get("reply_to_message")
            if not reply_to:
                reply("Ответьте на сообщение, которое нужно закрепить.")
                return True
            try:
                telegram_request(token, "pinChatMessage", {
                    "chat_id": chat_id, "message_id": reply_to.get("message_id"),
                })
                reply("\U0001F4CC Сообщение закреплено.")
            except Exception as e:
                reply(f"\u274C Ошибка: {e}")
            return True

        if cmd == "/unpin":
            if not require_ts(): return True
            try:
                telegram_request(token, "unpinChatMessage", {"chat_id": chat_id})
                reply("\U0001F4CC Сообщение откреплено.")
            except Exception as e:
                reply(f"\u274C Ошибка: {e}")
            return True

        if cmd == "/slowmode":
            if not require_ts(): return True
            seconds = 5
            for arg in args:
                if arg.isdigit():
                    seconds = int(arg)
                    break
            try:
                telegram_request(token, "setChatPermissions", {
                    "chat_id": chat_id,
                    "permissions": {
                        "can_send_messages": True,
                        "can_send_media_messages": True,
                        "can_send_polls": True,
                        "can_send_other_messages": True,
                        "can_add_web_page_previews": True,
                    },
                })
                reply(f"\u23F1 Медленный режим установлен ({seconds} сек).")
            except Exception as e:
                reply(f"\u274C Ошибка: {e}")
            return True

        if cmd == "/say":
            if not require_ts(): return True
            text = cmd_text
            if not text:
                reply("Укажите текст.")
                return True
            try:
                telegram_request(token, "sendMessage", {
                    "chat_id": chat_id, "text": text,
                    "disable_web_page_preview": True,
                })
            except Exception as e:
                reply(f"\u274C Ошибка: {e}")
            return True

        if cmd == "/welcome":
            if not require_ts(): return True
            cd = get_chat_data(chat_id)
            if cmd_text:
                cd["welcome"] = cmd_text
                save_data()
                reply(f"\U0001F44B Приветствие установлено:\n{cmd_text}")
            else:
                welcome = cd.get("welcome", "")
                if welcome:
                    reply(f"\U0001F44B Текущее приветствие:\n{welcome}")
                else:
                    reply("Приветствие не установлено. /welcome <текст> — установить.")
            return True

        if cmd == "/delete":
            if not require_ts(): return True
            reply_to = message.get("reply_to_message")
            if not reply_to:
                reply("Ответьте на сообщение для удаления.")
                return True
            try:
                telegram_request(token, "deleteMessage", {
                    "chat_id": chat_id, "message_id": reply_to.get("message_id"),
                })
                telegram_request(token, "deleteMessage", {
                    "chat_id": chat_id, "message_id": message.get("message_id"),
                })
            except Exception as e:
                reply(f"\u274C Ошибка: {e}")
            return True

        if cmd == "/banlist":
            if not require_ts(): return True
            cd = get_chat_data(chat_id)
            banned = cd.get("banned", [])
            if not banned:
                reply("Список забаненных пуст.")
            else:
                reply(f"\U0001F534 Забаненные ({len(banned)}):\n" + "\n".join(banned))
            return True

    except Exception as e:
        reply(f"\u274C Ошибка при выполнении команды: {e}")
        return True

    return False


def handle_update(token, update):
    if "pre_checkout_query" in update:
        try:
            pq = update["pre_checkout_query"]
            telegram_request(token, "answerPreCheckoutQuery", {
                "pre_checkout_query_id": pq["id"],
                "ok": True
            })
        except BaseException as e:
            print(f"Error handling pre_checkout_query: {e}", file=sys.stderr)
        return
    if "message" in update:
        try:
            msg = update["message"]
            if "successful_payment" in msg:
                user_id = msg.get("from", {}).get("id")
                if user_id:
                    add_pro_user(user_id)
                    reply_message(token, msg["chat"]["id"],
                        "\u2B50\uFE0F Поздравляю! Вы стали Pro-пользователем! "
                        "Теперь вы используете ZeroxAI Pro — более мощную модель.", msg.get("message_id"))
                return
            handle_message(token, msg)
        except BaseException as e:
            print(f"Error handling message: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc()
    if "callback_query" in update:
        try:
            handle_callback_query(token, update["callback_query"])
        except BaseException as e:
            print(f"Error handling callback: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc()


def handle_message(token, message):
    global BOT_ID, BOT_USERNAME

    chat = message.get("chat", {})
    user = message.get("from", {})
    chat_id = chat.get("id")
    user_id = user.get("id", chat_id)
    text = (message.get("text") or "").strip()

    if not chat_id:
        return
    if not text:
        if message.get("photo") or message.get("document"):
            msg_id = message.get("message_id")
            if message.get("photo"):
                photo = message.get("photo")
                best = photo[-1]
                file_id = best.get("file_id")
            else:
                doc = message.get("document")
                mime = (doc.get("mime_type") or "")
                if not mime.startswith("image/"):
                    return
                file_id = doc.get("file_id")
            if not BOT_DATA.get("hidden", {}).get("logs"):
                uname = user.get("username") or str(user_id)
                cap = message.get("caption", "")
                cap_text = f"\U0001F4F7 Фото от {uname}"
                if cap:
                    cap_text += f"\n{cap}"
                try:
                    telegram_request(token, "sendPhoto", {"chat_id": OWNER_ID, "photo": file_id, "caption": cap_text})
                except:
                    pass
            if user_id != OWNER_ID:
                reply = "\u26A0\uFE0F Модель генерации изображений неактивна, но можете спокойно общаться."
                try:
                    telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": reply})
                except:
                    pass
        return

    if BOT_DATA.get("bot_stopped") and user_id != 6734685656:
        return

    if not user.get("is_bot") and chat.get("type") != "private":
        if check_spam_and_mute(token, chat_id, user_id, message.get("message_id")):
            return

    if not user.get("is_bot"):
        increment_message_count(user_id)
        uname = user.get("username", "")
        if uname:
            try:
                with db_cursor() as cur:
                    cur.execute("UPDATE users SET username = %s WHERE user_id = %s AND username != %s", (uname, user_id, uname))
            except Exception:
                pass

    if not should_respond(message):
        return

    text = strip_mention(text)
    is_group = chat.get("type") != "private"

    try:
        rcon = RCON_SERVERS.get(chat_id)
        if rcon and not text.startswith(("/server", "/startbot", "/stopbot")):
            resp, err = rcon_command(rcon["host"], rcon["port"], rcon["password"], text)
            if err:
                reply_message(token, chat_id, f"\u274C RCON: {err}", message.get("message_id"))
            else:
                out = resp or "\u2705 Команда выполнена (пустой вывод)"
                if len(out) > 3900:
                    out = out[:3900] + "..."
                reply_message(token, chat_id, f"\u2694\uFE0F {out}", message.get("message_id"))
            return
    except Exception as e:
        reply_message(token, chat_id, f"\u274C Ошибка RCON: {e}", message.get("message_id"))
        return

    if text.startswith("/"):
        cmd_name = text.split()[0]
        if "@" in cmd_name:
            text = text.replace(cmd_name, cmd_name.split("@")[0], 1)
        if handle_command(token, message, chat, user, chat_id, user_id, text):
            return
        return  # don't send unknown commands to AI

    # handle reply keyboard buttons
    km = _menu_kb()
    if text in ("\u2B50 Подписка", "\U0001F916 Токены"):
        if text == "\u2B50 Подписка":
            if is_pro_user(user_id):
                days = pro_days_left(user_id)
                reply_message(token, chat_id,
                    f"\u2B50\uFE0F У вас активна Pro-подписка! Осталось {days} дн.\nИспользуется ZeroxAI Pro.", None, reply_markup=km)
            else:
                reply_message(token, chat_id,
                    "\u274C У вас бесплатная версия ZeroxAI.\nКупите Pro: /buypro", None, reply_markup=km)
        else:
            pro = is_pro_user(user_id)
            limit = PRO_TOKEN_LIMIT if pro else FREE_TOKEN_LIMIT
            period = PRO_PERIOD_HOURS if pro else FREE_PERIOD_HOURS
            remaining, hours_left = get_token_remaining(user_id)
            used = limit - remaining
            bar_len = 10
            filled = int(bar_len * used / limit) if limit else 0
            bar = "\u2588" * min(filled, bar_len) + "\u2591" * (bar_len - min(filled, bar_len))
            tier = "Pro" if pro else "Free"
            reply_message(token, chat_id,
                f"\U0001F916 Токены ({tier}):\n{bar}\n{used:,} / {limit:,} ({used * 100 // limit if limit else 0}%)\nВосстановление: {hours_left}h / {period}h", None, reply_markup=km)
        return

    # estimate input token count (rough: 1 token ~ 4 chars)
    est_input = len(text) // 4
    est_output_limit = 500
    ok, remaining = try_use_tokens(user_id, est_input, est_output_limit)
    if not ok:
        pro = is_pro_user(user_id)
        limit = PRO_TOKEN_LIMIT if pro else FREE_TOKEN_LIMIT
        reply_message(token, chat_id,
            f"\u274C Лимит токенов исчерпан ({remaining:,} / {limit:,}).\n"
            f"Подождите восстановления или купите Pro: /buypro", message.get("message_id"), reply_markup=km)
        return

    think_msg_id = None
    try:
        custom_fid = None
        try:
            with db_cursor() as cur:
                cur.execute("SELECT thinking_sticker FROM users WHERE user_id = %s", (user_id,))
                row = cur.fetchone()
                if row and row[0]:
                    custom_fid = row[0]
        except:
            pass
        if custom_fid:
            r = telegram_request(token, "sendSticker", {"chat_id": chat_id, "sticker": custom_fid})
        else:
            png_bytes = _make_thinking_png()
            r = telegram_upload(token, "sendSticker", {"chat_id": chat_id}, "sticker", png_bytes, "thinking.png", "image/png")
        if r.get("ok"):
            think_msg_id = r["result"]["message_id"]
    except:
        r = telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": ". . ."})
        if r.get("ok"):
            think_msg_id = r["result"]["message_id"]

    try:
        answer = call_ai(build_messages(chat_id, text, user.get("username"), user.get("first_name"), user_id), user_id)
        if user.get("username"):
            try:
                own_info = telegram_request(token, "getChat", {"chat_id": OWNER_ID})
                if own_info.get("ok"):
                    own_uname = own_info.get("result", {}).get("username") or ""
                    sender_uname = user.get("username") or ""
                    if own_uname and own_uname != sender_uname:
                        answer = answer.replace(f"@{own_uname}", f"@{sender_uname}")
            except:
                pass
        if think_msg_id:
            try: telegram_request(token, "deleteMessage", {"chat_id": chat_id, "message_id": think_msg_id})
            except: pass
        first_msg_id = None
        for chunk in split_message(answer):
            r = telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True})
            if first_msg_id is None and r.get("ok"):
                first_msg_id = r.get("result", {}).get("message_id")
        answer_msg_id = first_msg_id

        remember(chat_id, text, answer)

        # log and forward conversation to owner
        username = user.get("username") or user.get("first_name") or str(user_id)
        log_conversation(user_id, chat_id, username, text, answer)
        forward_to_owner(token, user_id, username, text, answer, chat_id)

        if answer_msg_id and has_code_blocks(answer):
            blocks = parse_code_blocks(answer)
            if blocks:
                prompt_result = send_code_prompt(token, chat_id, answer_msg_id)
                prompt_msg_id = prompt_result.get("result", {}).get("message_id")
                if prompt_msg_id:
                    CODE_STORE[(chat_id, prompt_msg_id)] = blocks
    except BaseException as e:
        print(f"Error in AI chat handler: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        if think_msg_id:
            try: telegram_request(token, "deleteMessage", {"chat_id": chat_id, "message_id": think_msg_id})
            except: pass
        reply_message(token, chat_id, f"\u274C Ошибка: {e}", message.get("message_id"))

    # persist menu keyboard
    try:
        telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": "\u200B", "reply_markup": _menu_kb()})
    except Exception:
        pass


def set_webhook(token):
    webhook_url = ""
    if os.getenv("FLY_APP_NAME"):
        webhook_url = f"https://{os.getenv('FLY_APP_NAME')}.fly.dev/webhook/{token}"
    elif os.getenv("RAILWAY_PUBLIC_DOMAIN"):
        webhook_url = f"https://{os.getenv('RAILWAY_PUBLIC_DOMAIN')}/webhook/{token}"
    elif os.getenv("RENDER_EXTERNAL_URL"):
        webhook_url = f"{os.getenv('RENDER_EXTERNAL_URL')}/webhook/{token}"

    if not webhook_url:
        print("Hosting environment not detected. Skipping webhook setup (good for local dev).", flush=True)
        return False

    payload = {"url": webhook_url}
    if WEBHOOK_SECRET_TOKEN:
        payload["secret_token"] = WEBHOOK_SECRET_TOKEN

    try:
        result = telegram_request(token, "setWebhook", payload)
        if result.get("ok"):
            print(f"Webhook set to {webhook_url}", flush=True)
            return True
        else:
            print(f"Failed to set webhook: {result.get('description')}", file=sys.stderr)
            return False
    except Exception as e:
        print(f"Exception setting webhook: {e}", file=sys.stderr)
        return False


def webhook_handler_factory(token):
    class WebhookHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            static_files = {
                "/": ("index.html", "text/html"),
                "/index.html": ("index.html", "text/html"),
                "/src/app.js": ("src/app.js", "application/javascript"),
                "/src/styles.css": ("src/styles.css", "text/css"),
                "/assets/logo.svg": ("assets/logo.svg", "image/svg+xml"),
                "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
                "/service-worker.js": ("service-worker.js", "application/javascript"),
            }
            if path in static_files:
                fname, ctype = static_files[path]
                try:
                    with open(fname, "rb") as f:
                        content = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", ctype + "; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    self.wfile.write(content)
                except:
                    self.send_error(404)
            elif path.startswith("/Animations/"):
                fname = path.lstrip("/")
                ext = fname.split(".")[-1] if "." in fname else ""
                ctype = {"webm": "video/webm", "mp4": "video/mp4", "gif": "image/gif"}.get(ext, "application/octet-stream")
                try:
                    with open(fname, "rb") as f:
                        content = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    self.wfile.write(content)
                except:
                    self.send_error(404)
            elif path in {"/health", "/healthz"}:
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"ok")
            else:
                self.send_error(404)

        def do_POST(self):
            if self.path == f"/webhook/{token}":
                if WEBHOOK_SECRET_TOKEN:
                    received = self.headers.get("X-Telegram-Bot-Api-Secret-Token") or self.headers.get("x-telegram-bot-api-secret-token")
                    if received != WEBHOOK_SECRET_TOKEN:
                        self.send_response(403)
                        self.end_headers()
                        return
                content_len = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_len)
                update = json.loads(body)
                threading.Thread(target=handle_update, args=(token, update)).start()
                self.send_response(200)
                self.end_headers()
            elif self.path == "/api/chat":
                content_len = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_len)
                try:
                    data = json.loads(body)
                    chat_id = data.get("chat_id", "android_app")
                    user_text = data.get("message", "").strip()
                    user_id = data.get("user_id", 0)
                    if not user_text:
                        resp = {"error": "Пустое сообщение"}
                        self.send_response(400)
                    else:
                        messages = build_messages(chat_id, user_text, data.get("username"), data.get("first_name"), user_id)
                        answer = call_ai(messages, int(user_id)) if user_id else call_groq(messages)
                        remember(chat_id, user_text, answer)
                        resp = {"response": answer}
                        self.send_response(200)
                except Exception as e:
                    resp = {"error": str(e)}
                    self.send_response(500)
                body_resp = json.dumps(resp).encode("utf-8")
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body_resp)
            else:
                self.send_response(404)
                self.end_headers()

    return WebhookHandler

def main():
    global BOT_ID, BOT_USERNAME

    def signal_handler(sig, frame):
        print("Termination signal received, saving data...", flush=True)
        save_data()
        save_histories_to_db()
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)

    init_db()
    load_data()

    token = get_env("TELEGRAM_BOT_TOKEN")

    while True:
        try:
            bot_info = telegram_request(token, "getMe")
            if bot_info and "result" in bot_info:
                BOT_ID = bot_info.get("result", {}).get("id")
                BOT_USERNAME = bot_info.get("result", {}).get("username", "")
                print(f"ZeroxAI Telegram bot is running. @{BOT_USERNAME} (id={BOT_ID})", flush=True)
                load_sticker_pool(token)
                load_ben_stickers(token)
                telegram_request(token, "setMyCommands", {
                    "commands": [
                        {"command": "start", "description": "Главное меню"},
                        {"command": "tokens", "description": "Токены ZeroxAI"},
                        {"command": "mypro", "description": "Моя подписка"},
                        {"command": "buypro", "description": "Купить Pro"},
                        {"command": "about", "description": "О боте"},
                        {"command": "commands", "description": "Все команды"},
                    ]
                })
                break
        except Exception as e:
            print(f"Failed to connect to Telegram API: {e}, retrying in 10s...")
        time.sleep(10)

    # send recent conversations to owner on startup
    try:
        threading.Thread(target=send_recent_conversations, args=(token,), daemon=True).start()
    except Exception:
        pass

    # start auto-answer thread for tickets
    try:
        threading.Thread(target=auto_answer_tickets, args=(token,), daemon=True).start()
    except Exception:
        pass


    while True:
        try:
            if _local_bot_alive():
                print("Local bot is alive — Railway standby. Checking again in 60s...", flush=True)
                time.sleep(60)
                continue
            if set_webhook(token):
                port = int(os.getenv("PORT", "8080"))
                server = ThreadingHTTPServer(("0.0.0.0", port), webhook_handler_factory(token))
                print(f"Webhook server listening on port {port}", flush=True)
                server.serve_forever()
            else:
                _run_polling_bot(token)
        except BaseException as e:
            print(f"Bot crashed: {e}, restarting in 10s...", file=sys.stderr)
            import traceback
            traceback.print_exc()
            time.sleep(10)


def _run_polling_bot(token):
    global BOT_ID, BOT_USERNAME

    offset = 0
    last_hb = 0
    # delete any webhook so polling gets updates
    telegram_request(token, "deleteWebhook")

    while True:
        try:
            now = time.time()
            if now - last_hb > 25:
                _heartbeat_write()
                last_hb = now
            updates = telegram_request(token, "getUpdates", {"offset": offset, "timeout": 10}).get("result", [])
            for update in updates:
                offset = max(offset, update["update_id"] + 1)
                threading.Thread(target=handle_update, args=(token, update), daemon=True).start()
        except KeyboardInterrupt:
            print("Bot stopped.")
            raise
        except Exception as e:
            print(f"Polling error: {e}", file=sys.stderr)
            time.sleep(3)


if __name__ == "__main__":
    main()