import io
import html
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
import subprocess
import datetime
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
from psycopg2 import pool
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "mistralai/mistral-7b-instruct-v0.3"
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
    for p in [".env", "/app/.env"]:
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip())
            break
        except Exception:
            continue


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

_BOT_TOKEN = None
_chat_owner_cache = {}
_super_admin_ids = set()
_username_cache = {}


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
        # create VPN tables
        try:
            with db_cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS vpn_servers (
                        id SERIAL PRIMARY KEY,
                        host TEXT NOT NULL UNIQUE,
                        port INT NOT NULL DEFAULT 51820,
                        country TEXT NOT NULL,
                        city TEXT NOT NULL,
                        location TEXT NOT NULL DEFAULT '',
                        public_key TEXT NOT NULL,
                        endpoint TEXT NOT NULL,
                        active BOOLEAN NOT NULL DEFAULT TRUE,
                        ping_ms INT,
                        load_pct REAL DEFAULT 0,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS vpn_user_configs (
                        id SERIAL PRIMARY KEY,
                        user_id BIGINT NOT NULL,
                        server_id INT NOT NULL REFERENCES vpn_servers(id) ON DELETE CASCADE,
                        config_text TEXT NOT NULL,
                        assigned_ip TEXT,
                        wg_private_key TEXT NOT NULL,
                        wg_public_key TEXT NOT NULL,
                        active BOOLEAN NOT NULL DEFAULT FALSE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (user_id, server_id)
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS vpn_favorites (
                        user_id BIGINT NOT NULL,
                        server_id INT NOT NULL REFERENCES vpn_servers(id) ON DELETE CASCADE,
                        PRIMARY KEY (user_id, server_id)
                    )
                """)
                # add flag column if missing (migration for existing servers)
                cur.execute("ALTER TABLE vpn_servers ADD COLUMN IF NOT EXISTS flag TEXT NOT NULL DEFAULT ''")
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


FREE_TOKEN_LIMIT = 1000
PRO_TOKEN_LIMIT = 4000
FREE_PERIOD_HOURS = 24
PRO_PERIOD_HOURS = 12
TOKEN_STARS_RATE = 50  # tokens per 1 Star for custom amount
PENDING_TOKEN_AMOUNTS = {}  # user_id -> pending token count

# ──────────────────────────────────────────────
# VPN configuration
# ──────────────────────────────────────────────
VPN_WG_PORT = 51820
VPN_WG_MTU = 1420
VPN_WG_KEEPALIVE = 25
VPN_DNS = "1.1.1.1, 8.8.8.8"
VPN_ALLOWED_IPS = "0.0.0.0/0, ::/0"  # full tunnel
VPN_SUBNET_PREFIX = "10.200"
VPN_CURRENT_IP = None  # cached current IP
VPN_LAST_IP_CHECK = 0
VPN_IP_CACHE_TTL = 60  # seconds

# Pre-defined server templates (admin can add more)
VPN_SERVER_TEMPLATES = {
    "oracle-eu-frankfurt": {"country": "Germany", "flag": "🇩🇪", "city": "Frankfurt", "location": "Frankfurt, Germany"},
    "oracle-us-ashburn": {"country": "USA", "flag": "🇺🇸", "city": "Ashburn", "location": "Ashburn, USA"},
    "oracle-uk-london": {"country": "UK", "flag": "🇬🇧", "city": "London", "location": "London, UK"},
    "oracle-jp-tokyo": {"country": "Japan", "flag": "🇯🇵", "city": "Tokyo", "location": "Tokyo, Japan"},
    "oracle-kr-seoul": {"country": "South Korea", "flag": "🇰🇷", "city": "Seoul", "location": "Seoul, South Korea"},
    "oracle-au-sydney": {"country": "Australia", "flag": "🇦🇺", "city": "Sydney", "location": "Sydney, Australia"},
    "oracle-br-saopaulo": {"country": "Brazil", "flag": "🇧🇷", "city": "São Paulo", "location": "São Paulo, Brazil"},
    "oracle-in-mumbai": {"country": "India", "flag": "🇮🇳", "city": "Mumbai", "location": "Mumbai, India"},
}

# VPN server groups by country
VPN_COUNTRIES = {}
for sid, info in VPN_SERVER_TEMPLATES.items():
    c = info["country"]
    if c not in VPN_COUNTRIES:
        VPN_COUNTRIES[c] = []
    VPN_COUNTRIES[c].append(sid)

VPN_USER_PEER_KEYS = {}  # cache: user_id -> {"private": ..., "public": ...}


def _vpn_gen_keys():
    """Generate WireGuard keypair using subprocess (wg must be in PATH or use Python crypto fallback)."""
    import subprocess
    try:
        priv = subprocess.run(["wg", "genkey"], capture_output=True, text=True, timeout=5).stdout.strip()
        pub = subprocess.run(["wg", "pubkey"], input=priv, capture_output=True, text=True, timeout=5).stdout.strip()
        return priv, pub
    except Exception:
        pass
    # fallback: use Python nacl/libsodium if available
    try:
        import nacl.bindings
        raw = os.urandom(32)
        priv_b64 = _wg_base64_encode(raw)
        pub_raw = nacl.bindings.crypto_scalarmult_base(raw)
        pub_b64 = _wg_base64_encode(pub_raw)
        return priv_b64, pub_b64
    except ImportError:
        pass
    # soft fallback: generate random keys (won't work with actual WireGuard but for demo/display)
    import base64
    priv = base64.b64encode(os.urandom(32)).decode().rstrip("=")
    pub = base64.b64encode(os.urandom(32)).decode().rstrip("=")
    return priv, pub


def _wg_base64_encode(data):
    import base64
    return base64.b64encode(data).decode().rstrip("=")


def _vpn_get_peer_keys(user_id):
    """Get or generate WireGuard keys for a user."""
    if user_id in VPN_USER_PEER_KEYS:
        return VPN_USER_PEER_KEYS[user_id]
    priv, pub = _vpn_gen_keys()
    VPN_USER_PEER_KEYS[user_id] = {"private": priv, "public": pub}
    return VPN_USER_PEER_KEYS[user_id]


# Cloudflare WARP — бесплатный встроенный VPN сервер (без карт, без регистрации)
WARP_SERVER_ID = 999
WARP_PUBLIC_KEY = "bmXOC+F1FxEMF9dyiK2H5/1SUtzH0JuVo51h2wPfgyo="
WARP_ENDPOINT = "engage.cloudflareclient.com:2408"
WARP_MTU = 1280


def vpn_is_warp(server):
    return server and server.get("id") == WARP_SERVER_ID


def vpn_warp_server():
    return {
        "id": WARP_SERVER_ID,
        "host": "engage.cloudflareclient.com",
        "port": 2408,
        "country": "Cloudflare",
        "city": "WARP",
        "location": "Cloudflare WARP (бесплатно, без регистрации)",
        "public_key": WARP_PUBLIC_KEY,
        "endpoint": WARP_ENDPOINT,
        "active": True,
        "ping_ms": None,
        "load_pct": 0,
        "flag": "💨",
    }


def vpn_get_servers():
    """Return list of registered VPN servers + WARP as first entry."""
    servers = [vpn_warp_server()]
    try:
        with db_cursor() as cur:
            cur.execute("SELECT id, host, port, country, city, location, public_key, endpoint, active, ping_ms, load_pct, created_at, flag FROM vpn_servers WHERE active = TRUE ORDER BY country, city")
            rows = cur.fetchall()
            for r in rows:
                servers.append({
                    "id": r[0], "host": r[1], "port": r[2] or VPN_WG_PORT,
                    "country": r[3], "city": r[4], "location": r[5],
                    "public_key": r[6], "endpoint": r[7],
                    "active": r[8], "ping_ms": r[9], "load_pct": r[10],
                    "flag": r[12] if len(r) > 12 and r[12] else "🌍",
                })
            return servers
    except Exception as e:
        print(f"vpn_get_servers error: {e}", file=sys.stderr)
        return servers


def vpn_get_server(server_id):
    """Get a single server by ID. Handles WARP virtual server."""
    if server_id == WARP_SERVER_ID:
        return vpn_warp_server()
    try:
        with db_cursor() as cur:
            cur.execute("SELECT id, host, port, country, city, location, public_key, endpoint, active, ping_ms, load_pct, flag FROM vpn_servers WHERE id = %s", (server_id,))
            r = cur.fetchone()
            if r:
                return {"id": r[0], "host": r[1], "port": r[2] or VPN_WG_PORT, "country": r[3], "city": r[4], "location": r[5], "public_key": r[6], "endpoint": r[7], "active": r[8], "ping_ms": r[9], "load_pct": r[10], "flag": r[11] if r[11] else "🌍"}
    except Exception:
        pass
    return None


def vpn_add_server(host, country, city, public_key, endpoint, port=51820, location=""):
    """Add a VPN server to the database."""
    # derive flag from country
    flag_map = {
        "Germany": "🇩🇪", "USA": "🇺🇸", "UK": "🇬🇧", "Japan": "🇯🇵",
        "South Korea": "🇰🇷", "Australia": "🇦🇺", "Brazil": "🇧🇷",
        "India": "🇮🇳", "Canada": "🇨🇦", "France": "🇫🇷", "Italy": "🇮🇹",
        "Spain": "🇪🇸", "Netherlands": "🇳🇱", "Singapore": "🇸🇬",
        "Russia": "🇷🇺", "China": "🇨🇳", "Taiwan": "🇹🇼", "Hong Kong": "🇭🇰",
        "Poland": "🇵🇱", "Sweden": "🇸🇪", "Norway": "🇳🇴", "Finland": "🇫🇮",
        "Denmark": "🇩🇰", "Switzerland": "🇨🇭", "Austria": "🇦🇹",
        "Belgium": "🇧🇪", "Ireland": "🇮🇪", "Portugal": "🇵🇹",
        "Mexico": "🇲🇽", "Argentina": "🇦🇷", "Chile": "🇨🇱",
        "Turkey": "🇹🇷", "UAE": "🇦🇪", "Israel": "🇮🇱",
    }
    flag = flag_map.get(country, "🌍")
    try:
        with db_cursor() as cur:
            cur.execute(
                "INSERT INTO vpn_servers (host, port, country, city, location, public_key, endpoint, flag) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (host) DO UPDATE SET public_key=EXCLUDED.public_key, endpoint=EXCLUDED.endpoint, flag=EXCLUDED.flag",
                (host, port, country, city, location or f"{city}, {country}", public_key, endpoint, flag)
            )
        return True
    except Exception as e:
        print(f"vpn_add_server error: {e}", file=sys.stderr)
        return False


def vpn_remove_server(server_id):
    try:
        with db_cursor() as cur:
            cur.execute("DELETE FROM vpn_servers WHERE id = %s", (server_id,))
            cur.execute("DELETE FROM vpn_user_configs WHERE server_id = %s", (server_id,))
        return True
    except Exception:
        return False


def vpn_get_user_config(user_id, server_id):
    """Get a WireGuard config for a user on a specific server."""
    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT id, config_text, assigned_ip, wg_private_key, wg_public_key, active FROM vpn_user_configs WHERE user_id = %s AND server_id = %s",
                (user_id, server_id)
            )
            r = cur.fetchone()
            if r:
                return {"id": r[0], "config_text": r[1], "assigned_ip": r[2], "private_key": r[3], "public_key": r[4], "active": r[5]}
    except Exception:
        pass
    return None


def vpn_create_user_config(user_id, server):
    """Generate and store a WireGuard config for a user on a given server."""
    if vpn_is_warp(server):
        # WARP uses a different config format
        keys = _vpn_get_peer_keys(user_id)
        config = f"""[Interface]
PrivateKey = {keys["private"]}
Address = 172.16.0.2/32
DNS = 1.1.1.1, 2606:4700:4700::1111
MTU = {WARP_MTU}

[Peer]
PublicKey = {WARP_PUBLIC_KEY}
AllowedIPs = {VPN_ALLOWED_IPS}
Endpoint = {WARP_ENDPOINT}
"""
        try:
            with db_cursor() as cur:
                cur.execute(
                    """INSERT INTO vpn_user_configs (user_id, server_id, config_text, assigned_ip, wg_private_key, wg_public_key, active)
                       VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                       ON CONFLICT (user_id, server_id) DO UPDATE SET config_text=EXCLUDED.config_text, active=TRUE""",
                    (user_id, WARP_SERVER_ID, config, "172.16.0.2", keys["private"], keys["public"])
                )
        except Exception:
            pass
        return config

    keys = _vpn_get_peer_keys(user_id)
    assigned_ip = _vpn_assign_ip(server["id"], user_id)
    if not assigned_ip:
        return None
    config = f"""[Interface]
PrivateKey = {keys["private"]}
Address = {assigned_ip}/24
DNS = {VPN_DNS}
MTU = {VPN_WG_MTU}

[Peer]
PublicKey = {server["public_key"]}
Endpoint = {server["endpoint"]}:{server["port"]}
AllowedIPs = {VPN_ALLOWED_IPS}
PersistentKeepalive = {VPN_WG_KEEPALIVE}
"""
    try:
        with db_cursor() as cur:
            cur.execute(
                """INSERT INTO vpn_user_configs (user_id, server_id, config_text, assigned_ip, wg_private_key, wg_public_key, active)
                   VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                   ON CONFLICT (user_id, server_id) DO UPDATE SET config_text=EXCLUDED.config_text, assigned_ip=EXCLUDED.assigned_ip, active=TRUE""",
                (user_id, server["id"], config, assigned_ip, keys["private"], keys["public"])
            )
        return config
    except Exception as e:
        print(f"vpn_create_user_config error: {e}", file=sys.stderr)
        return None


def _vpn_assign_ip(server_id, user_id):
    """Assign a unique IP in the VPN subnet for a user on a given server."""
    import random
    try:
        with db_cursor() as cur:
            # check if user already has an IP on this server
            cur.execute("SELECT assigned_ip FROM vpn_user_configs WHERE user_id = %s AND server_id = %s", (user_id, server_id))
            existing = cur.fetchone()
            if existing and existing[0]:
                return existing[0]
            # find next free IP in 10.200.SERVER_ID.X
            used_ips = set()
            cur.execute("SELECT assigned_ip FROM vpn_user_configs WHERE server_id = %s AND assigned_ip IS NOT NULL", (server_id,))
            for row in cur.fetchall():
                if row[0]:
                    used_ips.add(row[0])
            # try .2 to .254
            for _ in range(100):
                third = server_id % 255 if server_id else 1
                fourth = random.randint(2, 254)
                ip = f"{VPN_SUBNET_PREFIX}.{third}.{fourth}"
                if ip not in used_ips:
                    return ip
    except Exception as e:
        print(f"_vpn_assign_ip error: {e}", file=sys.stderr)
    # fallback
    return f"{VPN_SUBNET_PREFIX}.{server_id % 255}.{user_id % 254 + 2}"


def vpn_deactivate_config(user_id):
    """Deactivate all active VPN configs for a user."""
    try:
        with db_cursor() as cur:
            cur.execute("UPDATE vpn_user_configs SET active = FALSE WHERE user_id = %s", (user_id,))
        return True
    except Exception:
        return False


def vpn_get_favorites(user_id):
    """Get list of favorited server IDs for a user."""
    try:
        with db_cursor() as cur:
            cur.execute("SELECT server_id FROM vpn_favorites WHERE user_id = %s", (user_id,))
            return {r[0] for r in cur.fetchall()}
    except Exception:
        return set()


def vpn_toggle_favorite(user_id, server_id):
    if server_id == WARP_SERVER_ID:
        return False  # WARP не добавляем в избранное
    try:
        with db_cursor() as cur:
            cur.execute("SELECT 1 FROM vpn_favorites WHERE user_id = %s AND server_id = %s", (user_id, server_id))
            if cur.fetchone():
                cur.execute("DELETE FROM vpn_favorites WHERE user_id = %s AND server_id = %s", (user_id, server_id))
                return False  # removed
            else:
                cur.execute("INSERT INTO vpn_favorites (user_id, server_id) VALUES (%s, %s)", (user_id, server_id))
                return True  # added
    except Exception:
        return False


def vpn_get_active_config(user_id):
    """Get user's currently active VPN config."""
    # Check WARP config first
    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT config_text, assigned_ip FROM vpn_user_configs WHERE user_id = %s AND server_id = %s AND active = TRUE LIMIT 1",
                (user_id, WARP_SERVER_ID)
            )
            r = cur.fetchone()
            if r:
                warp = vpn_warp_server()
                return {"id": 0, "server_id": WARP_SERVER_ID, "config_text": r[0], "assigned_ip": r[1],
                        "country": warp["country"], "city": warp["city"], "host": warp["host"],
                        "location": warp["location"], "ping_ms": None, "flag": warp["flag"]}
    except Exception:
        pass
    # Check DB servers
    try:
        with db_cursor() as cur:
            cur.execute(
                """SELECT c.id, c.server_id, c.config_text, c.assigned_ip, s.country, s.city, s.host, s.location, s.ping_ms, s.flag
                   FROM vpn_user_configs c JOIN vpn_servers s ON c.server_id = s.id
                   WHERE c.user_id = %s AND c.active = TRUE AND s.active = TRUE LIMIT 1""",
                (user_id,)
            )
            r = cur.fetchone()
            if r:
                return {"id": r[0], "server_id": r[1], "config_text": r[2], "assigned_ip": r[3], "country": r[4], "city": r[5], "host": r[6], "location": r[7], "ping_ms": r[8], "flag": r[9] if r[9] else "🌍"}
    except Exception:
        pass
    return None


def vpn_get_my_ip():
    """Get current public IP via external service."""
    global VPN_CURRENT_IP, VPN_LAST_IP_CHECK
    now = time.time()
    if VPN_CURRENT_IP and (now - VPN_LAST_IP_CHECK) < VPN_IP_CACHE_TTL:
        return VPN_CURRENT_IP
    services = [
        "https://api.ipify.org",
        "https://icanhazip.com",
        "https://checkip.amazonaws.com",
        "https://ifconfig.me/ip",
    ]
    import urllib.request
    for svc in services:
        try:
            req = urllib.request.Request(svc, headers={"User-Agent": "curl/8.0"})
            with urllib.request.urlopen(req, timeout=5) as r:
                ip = r.read().decode("utf-8").strip()
                if ip:
                    VPN_CURRENT_IP = ip
                    VPN_LAST_IP_CHECK = now
                    return ip
        except Exception:
            continue
    return "Не удалось определить"


def vpn_ping_server(host, timeout=3):
    """Ping a server, return RTT in ms or None."""
    try:
        import subprocess
        if sys.platform == "win32":
            cmd = ["ping", "-n", "1", "-w", str(int(timeout * 1000)), host]
        else:
            cmd = ["ping", "-c", "1", "-W", str(int(timeout)), host]
        start = time.time()
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 1)
        elapsed = (time.time() - start) * 1000
        if r.returncode == 0:
            return int(elapsed)
    except Exception:
        pass
    return None


def _vpn_build_server_card(server, favorites, idx, active_sid, total_servers):
    """Build an inline keyboard button for a server."""
    star = "⭐" if server["id"] in favorites else ""
    active_mark = "✅ " if active_sid == server["id"] else ""
    ping_str = f" {server['ping_ms']}ms" if server.get("ping_ms") else ""
    label = f"{active_mark}{server['flag']} {server['city']}{ping_str}{star}"
    return {"text": label, "callback_data": f"vpn_server_{server['id']}"}


def vpn_menu(token, chat_id, user_id, msg_id=None):
    """Show VPN main menu."""
    servers = vpn_get_servers()
    active = vpn_get_active_config(user_id)
    favorites = vpn_get_favorites(user_id)
    my_ip = vpn_get_my_ip()

    active_section = ""
    if active:
        active_section = (
            f"\n✅ <b>Подключено:</b>\n"
            f"{active.get('flag', '🌍')} {active['country']}, {active['city']}\n"
            f"IP: <code>{active['assigned_ip']}</code>\n"
            f"Мой IP: <code>{my_ip}</code>\n"
        )
    else:
        active_section = (
            f"\n🔴 <b>Не подключено</b>\n"
            f"Мой IP: <code>{my_ip}</code>\n"
        )

    text = (
        f"🌍 <b>VPN — безопасное подключение</b>\n"
        f"{active_section}"
        f"\n📡 Серверов: {len(servers)}"
    )

    # Build server list inline keyboard
    kb = []
    # Group by country
    grouped = {}
    for s in servers:
        c = s["country"]
        if c not in grouped:
            grouped[c] = []
        grouped[c].append(s)

    sorted_countries = sorted(grouped.keys())
    for country in sorted_countries:
        country_servers = grouped[country]
        flag = country_servers[0].get("flag", "🌍")
        # Country header row
        kb.append([{"text": f"{flag} {country}", "callback_data": "vpn_noop"}])
        # Server rows (2 per row for compactness)
        row = []
        for i, s in enumerate(country_servers):
            ping_str = f" {s['ping_ms']}ms" if s.get("ping_ms") else ""
            fav = "⭐" if s["id"] in favorites else ""
            active_mark = "✅ " if active and active["server_id"] == s["id"] else ""
            label = f"{active_mark}{s['city']}{ping_str}{fav}"
            row.append({"text": label, "callback_data": f"vpn_server_{s['id']}"})
            if len(row) == 2:
                kb.append(row)
                row = []
        if row:
            kb.append(row)

    # Actions row
    action_row = []
    if active:
        action_row.append({"text": "🛑 Отключиться", "callback_data": "vpn_disconnect"})
    else:
        action_row.append({"text": "⚡ Быстрое подключение", "callback_data": "vpn_quick"})
        action_row.append({"text": "🔄 Авто", "callback_data": "vpn_auto"})
    action_row.append({"text": "🔄 Обновить", "callback_data": "vpn_refresh"})
    kb.append(action_row)

    # Bottom nav
    kb.append([
        {"text": "☰ Меню", "callback_data": "vpn_main"},
        {"text": "⚙️ Настройки", "callback_data": "vpn_settings"},
    ])

    method = "editMessageText" if msg_id else "sendMessage"
    telegram_request(token, method, {
        "chat_id": chat_id,
        "message_id": msg_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": kb},
    })


def vpn_show_server(token, chat_id, user_id, server_id, msg_id):
    """Show details for a specific VPN server."""
    server = vpn_get_server(server_id)
    if not server:
        telegram_request(token, "editMessageText", {
            "chat_id": chat_id, "message_id": msg_id,
            "text": "❌ Сервер не найден.",
            "reply_markup": {"inline_keyboard": [[{"text": "◀ Назад", "callback_data": "vpn_main"}]]},
        })
        return

    active = vpn_get_active_config(user_id)
    favorites = vpn_get_favorites(user_id)
    is_fav = server["id"] in favorites
    is_active = active and active["server_id"] == server["id"]
    ping_ms = server.get("ping_ms")

    # Ping if not cached
    if not ping_ms:
        ping_ms = vpn_ping_server(server["host"])
        if ping_ms:
            server["ping_ms"] = ping_ms
            try:
                with db_cursor() as cur:
                    cur.execute("UPDATE vpn_servers SET ping_ms = %s WHERE id = %s", (ping_ms, server_id))
            except Exception:
                pass

    status = "✅ Активен" if is_active else "🔴 Не подключён"
    ping_display = f"{ping_ms} мс" if ping_ms else "—"
    fav_display = "⭐ В избранном" if is_fav else "☆ Не в избранном"

    if vpn_is_warp(server):
        extra_info = (
            f"💨 <b>Cloudflare WARP</b> — бесплатный VPN от Cloudflare.\n"
            f"• Не требует регистрации\n"
            f"• Не требует карты\n"
            f"• Работает сразу\n"
            f"• Скорость до 200 Мбит/с\n"
            f"• Подходит для обхода блокировок\n\n"
            f"⚠️ IP будет принадлежать Cloudflare."
        )
    else:
        extra_info = (
            f"📋 <b>Ваш конфиг будет сгенерирован при подключении.</b>\n\n"
            f"📡 <b>Пинг:</b> {ping_display}\n"
            f"🔗 <b>Хост:</b> <code>{server['host']}:{server['port']}</code>"
        )

    text = (
        f"🌍 <b>{server.get('flag', '')} {server['country']}, {server['city']}</b>\n"
        f"{server.get('location', '')}\n\n"
        f"📊 <b>Статус:</b> {status}\n"
        f"{fav_display}\n\n"
        f"{extra_info}"
    )

    # Dynamic IP display
    my_ip = vpn_get_my_ip()
    text += f"\n🌐 <b>Мой IP:</b> <code>{my_ip}</code>"

    action_row = []
    if is_active:
        action_row.append({"text": "🛑 Отключиться", "callback_data": "vpn_disconnect"})
    else:
        action_row.append({"text": "🔌 Подключиться", "callback_data": f"vpn_connect_{server_id}"})
    if not vpn_is_warp(server):
        action_row.append({"text": "⭐" if not is_fav else "⭐ Убрать", "callback_data": f"vpn_fav_{server_id}"})

    telegram_request(token, "editMessageText", {
        "chat_id": chat_id, "message_id": msg_id,
        "text": text, "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [
                action_row,
                [{"text": "◀ К странам", "callback_data": "vpn_main"}],
            ]
        },
    })


def vpn_connect_user(token, chat_id, user_id, server_id, msg_id):
    """Connect user to a VPN server — show modern config card with QR + download."""
    server = vpn_get_server(server_id)
    if not server:
        telegram_request(token, "editMessageText", {
            "chat_id": chat_id, "message_id": msg_id,
            "text": "❌ Сервер не найден.",
        })
        return

    vpn_deactivate_config(user_id)
    config = vpn_create_user_config(user_id, server)
    if not config:
        telegram_request(token, "editMessageText", {
            "chat_id": chat_id, "message_id": msg_id,
            "text": "❌ Не удалось создать конфигурацию.",
            "reply_markup": {"inline_keyboard": [[{"text": "◀ Назад", "callback_data": "vpn_main"}]]},
        })
        return

    flag = server.get("flag", "🌍")
    country = server["country"]
    city = server["city"]
    is_warp = vpn_is_warp(server)

    # ── Beautiful modern card ──
    header = f"╔══════════════════════════╗\n{'WARP' if is_warp else 'VPN'} • {flag} {country} {city}\n╚══════════════════════════╝"
    card = (
        f"<b>━━━━━━━━━━━━━━━━━━━━</b>\n"
        f"<b>{flag}  {country} · {city}</b>\n"
        f"<b>━━━━━━━━━━━━━━━━━━━━</b>\n\n"

        f"📋 <b>Конфигурация WireGuard</b>\n"
        f"<pre lang=\"ini\">{config}</pre>\n\n"

        f"<b>━━━━━━━━━━━━━━━━━━━━</b>\n"
        f"📱 <b>Как подключиться</b>\n"
    )

    if is_warp:
        card += (
            f"1. Скачай <b>WireGuard</b> (App Store / Google Play)\n"
            f"2. Нажми <b>➕</b> → <b>Импорт из буфера</b>\n"
            f"3. Скопируй конфиг выше и вставь\n"
            f"4. Нажми <b>🔌 Активировать</b>\n\n"
            f"⚡ <b>WARP</b> — бесплатно, безлимитно, без регистрации"
        )
    else:
        card += (
            f"1. Скачай <b>WireGuard</b> (wireguard.com)\n"
            f"2. Создай новый туннель\n"
            f"3. Скопируй конфиг выше\n"
            f"4. Активируй 🚀\n\n"
            f"⚠️ Конфиг содержит твой приватный ключ — никому не передавай"
        )

    # Generate QR code URL for the config
    import urllib.parse
    config_encoded = urllib.parse.quote(config)
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={config_encoded}"

    # Edit message to show card
    telegram_request(token, "editMessageText", {
        "chat_id": chat_id, "message_id": msg_id,
        "text": card, "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [
                [{"text": "📎 Скачать .conf", "callback_data": f"vpn_dlconf_{server_id}"}],
                [{"text": "📱 QR-код для телефона", "callback_data": f"vpn_qr_{server_id}"}],
                [{"text": "🛑 Отключиться", "callback_data": "vpn_disconnect"}],
                [{"text": "◀ К серверам", "callback_data": "vpn_main"}],
            ]
        },
    })

    # Send QR code as a separate photo message
    try:
        # Try to send QR code photo
        qr_text = (
            f"📱 <b>QR-код для WireGuard</b>\n\n"
            f"{flag} <b>{country} · {city}</b>\n\n"
            f"Как использовать:\n"
            f"1. Установи WireGuard\n"
            f"2. Нажми <b>➕</b> → <b>Сканировать QR</b>\n"
            f"3. Наведи камеру на этот код\n\n"
            f"🔒 Конфиг уже сохранён в боте"
        )
        telegram_request(token, "sendPhoto", {
            "chat_id": chat_id,
            "photo": qr_url,
            "caption": qr_text,
            "parse_mode": "HTML",
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "🛑 Отключиться", "callback_data": "vpn_disconnect"}],
                ]
            },
        })
    except Exception:
        pass  # QR sending is optional


def vpn_disconnect_user(token, chat_id, user_id, msg_id):
    """Disconnect user from VPN (deactivate config)."""
    active = vpn_get_active_config(user_id)
    vpn_deactivate_config(user_id)
    my_ip = vpn_get_my_ip()
    if active:
        text = (
            f"🛑 <b>Отключено от VPN</b>\n"
            f"Был: {active.get('flag', '')} {active['country']}, {active['city']}\n"
            f"Теперь ваш IP: <code>{my_ip}</code>"
        )
    else:
        text = (
            f"❌ Нет активного подключения.\n"
            f"Ваш IP: <code>{my_ip}</code>"
        )
    telegram_request(token, "editMessageText" if msg_id else "sendMessage", {
        "chat_id": chat_id, "message_id": msg_id,
        "text": text, "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": [
            [{"text": "🌍 К странам", "callback_data": "vpn_main"}],
        ]},
    })


def vpn_auto_select(servers):
    """Auto-select best server based on ping (skips WARP)."""
    best = None
    best_ping = float("inf")
    import random
    real = [s for s in servers if not vpn_is_warp(s)]
    if not real:
        return servers[0] if servers else None
    random.shuffle(real)
    for s in real[:5]:
        ping = vpn_ping_server(s["host"])
        if ping and ping < best_ping:
            best_ping = ping
            best = s
    return best or real[0]


def vpn_show_settings(token, chat_id, user_id, msg_id):
    """Show VPN settings."""
    text = (
        "⚙️ <b>Настройки VPN</b>\n\n"
        "🛡️ <b>Протокол:</b> WireGuard\n"
        "🔒 <b>Шифрование:</b> ChaCha20-Poly1305\n"
        "📡 <b>Туннель:</b> Полный (весь трафик)\n"
        "📶 <b>MTU:</b> 1420 (WARP: 1280)\n"
        "🌐 <b>DNS:</b> 1.1.1.1, 8.8.8.8\n\n"
        "💨 <b>Cloudflare WARP</b> — встроенный бесплатный сервер\n"
        "• Работает без карты и регистрации\n"
        "• Высокая скорость (до 200 Мбит/с)\n\n"
        "🔧 <b>Дополнительные серверы (для админа):</b>\n"
        "Добавляются через /vpn_addserver\n"
        "Бесплатные платформы:\n"
        "• Oracle Cloud Always Free\n"
        "• Google Cloud Free Tier\n\n"
        "💡 <b>Совет:</b> WARP подходит для обхода блокировок сразу после установки."
    )
    telegram_request(token, "editMessageText", {
        "chat_id": chat_id, "message_id": msg_id,
        "text": text, "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": [
            [{"text": "◀ Назад", "callback_data": "vpn_main"}],
        ]},
    })


def vpn_deploy_script():
    """Generate a bash script to auto-deploy WireGuard on a fresh Ubuntu VPS.
    Returns the script text that admin can run on the server."""
    script = """#!/bin/bash
# WireGuard VPN Server Auto-Deploy Script
# Run this script on your Ubuntu 22.04+ VPS as root

set -e

echo "=== WireGuard VPN Server Setup ==="

# Update system
apt-get update -qq
apt-get upgrade -y -qq

# Install WireGuard
apt-get install -y -qq wireguard

# Generate server keys
cd /etc/wireguard
wg genkey | tee server_private_key | wg pubkey > server_public_key
SERVER_PRIV=$(cat server_private_key)
SERVER_PUB=$(cat server_public_key)
echo "Server public key: $SERVER_PUB"

# Detect public interface
IFACE=$(ip -4 route ls | grep default | grep -Po '(?<=dev )(\\S+)' | head -1)
echo "Detected interface: $IFACE"

# Create server config
cat > /etc/wireguard/wg0.conf << WGEOF
[Interface]
PrivateKey = $SERVER_PRIV
Address = 10.200.0.1/24
ListenPort = 51820
PostUp = iptables -A FORWARD -i wg0 -j ACCEPT; iptables -t nat -A POSTROUTING -o $IFACE -j MASQUERADE; ip6tables -A FORWARD -i wg0 -j ACCEPT; ip6tables -t nat -A POSTROUTING -o $IFACE -j MASQUERADE
PostDown = iptables -D FORWARD -i wg0 -j ACCEPT; iptables -t nat -D POSTROUTING -o $IFACE -j MASQUERADE; ip6tables -D FORWARD -i wg0 -j ACCEPT; ip6tables -t nat -D POSTROUTING -o $IFACE -j MASQUERADE
WGEOF

# Enable IP forwarding
echo "net.ipv4.ip_forward = 1" >> /etc/sysctl.conf
echo "net.ipv6.conf.all.forwarding = 1" >> /etc/sysctl.conf
sysctl -p

# Open firewall
ufw allow 51820/udp || true

# Enable and start
systemctl enable wg-quick@wg0
systemctl start wg-quick@wg0

echo ""
echo "=== Setup Complete ==="
echo "Server Public Key: $SERVER_PUB"
echo "Server IP: $(curl -s ifconfig.me || curl -s api.ipify.org)"
echo "Port: 51820"
echo ""
echo "Add this server to your bot with:"
echo "/vpn_addserver <SERVER_IP> <COUNTRY> <CITY> $SERVER_PUB <SERVER_IP>"
"""
    return script


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

def add_bonus_tokens(user_id, amount):
    try:
        with db_cursor() as cur:
            cur.execute(
                "INSERT INTO user_tokens (user_id, period_start, tokens_used) VALUES (%s, NOW(), %s) "
                "ON CONFLICT (user_id) DO UPDATE SET tokens_used = user_tokens.tokens_used - %s",
                (user_id, -amount, amount),
            )
    except Exception as e:
        print(f"add_bonus_tokens({user_id}, {amount}) error: {e}", file=sys.stderr)

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
    project_mode = messages_are_project(messages)
    if is_pro_user(user_id) and _LOCAL_PRO_MODE:
        res = call_ollama(messages)
        if res:
            return res
    if project_mode:
        return call_cerebras(messages)
    if is_pro_user(user_id):
        return call_openrouter(messages, "mistralai/mistral-7b-instruct-v0.3")
    return call_openrouter(messages, "meta-llama/llama-3.2-3b-instruct")


def call_local_ai(messages, model=None):
    local_url = os.getenv("LOCAL_AI_URL", "").strip()
    if not local_url:
        print("call_local_ai: LOCAL_AI_URL not set", flush=True)
        return ""
    model_name = model or os.getenv("LOCAL_AI_MODEL", "qwen2.5:7b")
    import urllib.request, urllib.parse, json
    body = json.dumps({"model": model_name, "messages": messages, "stream": False, "temperature": 0.4, "top_p": 0.9}).encode("utf-8")
    for attempt in range(2):
        try:
            req = urllib.request.Request(
                local_url.rstrip("/") + "/api/chat",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "ngrok-skip-browser-warning": "true",
                    "User-Agent": "ZeroxAI-Bot/1.0",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8")
                data = json.loads(raw)
                content = data.get("message", {}).get("content", "").strip()
                if content:
                    return content
                print(f"call_local_ai: empty response from {local_url}", flush=True)
                return ""
        except Exception as e:
            print(f"call_local_ai: attempt {attempt} failed: {e}", flush=True)
            if attempt:
                return ""
            import time
            time.sleep(1)
    return ""


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
        return "Ошибка: OPENROUTER_API_KEY не настроен."
    model_name = model or "openai/gpt-4o-mini"
    payload = {"model": model_name, "messages": messages, "temperature": 0.45 if messages_are_project(messages) else 0.55, "top_p": 0.9, "max_tokens": 8192 if messages_are_project(messages) else 2048}
    body = json.dumps(payload).encode("utf-8")
    import http.client
    last_err = ""
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
                last_err = f"OpenRouter status {resp.status}: {raw[:200]}"
                continue
            data = json.loads(raw)
            return data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        except Exception as e:
            last_err = f"OpenRouter exception: {e}"
            time.sleep(1)
    return f"OpenRouter не отвечает: {last_err}"


def call_cerebras(messages, model=None):
    key = os.getenv("CEREBRAS_API_KEY") or "csk-6w6x2dhx248jv4mh43h4cmtxjjcecermek4r38epn9v3e398"
    model_name = model or "llama3.1-70b"
    payload = {"model": model_name, "messages": messages, "temperature": 0.45 if messages_are_project(messages) else 0.55, "top_p": 0.9, "max_tokens": 8192 if messages_are_project(messages) else 2048}
    last_err = None
    for attempt in range(3):
        try:
            body = json.dumps(payload)
            conn = http.client.HTTPSConnection("api.cerebras.ai", timeout=60, context=SSL_CONTEXT)
            conn.request("POST", "/v1/chat/completions", body=body, headers={
                "Content-Type": "application/json", "Authorization": f"Bearer {key}",
            })
            resp = conn.getresponse()
            raw = resp.read().decode()
            if resp.status != 200:
                last_err = f"Cerebras status {resp.status}: {raw[:200]}"
                continue
            data = json.loads(raw)
            return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            last_err = f"Cerebras exception: {e}"
            time.sleep(1)
    return call_openrouter(messages, "deepseek/deepseek-chat")


def call_nvidia(messages, model=None):
    nv_key = os.getenv("NVIDIA_API_KEY")
    if not nv_key:
        return call_openrouter(messages, "meta-llama/llama-3.2-3b-instruct")
    model_name = model or "meta/llama-3.1-8b-instruct"
    payload = {"model": model_name, "messages": messages, "temperature": 0.45 if messages_are_project(messages) else 0.55, "top_p": 0.9, "max_tokens": 8192 if messages_are_project(messages) else 2048}
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
    return call_openrouter(messages, "google/gemini-2.0-flash-001")

def call_mistral(messages, model=None):
    ms_key = os.getenv("MISTRAL_API_KEY")
    if not ms_key:
        return "AI временно недоступен: MISTRAL_API_KEY не настроен."
    model_name = model or "mistral-large-latest"
    payload = {"model": model_name, "messages": messages, "temperature": 0.45 if messages_are_project(messages) else 0.55, "top_p": 0.9, "max_tokens": 8192 if messages_are_project(messages) else 2048}
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
    return call_openrouter(messages, "google/gemini-2.0-flash-001")

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
    1: ["/start", "/help", "/about", "/ping", "/id", "/myrole", "/staff", "/team", "/lightlist", "/rules", "/commands", "/stats", "/report", "/joke", "/coin", "/dice", "/roll", "/choose", "/8ball", "/hug", "/slap", "/quote", "/meme", "/free", "/promo", "/bal", "/slot"],
    5: ["/warn", "/warns", "/unwarn"],
    6: ["/mute", "/unmute", "/kick", "/ban", "/unban"],
    8: ["/role add", "/role remove", "/role give", "/role take", "/role list", "/role info", "/setrules"],
    10: ["/ticket", "/closeticket", "/feedback", "/announce", "/userinfo", "/support", "/clean", "/pin", "/unpin", "/slowmode", "/say", "/welcome", "/delete", "/banlist"],
}

SYSTEM_PROMPT = """
Ты ZeroxAI — AI-ассистент и Project Studio. Создан Эриком Арутюняном.
Не представляйся в каждом ответе. Отвечай коротко и по делу, если не просят иначе.
Идентичность раскрывай только когда напрямую спросили «кто ты».

ИДЕНТИЧНОСТЬ:
- На вопрос «кто ты?» отвечай: «Я ZeroxAI — AI-ассистент и Project Studio.»
- На вопрос «кто тебя создал?»: «Мой создатель Эрик Арутюнян»
- Не называй себя ChatGPT, OpenAI, Groq, Llama, GPT.
- На вопрос о тарифе отвечай «ZeroxAI Pro» или «ZeroxAI Free».

ЯЗЫК И ОБЩЕНИЕ:
- ВАЖНО: Всегда отвечай строго на том же языке, что и пользователь.
- НИКОГДА не смешивай языки в одном ответе.
- Если пользователь написал по-русски — весь ответ только по-русски.
- Если пользователь написал по-армянски — весь ответ только по-армянски.
- Если пользователь написал по-английски — весь ответ только по-английски.
- Русские слова в английской раскладке распознавай как русский текст.
- Обращайся по имени или @username, когда это уместно.
- Не высмеивай ошибки. Понимай намерение и помогай исправить проблему.
- Для простой задачи отвечай коротко. Для проекта, диагностики или сложного кода давай полноценный результат.
- Не обещай сделать работу позже. Делай максимум в текущем ответе.
- Не задавай лишних вопросов: если данных немного не хватает, выбери разумные значения и явно укажи их.
- Не используй HTML-теги Telegram внутри обычного AI-ответа.
- Не оборачивай весь ответ в один общий блок ```. Это ломает сборку файлов.

ФОРМАТИРОВАНИЕ КОДА:
- Любой исходный код, конфиг, JSON, YAML, SQL, команды терминала или содержимое файла всегда помещай в отдельный fenced-блок с тройными обратными кавычками.
- После открывающих кавычек обязательно указывай язык: ```php, ```java, ```python, ```javascript, ```json, ```yaml, ```bash и т.д.
- Не смешивай объяснение и код внутри одного блока. Объяснение пиши обычным текстом перед блоком.
- Никогда не отправляй многострочный код как обычный текст без fenced-блока: Telegram должен показать отдельный блок с возможностью копирования.
- Для полного файла по возможности указывай путь: ```php filename="src/Main.php".
- Короткие имена команд, методов и переменных можно выделять одиночными обратными кавычками, но полноценный код всегда оформляй отдельным блоком.

РАБОТА С ПРОЕКТАМИ:
- Сначала определи платформу, язык, ядро, версию и формат результата из запроса.
- Не путай платформы. Paper/Spigot/Purpur/Bukkit — Java. PocketMine-MP/PMMP/Submarine/EnvyCore — PHP. Nukkit/PowerNukkitX — Java. Bedrock Add-On — behavior/resource pack и при необходимости JavaScript/TypeScript Script API.
- Если пользователь просит исправить существующий проект, сохраняй его архитектуру и меняй только необходимое, если полная переработка не нужна.
- Создавай реально запускаемый проект: все обязательные файлы, конфиги, manifest/plugin.yml, зависимости, README, команды запуска или сборки.
- Не оставляй «TODO», «здесь добавьте код», многоточия вместо реализации и фиктивные функции, если пользователь не просил шаблон.
- Проверяй согласованность имён пакетов, namespace, импортов, путей, команд, permissions, конфигов и версий API.
- Для интерфейсов делай современный, аккуратный, адаптивный дизайн с нормальными состояниями: загрузка, ошибка, пустые данные, мобильная версия.
- Для исправления ошибки сначала найди вероятную первопричину, затем дай исправленные файлы, а не только объяснение.

ФОРМАТ ФАЙЛОВ ДЛЯ АРХИВА:
- Каждый файл выводи отдельным fenced-блоком.
- В первой строке блока обязательно указывай язык и точный путь:
  ```php filename="src/ZeroxAI/Main.php"
  ...полный код файла...
  ```
- Для файлов без отдельного языка используй text, json, yaml, xml, markdown или другой подходящий идентификатор.
- Не объединяй несколько файлов в одном блоке.
- Перед файлами напиши строку: PROJECT_NAME: короткое_имя_проекта
- После файлов дай только краткую инструкцию запуска/установки и список важных допущений.

КАЧЕСТВО КОДА:
- Пиши чистый, понятный и безопасный код с обработкой ошибок.
- Не встраивай секретные API-ключи и токены в исходники. Используй переменные окружения и .env.example.
- Учитывай совместимость с указанной версией языка и API.
- Не выдумывай методы библиотек. Используй устойчивые публичные API и понятные fallback-механизмы.
- Для больших проектов предпочитай несколько хорошо связанных файлов вместо одного огромного файла.
- Комментарии добавляй только там, где они реально помогают.

ВОЗМОЖНОСТИ БОТА:
- Чат-менеджмент: /mute, /unmute, /kick, /ban, /unban, /warn, /warns, /unwarn, /clean, /slowmode, /pin, /unpin, /welcome, /delete, /setrules, /rules
- Казино и экономика: /coin, /dice, /roll, /slot, /allin, /bal, /top, /transfer, /shop, /buy, /free, /promo
- Информация: /help, /about, /ping, /id, /myrole, /team, /stats, /commands, /report, /support
- Проекты: /project <описание> или обычный запрос «создай проект/плагин/сайт/бота»
- Pro: /mypro, /buypro, /tokens
- Сервер/RCON: /server, /startbot, /stopbot, /statbot
""".strip()

PROJECT_BASE_PROMPT = """
ZEROX_PROJECT_MODE
Ты работаешь в режиме ZeroxAI Project Studio. Результат должен быть пригоден для автоматической сборки ZIP-архива.

Обязательные требования:
1. Выдай законченную реализацию, а не демонстрационный фрагмент.
2. Начни с `PROJECT_NAME: имя_проекта` латиницей без пробелов.
3. Каждый файл выдай отдельным блоком вида ```язык filename="путь/к/файлу.ext".
4. Пути должны быть относительными, без `..`, абсолютных путей и дубликатов.
5. Добавь README.md с установкой, запуском/сборкой, требованиями и краткой структурой.
6. Добавь .env.example, если проект использует токены, БД или внешние API. Реальные секреты не вставляй.
7. Не сокращай код словами «остальное аналогично», не оставляй TODO и заглушки.
8. Учитывай предыдущие сообщения: при доработке возвращай все изменённые файлы целиком.
9. В конце коротко перечисли, что реализовано и как запустить.
10. Если объём очень большой, в первую очередь обеспечь полностью рабочее ядро проекта и критические файлы, а не множество пустых модулей.
""".strip()

PROJECT_KIND_PROMPTS = {
    "mcbe_php": """
Специализация: плагины Minecraft Bedrock/MCPE для PocketMine-MP и PHP-ядер.
- PocketMine-MP, PMMP, Submarine, EnvyCore и их форки используют PHP-плагин, если пользователь явно не указал иное.
- Строго следуй структуре папок:
  plugin.yml          — корень проекта
  resources/
    config.yml        — конфиг с настройками по умолчанию
    messages.yml      — файл сообщений/локализации (если нужен)
  src/<Namespace>/
    Main.php          — главный класс, extends PluginBase
    BanManager.php    — менеджер банов (если нужен)
    PlayerListener.php — слушатель событий (если нужен)
    commands/
      BanCommand.php
      UnbanCommand.php
      (другие команды по необходимости)
  README.md
- Обязательно создай plugin.yml, resources/config.yml, корректный src/namespace и главный класс PluginBase.
- Учитывай точную версию ядра/API и версию PHP. Для старых ядер не используй синтаксис и API новых PMMP.
- Не подменяй API одного ядра другим. Если ядро нестандартное, опирайся на названия классов и стиль API из контекста пользователя.
- Команды должны иметь permissions, usage, aliases при необходимости; события регистрируй в onEnable.
- Сохраняй данные безопасно: YAML/JSON/SQLite в зависимости от задачи, с созданием каталогов и обработкой отсутствующих ключей.
- Для многоверсионных серверов не используй новые блоки/предметы без fallback, если пользователь просит поддержку старых клиентов.
- Добавь README с установкой .phar/исходников и совместимостью. Не утверждай, что PHAR уже скомпилирован, если выдаёшь только исходники.
- Каждый PHP-файл — полноценный рабочий класс без заглушек и TODO.
""".strip(),
    "nukkit": """
Специализация: Minecraft Bedrock-серверы Nukkit/PowerNukkitX.
- Используй Java и API указанного ядра, plugin.yml, Maven или Gradle, корректный main class.
- Не смешивай Nukkit API с Bukkit/Paper или PocketMine.
- Добавь pom.xml/build.gradle, README и настройки ресурсов.
""".strip(),
    "bedrock_addon": """
Специализация: Minecraft Bedrock Add-On.
- Создавай behavior_pack и при необходимости resource_pack с корректными manifest.json, UUID, min_engine_version и зависимостями.
- Для Script API используй JavaScript/TypeScript и только совместимые модули @minecraft/server для указанной версии.
- Добавь функции, entities, items, blocks, recipes, texts и textures только когда они нужны задаче.
- README должен объяснять импорт .mcpack/.mcaddon и включение экспериментальных функций, если они действительно требуются.
""".strip(),
    "minecraft_java": """
Специализация: Minecraft Java Edition plugins/mods/datapacks.
- Paper/Spigot/Purpur/Bukkit — Java plugin. Строгая структура:
  <ProjectName>/
    plugin.yml
    config.yml           — если нужен конфиг
    messages.yml         — если нужны сообщения
    src/main/java/<package>/
      Main.java          — extends JavaPlugin
      commands/
        <Command>.java
      listeners/
        <Listener>.java
      managers/
        <Manager>.java
      database/
        <Database>.java  — если есть БД
      utils/
        <Util>.java
    src/main/resources/
      plugin.yml         — копия в ресурсах для сборки
      config.yml
    pom.xml ИЛИ build.gradle / settings.gradle
    README.md
    .gitignore
- Fabric/Forge/NeoForge — мод с соответствующим loader metadata и build-конфигурацией.
- Datapack — pack.mcmeta и data namespace без Java-классов.
- Не смешивай Bukkit API, Fabric API и Bedrock/PocketMine API.
- Каждый Java-файл — полноценный рабочий класс без заглушек и TODO.
""".strip(),
    "telegram_bot": """
Специализация: Telegram-боты.
- Выбирай библиотеку из запроса; для Python предпочтительно современное aiogram 3.x, если версия не задана.
- Разделяй handlers, services, storage/config, добавляй .env.example, requirements.txt и обработку ошибок.
- Не вставляй токен в код. Учитывай private/group chats, callback queries, права и rate limiting.
""".strip(),
    "web": """
Специализация: сайты и web-приложения.
- Делай адаптивный интерфейс для телефона и ПК, семантическую разметку, доступность и понятную навигацию.
- Не используй несуществующие изображения. Для локальных assets добавляй понятные placeholders или генеративные CSS/SVG элементы.
- Проверяй, что HTML/CSS/JS пути совпадают и приложение запускается без сборщика, если пользователь просит один index.html.
""".strip(),
    "android": """
Специализация: Android-проекты.
- Указывай совместимые Gradle/AGP/JDK/minSdk/targetSdk, manifest permissions и структуру модулей.
- Для камеры, файлов, Bluetooth и других разрешений реализуй runtime permission flow.
- Не выдумывай ресурсы и зависимости; добавляй полный settings.gradle(.kts), build files и README.
""".strip(),
    "generic": """
Специализация: универсальная разработка проектов.
- Выбери понятную архитектуру, минимально необходимые зависимости, конфигурацию запуска и тестовый сценарий.
- Обеспечь согласованность всех файлов и реальную точку входа.
""".strip(),
}

PROJECT_CREATE_WORDS = (
    "создай", "сделай", "напиши", "собери", "разработай", "реализуй", "добавь",
    "create", "build", "make", "develop", "implement", "generate",
)
PROJECT_NOUNS = (
    "проект", "плагин", "plugin", "сайт", "website", "бот", "bot", "приложение", "app",
    "игру", "game", "датапак", "datapack", "мод", "mod", "аддон", "addon", "архив", "zip",
    "api", "скрипт", "script", "ядро", "core",
)
PROJECT_FOLLOWUP_WORDS = (
    "исправь", "почини", "улучши", "доработай", "обнови", "не работает", "ошибка", "баг",
    "fix", "repair", "improve", "update", "bug", "broken",
)


def _history_has_project(history):
    recent = " ".join(str(item.get("content", "")) for item in history[-8:]).lower()
    return any(noun in recent for noun in PROJECT_NOUNS) or "project_name:" in recent


def detect_project_kind(user_text, history=None, force_project=False):
    history = history or []
    lower = (user_text or "").lower()
    combined = lower + " " + " ".join(str(item.get("content", "")).lower() for item in history[-6:])

    explicit_create = any(word in lower for word in PROJECT_CREATE_WORDS) and any(noun in lower for noun in PROJECT_NOUNS)
    explicit_project = any(token in lower for token in ("/project", "project_name:", "полный проект", "готовый проект", "в архив"))
    followup = any(word in lower for word in PROJECT_FOLLOWUP_WORDS) and _history_has_project(history)
    if not (force_project or explicit_create or explicit_project or followup):
        return None

    if any(x in combined for x in ("paper", "spigot", "purpur", "bukkit", "fabric", "forge", "neoforge", "java edition", "minecraft java")):
        return "minecraft_java"
    if any(x in combined for x in ("nukkit", "powernukkit", "powernukkitx")):
        return "nukkit"
    if any(x in combined for x in ("behavior pack", "resource pack", "behavior_pack", "resource_pack", "mcaddon", "mcpack", "bedrock add-on", "bedrock addon", "script api", "@minecraft/server")):
        return "bedrock_addon"
    if any(x in combined for x in ("pocketmine", "pmmp", "submarine", "envycore", "mcpe", "minecraft pe", "minecraft bedrock", ".phar", "plugin.yml")) and not any(x in combined for x in ("paper", "spigot", "purpur", "bukkit")):
        return "mcbe_php"
    if "minecraft" in combined and any(x in combined for x in ("плагин", "plugin", "датапак", "datapack", "мод", "mod")):
        return "generic"
    if any(x in combined for x in ("telegram", "телеграм", "aiogram", "pyrogram", "telebot")):
        return "telegram_bot"
    if any(x in combined for x in ("android", "apk", "gradle", "kotlin", "android studio")):
        return "android"
    if any(x in combined for x in ("html", "css", "javascript", "typescript", "react", "vue", "сайт", "website", "лендинг", "frontend", "web app")):
        return "web"
    return "generic"


def build_project_instruction(kind):
    return PROJECT_BASE_PROMPT + "\n\n" + PROJECT_KIND_PROMPTS.get(kind or "generic", PROJECT_KIND_PROMPTS["generic"])


def messages_are_project(messages):
    return any(msg.get("role") == "system" and "ZEROX_PROJECT_MODE" in str(msg.get("content", "")) for msg in messages)



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
            "chapters": [],
        }
    return BOT_DATA["chats"][cid]


def get_user_level(chat_id, user_id):
    cd = get_chat_data(chat_id)
    role_name = cd.get("users", {}).get(str(user_id))
    if role_name and role_name in cd.get("roles", {}):
        return cd["roles"][role_name]
    return 1


def has_level(chat_id, user_id, required):
    if user_id in _super_admin_ids:
        return True
    if _BOT_TOKEN and chat_id not in _chat_owner_cache:
        try:
            admins = telegram_request(_BOT_TOKEN, "getChatAdministrators", {"chat_id": chat_id}).get("result", [])
            for a in admins:
                if a.get("status") == "creator":
                    _chat_owner_cache[chat_id] = a["user"]["id"]
                    break
        except Exception:
            _chat_owner_cache[chat_id] = None
    if _chat_owner_cache.get(chat_id) == user_id:
        return True
    return get_user_level(chat_id, user_id) >= required


def get_role_name(chat_id, user_id):
    cd = get_chat_data(chat_id)
    return cd.get("users", {}).get(str(user_id), "")


def migrate_legacy_user_keys(token):
    for cid_str, cd in list(BOT_DATA.get("chats", {}).items()):
        users = cd.get("users", {})
        changed = False
        for key, role_name in list(users.items()):
            if isinstance(key, str) and key.startswith("@"):
                resolved = resolve_username(token, key, int(cid_str))
                if resolved:
                    users[str(resolved)] = role_name
                    del users[key]
                    changed = True
        if changed:
            save_data()


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
    username_clean = username.lstrip("@").lower()
    cached = _username_cache.get(username_clean)
    if cached:
        return cached
    try:
        if DB_POOL:
            with db_cursor() as cur:
                cur.execute("SELECT user_id FROM users WHERE LOWER(username) = %s", (username_clean,))
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
    # fallback: try getChat with @username (Telegram may accept user @usernames too)
    try:
        chat_info = telegram_request(token, "getChat", {"chat_id": f"@{username_clean}"})
        if chat_info:
            result = chat_info.get("result") or {}
            if result.get("type") == "private":
                uid = result.get("id")
                if uid:
                    _username_cache[username_clean] = uid
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


_BASE64 = __import__("base64")

_STICKER_B64 = "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"

def _make_thinking_png():
    return _BASE64.b64decode(_STICKER_B64)

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


def _menu_kb(is_pm=True):
    if not is_pm:
        return {"remove_keyboard": True}
    return {
        "keyboard": [
            [{"text": "🛒 Магазин"}, {"text": "🌍 VPN"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def shop(token, chat_id, user_id):
    is_pro = is_pro_user(user_id)
    tier = "Pro" if is_pro else "Free"
    text = (
        "\U0001F6D2 <b>Магазин ZeroxAI</b>\n\n"
        "\U0001F535 <b>Free</b> — Бесплатно\n"
        "Meta Llama 3.2 3B, до 1000 токенов/24ч\n\n"
        "\u2B50 <b>Pro</b> — 100 ⭐\n"
        "Mistral 7B (чат) + Cerebras Llama 3.1 70B (код), до 4000 токенов/12ч, 30 дней\n\n"
        "\U0001F916 <b>Токены</b>\n"
        "\uD83D\uDFE2 50 = ⭐1 | 100 = ⭐2 | 250 = ⭐4\n"
        "\uD83D\uDFE2 500 = ⭐7 | 1000 = ⭐12\n"
        "Кастом: 50 токенов = 1 ⭐\n\n"
        f"Текущий тариф: <b>{tier}</b>"
    )
    inline_kb = {
        "inline_keyboard": [[
            {"text": "\U0001F535 Free", "callback_data": "shop_free"},
            {"text": "\u2B50 Pro", "callback_data": "shop_pro_info"},
            {"text": "\U0001F916 Токены", "callback_data": "shop_tokens"},
        ]]
    }
    reply_message(token, chat_id, text, None, parse_mode="HTML", reply_markup=inline_kb)


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


def build_messages(chat_id, user_text, username=None, first_name=None, user_id=None, force_project=False):
    history = USER_HISTORIES.get(chat_id, [])[-MAX_HISTORY_MESSAGES:]
    user_ref = first_name or username or "Пользователь"
    lang = detect_lang(user_text)
    lang_names = {"ru": "русский", "arm": "армянский", "en": "английский"}
    lang_name = lang_names.get(lang, "русский")
    context = f"С тобой говорит {user_ref}."
    if username:
        context += f" Его юзернейм: @{username}."
    if user_id and is_pro_user(user_id):
        context += " У пользователя Pro-подписка."
    context += f" Язык пользователя: {lang_name}. ОТВЕЧАЙ ТОЛЬКО НА ЭТОМ ЯЗЫКЕ. НИ СЛОВА НА ДРУГИХ ЯЗЫКАХ."

    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "system", "content": context}]
    project_kind = detect_project_kind(user_text, history, force_project=force_project)
    if project_kind:
        messages.append({"role": "system", "content": build_project_instruction(project_kind)})
    messages.extend(history)
    messages.append({"role": "user", "content": user_text + f"\n\n[ВАЖНО: Отвечай ТОЛЬКО на {lang_name} языке. Ни одного слова на других языках.]"})
    return messages


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
                (user_id, chat_id, username, user_message, ai_response or "")
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
        f"<b>AI:</b>\n{ai_response[:1500] if ai_response else 'Пустой ответ'}"
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
        print("Groq: no API keys configured", flush=True)
        return "Ошибка: добавьте ZEROXAI_API_KEYS в переменные окружения."
    model_name = model or MODEL
    payload = {"model": model_name, "messages": messages, "temperature": 0.45 if messages_are_project(messages) else 0.55, "top_p": 0.9, "max_tokens": 8192 if messages_are_project(messages) else 2048}
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
    print(f"Groq: all keys failed, last error: {last_error}", flush=True)
    return f"Ошибка Groq: {last_error}"


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
    """Parse fenced code blocks and preserve exact project file paths."""
    pattern = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)
    result = []
    for match in pattern.finditer(text or ""):
        header = (match.group(1) or "").strip()
        code = match.group(2).strip("\n")
        parts = header.split()
        lang = parts[0].lower() if parts else "text"
        filename = ""

        fm = re.search(r'(?:filename|file|path)\s*=\s*(["\'])(.+?)\1', header, re.IGNORECASE)
        if not fm:
            fm = re.search(r'(?:filename|file|path)\s*=\s*([^\s]+)', header, re.IGNORECASE)
        if fm:
            filename = fm.group(2 if fm.lastindex and fm.lastindex >= 2 else 1).strip()

        if not filename:
            first_lines = "\n".join(code.splitlines()[:3])
            fm = re.search(r'^\s*(?://|#|;|<!--)\s*(?:filename|file|path)\s*:\s*([^\n>]+)', first_lines, re.IGNORECASE | re.MULTILINE)
            if fm:
                filename = fm.group(1).strip()

        result.append((lang, filename, code))
    return result


def has_code_blocks(text):
    if "```" not in (text or ""):
        return False
    blocks = parse_code_blocks(text)
    if not blocks:
        return False
    for lang, filename, code in blocks:
        if filename or (lang and lang != "text"):
            return True
        if code and len(code) >= 10 and re.search(r"[{}();\[\]<>]|\b(function|class|def|if|for|while|import|echo|return|<\?php)\b", code):
            return True
    return False


def get_file_extension(lang):
    ext = LANG_EXT.get((lang or "text").lower())
    return ext if ext is not None else (f".{lang}" if lang else ".txt")


def _safe_project_path(filename, fallback):
    name = (filename or fallback).replace("\\", "/").strip().lstrip("/")
    parts = [part for part in name.split("/") if part not in ("", ".", "..")]
    cleaned = "/".join(parts)
    cleaned = re.sub(r"[\x00-\x1f:*?\"<>|]", "_", cleaned)
    return cleaned[:240] or fallback


def _project_slug(value):
    value = (value or "zeroxai_project").strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "_", value, flags=re.IGNORECASE)
    value = re.sub(r"_+", "_", value).strip("._-")
    return (value or "zeroxai_project")[:64]


def extract_project_name(answer, user_text=""):
    match = re.search(r"^\s*PROJECT_NAME\s*:\s*([^\n]+)", answer or "", re.IGNORECASE | re.MULTILINE)
    if match:
        return _project_slug(match.group(1))
    words = re.findall(r"[A-Za-z0-9_-]+", user_text or "")
    if words:
        return _project_slug("_".join(words[:5]))
    return "zeroxai_project"


def create_project_zip(blocks, project_name="zeroxai_project", source_request=""):
    buf = io.BytesIO()
    used = set()
    files_written = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, (lang, filename, code) in enumerate(blocks):
            ext = get_file_extension(lang)
            fallback = f"file_{i + 1}{ext}"
            safe_name = _safe_project_path(filename, fallback)
            base_name = safe_name
            duplicate = 2
            while safe_name.lower() in used:
                stem, suffix = os.path.splitext(base_name)
                safe_name = f"{stem}_{duplicate}{suffix}"
                duplicate += 1
            used.add(safe_name.lower())
            zf.writestr(safe_name, code)
            files_written.append(safe_name)

        if not any(name.lower() in ("readme.md", "readme.txt") or name.lower().endswith("/readme.md") for name in files_written):
            readme = (
                f"# {project_name}\n\n"
                "Проект автоматически собран ZeroxAI Project Studio.\n\n"
                "## Файлы\n" + "\n".join(f"- `{name}`" for name in files_written) + "\n\n"
                "## Исходный запрос\n" + (source_request.strip() or "Не указан") + "\n"
            )
            zf.writestr("README_ZeroxAI.md", readme)
    return buf.getvalue()


def project_display_text(answer, blocks, project_name):
    without_code = re.sub(r"```[^\n`]*\n.*?```", "", answer or "", flags=re.DOTALL)
    without_code = re.sub(r"^\s*PROJECT_NAME\s*:[^\n]*", "", without_code, flags=re.IGNORECASE | re.MULTILINE)
    without_code = re.sub(r"\n{3,}", "\n\n", without_code).strip()
    filenames = []
    for index, (lang, filename, code) in enumerate(blocks):
        filenames.append(_safe_project_path(filename, f"file_{index + 1}{get_file_extension(lang)}"))
    file_preview = "\n".join(f"• {name}" for name in filenames[:12])
    if len(filenames) > 12:
        file_preview += f"\n• … ещё {len(filenames) - 12}"
    intro = f"✅ Проект `{project_name}` собран: {len(filenames)} {_russian_file_word(len(filenames))}."
    details = f"\n\n{without_code}" if without_code else ""
    listing = f"\n\n📁 Состав архива:\n{file_preview}" if filenames else ""
    return intro + details + listing


def validate_project_blocks(kind, blocks):
    """Return critical issues that make an automatically generated project incomplete."""
    issues = []
    if not blocks:
        return ["Ответ не содержит ни одного файла в fenced-блоках."]

    normalized = []
    seen = set()
    for index, (lang, filename, code) in enumerate(blocks):
        safe_name = _safe_project_path(filename, f"file_{index + 1}{get_file_extension(lang)}")
        lower_name = safe_name.lower()
        normalized.append(lower_name)
        if not filename:
            issues.append(f"У файла №{index + 1} не указано filename=...")
        if lower_name in seen:
            issues.append(f"Повторяется путь файла: {safe_name}")
        seen.add(lower_name)
        if not (code or "").strip():
            issues.append(f"Файл {safe_name} пустой.")
        if re.search(r"\bTODO\b|остальн(?:ое|ые)\s+аналогично|здесь\s+(?:добавьте|вставьте)\s+код", code or "", re.IGNORECASE):
            issues.append(f"В файле {safe_name} осталась заглушка/TODO.")

    def has_name(name):
        name = name.lower()
        return any(item == name or item.endswith("/" + name) for item in normalized)

    def has_suffix(suffix):
        suffix = suffix.lower()
        return any(item.endswith(suffix) for item in normalized)

    if kind == "mcbe_php":
        if not has_name("plugin.yml"):
            issues.append("Для PocketMine/MCPE-плагина отсутствует plugin.yml.")
        if not any(item.endswith(".php") for item in normalized):
            issues.append("Для PocketMine/MCPE-плагина отсутствуют PHP-файлы.")
    elif kind == "nukkit":
        if not has_name("plugin.yml"):
            issues.append("Для Nukkit-плагина отсутствует plugin.yml.")
        if not any(item.endswith(".java") for item in normalized):
            issues.append("Для Nukkit-плагина отсутствуют Java-файлы.")
        if not (has_name("pom.xml") or has_name("build.gradle") or has_name("build.gradle.kts")):
            issues.append("Для Nukkit-плагина отсутствует Maven/Gradle-конфигурация.")
    elif kind == "minecraft_java":
        is_datapack = has_name("pack.mcmeta")
        if not is_datapack and not any(item.endswith(".java") for item in normalized):
            issues.append("Для Java-проекта Minecraft отсутствуют Java-файлы или pack.mcmeta.")
        if not is_datapack and not (has_name("pom.xml") or has_name("build.gradle") or has_name("build.gradle.kts")):
            issues.append("Для Java-проекта Minecraft отсутствует Maven/Gradle-конфигурация.")
    elif kind == "bedrock_addon":
        manifests = [item for item in normalized if item.endswith("manifest.json")]
        if not manifests:
            issues.append("Для Bedrock Add-On отсутствует manifest.json.")
    elif kind == "web":
        if not has_name("index.html") and not any(item.endswith((".tsx", ".jsx", ".vue")) for item in normalized):
            issues.append("Для web-проекта отсутствует index.html или основной компонент приложения.")
    elif kind == "telegram_bot":
        if not any(item.endswith(".py") for item in normalized):
            issues.append("Для Telegram-бота отсутствуют Python-файлы.")
        if not (has_name("requirements.txt") or has_name("pyproject.toml")):
            issues.append("Для Telegram-бота отсутствует requirements.txt или pyproject.toml.")
    elif kind == "android":
        if not has_suffix("androidmanifest.xml"):
            issues.append("Для Android-проекта отсутствует AndroidManifest.xml.")
        if not (has_name("settings.gradle") or has_name("settings.gradle.kts")):
            issues.append("Для Android-проекта отсутствует settings.gradle(.kts).")

    if not any(item.endswith("readme.md") or item.endswith("readme.txt") for item in normalized):
        issues.append("Отсутствует README с установкой и запуском.")
    return issues


def _project_answer_score(kind, answer):
    blocks = parse_code_blocks(answer) if has_code_blocks(answer) else []
    issues = validate_project_blocks(kind, blocks)
    named = sum(1 for _lang, filename, code in blocks if filename and (code or "").strip())
    return named * 10 + len(blocks) * 2 - len(issues) * 20


def repair_project_answer(ai_messages, answer, kind, user_id):
    """Run one bounded self-review pass when critical project files are missing."""
    if os.getenv("ZEROXAI_PROJECT_SELF_REVIEW", "1").strip().lower() in ("0", "false", "off", "no"):
        return answer
    blocks = parse_code_blocks(answer) if has_code_blocks(answer) else []
    issues = validate_project_blocks(kind, blocks)
    if not issues:
        return answer

    issue_text = "\n".join(f"- {issue}" for issue in issues[:12])
    repair_prompt = (
        "Проведи финальную самопроверку проекта. Ниже обнаружены критические проблемы:\n"
        f"{issue_text}\n\n"
        "Верни заново ВЕСЬ исправленный проект, а не патч. Соблюдай PROJECT_NAME и отдельный "
        "fenced-блок с filename= для каждого файла. Удали TODO и обеспечь согласованность путей, "
        "namespace/package, конфигов, команд и зависимостей."
    )
    repair_messages = [*ai_messages, {"role": "assistant", "content": answer}, {"role": "user", "content": repair_prompt}]
    repaired = call_ai(repair_messages, user_id)
    if _project_answer_score(kind, repaired) > _project_answer_score(kind, answer):
        return repaired
    return answer


def _russian_file_word(count):
    count = abs(int(count)) % 100
    last = count % 10
    if 11 <= count <= 19:
        return "файлов"
    if last == 1:
        return "файл"
    if 2 <= last <= 4:
        return "файла"
    return "файлов"


def _telegram_code_language(lang):
    """Return a safe Telegram language hint for syntax highlighting."""
    value = (lang or "text").strip().lower()
    aliases = {
        "py": "python", "js": "javascript", "ts": "typescript",
        "yml": "yaml", "sh": "bash", "shell": "bash",
        "c++": "cpp", "cs": "csharp", "html5": "html",
        "txt": "text", "md": "markdown",
    }
    value = aliases.get(value, value)
    value = re.sub(r"[^a-z0-9_+.#-]", "", value)
    return value[:32] or "text"


def _split_code_for_telegram_html(code, max_escaped_length=3000):
    """Split code without breaking Telegram HTML <pre> wrappers."""
    code = (code or "").replace("\r\n", "\n").replace("\r", "\n")
    if not code:
        return [""]

    chunks = []
    current = ""
    for line in code.splitlines(keepends=True):
        candidate = current + line
        if current and len(html.escape(candidate)) > max_escaped_length:
            chunks.append(current.rstrip("\n"))
            current = line
        else:
            current = candidate

        while len(html.escape(current)) > max_escaped_length:
            low, high = 1, len(current)
            best = 1
            while low <= high:
                mid = (low + high) // 2
                if len(html.escape(current[:mid])) <= max_escaped_length:
                    best = mid
                    low = mid + 1
                else:
                    high = mid - 1
            chunks.append(current[:best])
            current = current[best:]

    if current or not chunks:
        chunks.append(current.rstrip("\n"))
    return chunks


def _looks_like_standalone_code(text):
    """Conservative fallback for model responses that forgot fenced blocks."""
    value = (text or "").strip()
    if not value or "\n" not in value or len(value) < 24:
        return False
    lines = [line for line in value.splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    code_hits = sum(bool(re.search(
        r"^\s*(?:import\s+|from\s+\S+\s+import\s+|class\s+\w+|def\s+\w+|function\s+\w+|"
        r"(?:public|private|protected|static|final|const|let|var)\s+|<\?php|<!DOCTYPE|<html|"
        r"SELECT\s+|INSERT\s+|UPDATE\s+|CREATE\s+TABLE|package\s+|namespace\s+|#include\s*[<\"]|"
        r"[}\]);]\s*$)", line, re.IGNORECASE
    )) for line in lines)
    symbols = len(re.findall(r"[{}();=\[\]<>]", value))
    return code_hits >= 2 or (code_hits >= 1 and symbols >= 5)


def _send_plain_ai_text(token, chat_id, text, reply_to_msg_id=None):
    first_message_id = None
    for chunk in split_message((text or "").strip()):
        if not chunk:
            continue
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        if reply_to_msg_id and first_message_id is None:
            payload["reply_to_message_id"] = reply_to_msg_id
        result = telegram_request(token, "sendMessage", payload)
        if first_message_id is None and result.get("ok"):
            first_message_id = result.get("result", {}).get("message_id")
    return first_message_id


def _send_copyable_code_block(token, chat_id, code, lang="text", filename="", reply_to_msg_id=None):
    """Send code as Telegram HTML <pre>, which gives a native copy action."""
    first_message_id = None
    language = _telegram_code_language(lang)
    safe_filename = html.escape(filename or "")
    parts = _split_code_for_telegram_html(code)

    for index, part in enumerate(parts):
        title = ""
        if safe_filename:
            suffix = f" ({index + 1}/{len(parts)})" if len(parts) > 1 else ""
            title = f"📄 <code>{safe_filename}</code>{suffix}\n"
        elif len(parts) > 1:
            title = f"🧩 Код ({index + 1}/{len(parts)})\n"

        escaped_code = html.escape(part)
        body = f'{title}<pre><code class="language-{language}">{escaped_code}</code></pre>'
        payload = {
            "chat_id": chat_id,
            "text": body,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_to_msg_id and first_message_id is None:
            payload["reply_to_message_id"] = reply_to_msg_id
        result = telegram_request(token, "sendMessage", payload)
        if first_message_id is None and result.get("ok"):
            first_message_id = result.get("result", {}).get("message_id")
    return first_message_id


def send_ai_formatted_response(token, chat_id, answer, reply_to_msg_id=None):
    """Send prose normally and fenced code as native copyable Telegram blocks."""
    answer = answer or "Пустой ответ от модели."
    pattern = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)
    matches = list(pattern.finditer(answer))

    if not matches:
        if _looks_like_standalone_code(answer):
            return _send_copyable_code_block(
                token, chat_id, answer.strip(), "text", reply_to_msg_id=reply_to_msg_id
            )
        return _send_plain_ai_text(token, chat_id, answer, reply_to_msg_id)

    first_message_id = None
    cursor = 0
    for match in matches:
        prose = answer[cursor:match.start()].strip()
        if prose:
            sent_id = _send_plain_ai_text(
                token, chat_id, prose,
                reply_to_msg_id if first_message_id is None else None,
            )
            first_message_id = first_message_id or sent_id

        header = (match.group(1) or "").strip()
        code = match.group(2).strip("\n")
        parsed = parse_code_blocks(match.group(0))
        if parsed:
            lang, filename, code = parsed[0]
        else:
            parts = header.split()
            lang = parts[0] if parts else "text"
            filename = ""

        sent_id = _send_copyable_code_block(
            token, chat_id, code, lang, filename,
            reply_to_msg_id if first_message_id is None else None,
        )
        first_message_id = first_message_id or sent_id
        cursor = match.end()

    tail = answer[cursor:].strip()
    if tail:
        sent_id = _send_plain_ai_text(
            token, chat_id, tail,
            reply_to_msg_id if first_message_id is None else None,
        )
        first_message_id = first_message_id or sent_id

    return first_message_id


def send_code_prompt(token, chat_id, reply_to_msg_id):
    payload = {
        "chat_id": chat_id,
        "text": "✅ Код выше оформлен блоком — нажмите на него, чтобы скопировать. Дополнительно отправить?",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "📦 ZIP-файлом", "callback_data": "code_file"},
                {"text": "📋 Повторить блоками", "callback_data": "code_text"},
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
    first = True
    for lang, filename, code in blocks:
        _send_copyable_code_block(
            token,
            chat_id,
            code,
            lang,
            filename,
            reply_to_msg_id if first else None,
        )
        first = False


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

    # ---- Shop callbacks ----
    if data == "shop_free":
        if is_pro_user(user_id):
            remove_pro_user(user_id)
            text = "\U0001F535 Вы переключены на <b>Free</b>-тариф."
        else:
            text = "\U0001F535 У вас уже бесплатный тариф."
        telegram_request(token, "editMessageText", {
            "chat_id": chat_id, "message_id": msg_id, "text": text, "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": [[
                {"text": "\u25C0 В магазин", "callback_data": "shop_main"},
            ]]},
        })
        return

    if data == "shop_pro_info":
        if is_pro_user(user_id):
            days = pro_days_left(user_id)
            text = f"\u2B50\uFE0F У вас уже активна Pro-подписка! Осталось {days} дн."
            telegram_request(token, "editMessageText", {
                "chat_id": chat_id, "message_id": msg_id, "text": text, "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": [[
                    {"text": "\u25C0 В магазин", "callback_data": "shop_main"},
                ]]},
            })
        else:
            text = (
                "\u2B50 <b>Покупка Pro-подписки</b>\n\n"
                "\u2022 Модель: Cerebras Llama 3.1 70B\n"
                "\u2022 Лимит: 10 000 токенов / 12ч\n"
                "\u2022 Срок: 30 дней\n"
                "\u2022 Цена: 100 \u2B50\n\n"
                "\u26A0\uFE0F <b>Важно:</b>\n"
                "\u2022 Возврат средств невозможен\n"
                "\u2022 Покупка является добровольной\n"
                "\u2022 После оплаты подписка активируется автоматически\n"
                "\u2022 Оплата через Telegram Stars\n\n"
                "\U0001F4E6 Создаю счёт..."
            )
            telegram_request(token, "editMessageText", {
                "chat_id": chat_id, "message_id": msg_id, "text": text, "parse_mode": "HTML",
            })
            result = telegram_request(token, "sendInvoice", {
                "chat_id": chat_id,
                "title": "\u2B50 ZeroxAI Pro",
                "description": (
                    "\u2714\uFE0F Доступ к Cerebras Llama 3.1 70B\n"
                    "\u2714\uFE0F Более умные и развёрнутые ответы\n"
                    "\u2714\uFE0F Приоритетная обработка запросов\n"
                    "\u2714\uFE0F На 30 дней"
                ),
                "payload": f"pro_{user_id}",
                "provider_token": "",
                "currency": "XTR",
                "prices": [{"label": "\u2B50 ZeroxAI Pro", "amount": 100}],
            })
            if not result.get("ok"):
                telegram_request(token, "editMessageText", {
                    "chat_id": chat_id, "message_id": msg_id,
                    "text": f"\u274C Ошибка при создании счёта: {result.get('description', 'неизвестно')}",
                    "parse_mode": "HTML",
                    "reply_markup": {"inline_keyboard": [[
                        {"text": "\u25C0 Назад", "callback_data": "shop_main"},
                    ]]},
                })
        return

    if data == "shop_tokens":
        text = (
            "\U0001F916 <b>Купить токены</b>\n\n"
            "\uD83D\uDFE2 50 токенов — ⭐1\n"
            "\uD83D\uDFE2 100 токенов — ⭐2\n"
            "\uD83D\uDFE2 250 токенов — ⭐4\n"
            "\uD83D\uDFE2 500 токенов — ⭐7\n"
            "\uD83D\uDFE2 1000 токенов — ⭐12\n\n"
            "Докупленные токены не сгорают.\n\n"
            "Выберите количество:"
        )
        telegram_request(token, "editMessageText", {
            "chat_id": chat_id, "message_id": msg_id, "text": text, "parse_mode": "HTML",
            "reply_markup": {
                "inline_keyboard": [
                    [
                        {"text": "50 ⭐1", "callback_data": "shop_tokens_buy_50"},
                        {"text": "100 ⭐2", "callback_data": "shop_tokens_buy_100"},
                    ],
                    [
                        {"text": "250 ⭐4", "callback_data": "shop_tokens_buy_250"},
                        {"text": "500 ⭐7", "callback_data": "shop_tokens_buy_500"},
                    ],
                    [
                        {"text": "1000 ⭐12", "callback_data": "shop_tokens_buy_1000"},
                    ],
                    [
                        {"text": "\u270F\uFE0F Своё количество", "callback_data": "shop_tokens_custom"},
                    ],
                    [
                        {"text": "\u25C0 Назад", "callback_data": "shop_main"},
                    ],
                ]
            },
        })
        return

    if data == "shop_tokens_custom":
        PENDING_TOKEN_AMOUNTS[user_id] = True
        text = "\u270F\uFE0F Введите нужное количество токенов (число, от 50):"
        telegram_request(token, "editMessageText", {
            "chat_id": chat_id, "message_id": msg_id, "text": text, "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": [[
                {"text": "\u25C0 Назад", "callback_data": "shop_tokens"},
            ]]},
        })
        return

    if data.startswith("shop_tokens_buy_"):
        try:
            amount = int(data.split("_")[-1])
        except (ValueError, IndexError):
            return
        fixed = {50: 1, 100: 2, 250: 4, 500: 7, 1000: 12}
        stars = fixed.get(amount, max(1, (amount + TOKEN_STARS_RATE - 1) // TOKEN_STARS_RATE))
        result = telegram_request(token, "sendInvoice", {
            "chat_id": chat_id,
            "title": f"\U0001F916 {amount} токенов",
            "description": f"Пополнение токенов ZeroxAI — {amount} токенов",
            "payload": f"tokens_{user_id}_{amount}",
            "provider_token": "",
            "currency": "XTR",
            "prices": [{"label": f"\U0001F916 {amount} токенов", "amount": stars}],
        })
        if not result.get("ok"):
            telegram_request(token, "editMessageText", {
                "chat_id": chat_id, "message_id": msg_id,
                "text": f"\u274C Ошибка: {result.get('description', 'неизвестно')}",
            })
        return

    if data == "shop_main":
        shop(token, chat_id, user_id)
        return

    # ---- VPN callbacks ----
    if data == "vpn_main":
        vpn_menu(token, chat_id, user_id, msg_id)
        return
    if data == "vpn_refresh":
        vpn_menu(token, chat_id, user_id, msg_id)
        return
    if data == "vpn_settings":
        vpn_show_settings(token, chat_id, user_id, msg_id)
        return
    if data == "vpn_disconnect":
        vpn_disconnect_user(token, chat_id, user_id, msg_id)
        return
    if data == "vpn_quick":
        servers = vpn_get_servers()
        if servers:
            best = vpn_auto_select(servers)
            if best:
                vpn_connect_user(token, chat_id, user_id, best["id"], msg_id)
            else:
                telegram_request(token, "editMessageText", {
                    "chat_id": chat_id, "message_id": msg_id,
                    "text": "❌ Не удалось найти доступный сервер.",
                    "reply_markup": {"inline_keyboard": [[{"text": "◀ Назад", "callback_data": "vpn_main"}]]},
                })
        else:
            telegram_request(token, "editMessageText", {
                "chat_id": chat_id, "message_id": msg_id,
                "text": "❌ Нет доступных серверов. Администратор может добавить их через /vpn_addserver",
                "reply_markup": {"inline_keyboard": [[{"text": "◀ Назад", "callback_data": "vpn_main"}]]},
            })
        return
    if data == "vpn_auto":
        servers = vpn_get_servers()
        if servers:
            best = vpn_auto_select(servers)
            if best:
                vpn_connect_user(token, chat_id, user_id, best["id"], msg_id)
            else:
                telegram_request(token, "editMessageText", {
                    "chat_id": chat_id, "message_id": msg_id,
                    "text": "❌ Автовыбор не дал результатов.",
                    "reply_markup": {"inline_keyboard": [[{"text": "◀ Назад", "callback_data": "vpn_main"}]]},
                })
        else:
            telegram_request(token, "editMessageText", {
                "chat_id": chat_id, "message_id": msg_id,
                "text": "❌ Нет серверов.",
                "reply_markup": {"inline_keyboard": [[{"text": "◀ Назад", "callback_data": "vpn_main"}]]},
            })
        return
    if data.startswith("vpn_server_"):
        sid = int(data.split("_")[-1])
        vpn_show_server(token, chat_id, user_id, sid, msg_id)
        return
    if data.startswith("vpn_connect_"):
        sid = int(data.split("_")[-1])
        vpn_connect_user(token, chat_id, user_id, sid, msg_id)
        return
    if data.startswith("vpn_fav_"):
        sid = int(data.split("_")[-1])
        vpn_toggle_favorite(user_id, sid)
        vpn_show_server(token, chat_id, user_id, sid, msg_id)
        return
    if data.startswith("vpn_dlconf_"):
        sid = int(data.split("_")[-1])
        cfg = vpn_get_user_config(user_id, sid)
        if cfg and cfg.get("config_text"):
            server = vpn_get_server(sid)
            country = server["country"] if server else "?"
            fname = f"zeroxai_vpn_{country.lower()}.conf"
            telegram_upload(token, "sendDocument",
                {"chat_id": str(chat_id), "caption": f"📁 {country} · WireGuard config"},
                "document", cfg["config_text"].encode("utf-8"), fname, "text/plain",
            )
            telegram_request(token, "answerCallbackQuery", {
                "callback_query_id": cq_id,
                "text": f"✅ Файл {fname} отправлен!",
            })
        else:
            telegram_request(token, "answerCallbackQuery", {
                "callback_query_id": cq_id,
                "text": "❌ Сначала подключитесь к серверу",
                "show_alert": True,
            })
        return
    if data == "vpn_noop":
        telegram_request(token, "answerCallbackQuery", {"callback_query_id": cq_id, "text": ""})
        return

    if data.startswith("vpn_qr_"):
        sid = int(data.split("_")[-1])
        cfg = vpn_get_user_config(user_id, sid)
        if cfg and cfg.get("config_text"):
            server = vpn_get_server(sid)
            flag = server["flag"] if server else "🌍"
            country = server["country"] if server else ""
            city = server["city"] if server else ""
            import urllib.parse
            config_encoded = urllib.parse.quote(cfg["config_text"])
            qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=400x400&data={config_encoded}"
            qr_text = (
                f"📱 <b>QR-код для WireGuard</b>\n\n"
                f"{flag} <b>{country} · {city}</b>\n\n"
                f"1. Установи WireGuard\n"
                f"2. Нажми <b>➕</b> → <b>Сканировать QR</b>\n"
                f"3. Наведи камеру на код\n\n"
                f"⚡ Работает на iPhone, Android, Windows, Mac"
            )
            telegram_request(token, "sendPhoto", {
                "chat_id": chat_id,
                "photo": qr_url,
                "caption": qr_text,
                "parse_mode": "HTML",
            })
        else:
            telegram_request(token, "answerCallbackQuery", {
                "callback_query_id": cq_id,
                "text": "❌ Сначала подключитесь к серверу",
                "show_alert": True,
            })
        return

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
                    {"text": "\U0001F6D2 Магазин", "callback_data": "shop_main"},
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
    "/role", "/staff", "/setrules",
    "/ticket", "/closeticket", "/feedback", "/announce", "/userinfo", "/support",
    "/clean", "/pin", "/unpin", "/slowmode", "/say", "/welcome", "/delete", "/banlist", "/shop",
    "/joke", "/coin", "/dice", "/roll", "/choose", "/8ball", "/hug", "/slap", "/quote", "/meme",
    "/free", "/promo", "/bal", "/balance", "/slot", "/allin",
    "/transfer", "/give", "/send",
    "/addcoin", "/addmoney", "/removecoin", "/removemoney",
    "/stopcasino", "/startcasino", "/stopbot", "/startbot", "/statbot", "/tokens",
    "/server", "/addsticker", "/mypro", "/buypro", "/shop",
 "/top", "/ben", "/grantpro", "/luckset", "/resettokens", "/buy", "/info",
    "/hide", "/savehistory", "/answer",
    "/giveall", "/addcoin", "/testshop", "/logs", "/setsub",
    "/setlocalmodel", "/trainmodel",
    "/project",
    "/vpn", "/vpn_addserver", "/vpn_delserver", "/vpn_servers",
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
                       f"Pro-юзерам будет {'Qwen2.5-Coder (локально)' if _LOCAL_PRO_MODE else 'Mistral 7B (OpenRouter)'}. "
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

            # ─── VPN commands ───
            if cmd == "/vpn":
                vpn_menu(token, chat_id, user_id)
                return True

            if cmd == "/vpn_addserver":
                if user_id != OWNER_ID:
                    reply("❌ Только владелец может добавлять серверы.")
                    return True
                if len(args) < 5:
                    reply(
                        "📋 <b>Добавление VPN-сервера</b>\n\n"
                        "Использование:\n"
                        "<code>/vpn_addserver &lt;host&gt; &lt;страна&gt; &lt;город&gt; &lt;публичный_ключ&gt; &lt;endpoint&gt; [порт]</code>\n\n"
                        "Пример:\n"
                        "<code>/vpn_addserver 203.0.113.1 Германия Франкфурт fE38...= 203.0.113.1 51820</code>\n\n"
                        "💡 <b>Бесплатные VPS для сервера:</b>\n"
                        "• Oracle Cloud Always Free — oracle.com/cloud/free\n"
                        "• Google Cloud Free Tier — cloud.google.com/free\n"
                        "• AWS Free Tier — aws.amazon.com/free\n\n"
                        "📜 Скрипт развёртывания: <code>/vpn_deploy</code>",
                        parse_mode="HTML"
                    )
                    return True
                host = args[0]
                country = args[1]
                city = args[2]
                pubkey = args[3]
                endpoint = args[4]
                port = int(args[5]) if len(args) > 5 else VPN_WG_PORT
                ok = vpn_add_server(host, country, city, pubkey, endpoint, port)
                if ok:
                    reply(f"✅ Сервер {host} ({country}, {city}) добавлен!")
                else:
                    reply("❌ Ошибка при добавлении сервера.")
                return True

            if cmd == "/vpn_delserver":
                if user_id != OWNER_ID:
                    reply("❌ Только владелец.")
                    return True
                if not args:
                    reply("Использование: /vpn_delserver <id>\n\nID серверов:\n" + "\n".join(f"{s['id']}: {s['country']} {s['city']} ({s['host']})" for s in vpn_get_servers()))
                    return True
                try:
                    sid = int(args[0])
                    if vpn_remove_server(sid):
                        reply(f"✅ Сервер #{sid} удалён.")
                    else:
                        reply(f"❌ Сервер #{sid} не найден.")
                except ValueError:
                    reply("❌ ID должен быть числом.")
                return True

            if cmd == "/vpn_servers":
                servers = vpn_get_servers()
                if not servers:
                    reply("❌ Нет добавленных серверов.")
                    return True
                text = "📡 <b>VPN серверы:</b>\n\n"
                for s in servers:
                    flag = s.get("flag", "🌍")
                    ping_str = f" ({s['ping_ms']}ms)" if s.get("ping_ms") else ""
                    text += f"#{s['id']} {flag} {s['country']}, {s['city']}{ping_str}\n<code>{s['host']}:{s['port']}</code>\n\n"
                reply(text.strip(), parse_mode="HTML")
                return True

            if cmd == "/vpn_deploy":
                if user_id != OWNER_ID:
                    reply("❌ Только владелец.")
                    return True
                script = vpn_deploy_script()
                text = (
                    "📜 <b>Скрипт развёртывания WireGuard сервера</b>\n\n"
                    "1. Зайдите на свежий Ubuntu VPS по SSH\n"
                    "2. Выполните следующие команды:\n\n"
                    f"<pre lang=\"bash\">{script}</pre>\n\n"
                    "3. После выполнения скопируйте <b>Server Public Key</b> "
                    "и добавьте сервер через /vpn_addserver"
                )
                # Split if too long
                for part in split_message(text):
                    reply(part, parse_mode="HTML")
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
            name = user.get("first_name") or user.get("username") or "пользователь"
            kb = _menu_kb(is_pm=not is_group)
            reply(
                f"⚡ ZEROXAI PROJECT STUDIO\n\n"
                f"Привет, {name}! Я создаю и исправляю полноценные проекты:\n"
                "🧩 MCBE/MCPE, PocketMine, Nukkit и Java-плагины\n"
                "🌐 сайты и web-приложения\n"
                "🤖 Telegram-боты и Python-проекты\n"
                "📱 Android-приложения\n"
                "🛠 диагностика ошибок и сборка ZIP-архивов\n\n"
                "Напиши задачу обычным сообщением или используй:\n"
                "/project <подробное описание>\n\n"
                "Создатель: Эрик Арутюнян",
                reply_markup=kb,
            )
            return True

        if cmd == "/about":
            reply(
                "⚡ ZeroxAI v3.0\n\n"
                "AI-ассистент и Project Studio для разработки, дизайна и отладки проектов.\n"
                "Создатель: Эрик Арутюнян\n\n"
                "✅ ZeroxAI Free — обычные ответы и проекты\n"
                "⭐ ZeroxAI Pro — более мощная модель и увеличенные лимиты"
            )
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

        if cmd == "/shop":
            shop(token, chat_id, user_id)
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
                            if isinstance(uid, str) and uid.startswith("@"):
                                names.append(uid)
                                continue
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
                     "/project <описание> — собрать полноценный ZIP-проект",
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
            target_orig = target
            if isinstance(target, str) and target.startswith("@"):
                resolved = resolve_username(token, target, chat_id)
                if resolved:
                    target = resolved
            if isinstance(target, str) and target.startswith("@"):
                try:
                    r = telegram_request(token, "restrictChatMember", {
                        "chat_id": chat_id, "user_id": target,
                        "permissions": {"can_send_messages": False},
                    })
                    if r and r.get("ok"):
                        reply(f"\U0001F507 {target_orig} заглушён.")
                        return True
                except Exception:
                    pass
                reply(f"\u2757 @{target.lstrip('@')} не отвечал боту — нет ID. Ответьте на сообщение пользователя.")
                return True
            minutes = 60
            if len(args) >= 2:
                try:
                    minutes = max(1, int(re.sub(r"\D", "", args[1])))
                except:
                    pass
            import time
            until = int(time.time()) + minutes * 60
            try:
                telegram_request(token, "restrictChatMember", {
                    "chat_id": chat_id, "user_id": target,
                    "permissions": {"can_send_messages": False},
                    "until_date": until,
                })
                cd = get_chat_data(chat_id)
                cd.setdefault("muted", {})
                cd["muted"][str(target)] = {"until": until, "minutes": minutes}
                save_data()
                reply(f"\U0001F507 {target_orig} заглушён на {format_minutes_duration(minutes)}.")
            except Exception as e:
                reply(f"\u274C Ошибка: {e}")
            return True

        if cmd == "/unmute":
            if not require(6): return True
            target = parse_user_ref(message, args)
            if not target:
                reply("Укажите пользователя.")
                return True
            target_orig = target
            if isinstance(target, str) and target.startswith("@"):
                resolved = resolve_username(token, target, chat_id)
                if resolved:
                    target = resolved
            if isinstance(target, str) and target.startswith("@"):
                try:
                    r = telegram_request(token, "restrictChatMember", {
                        "chat_id": chat_id, "user_id": target,
                        "permissions": {
                            "can_send_messages": True, "can_send_media_messages": True,
                            "can_send_polls": True, "can_send_other_messages": True,
                            "can_add_web_page_previews": True,
                        },
                    })
                    if r and r.get("ok"):
                        reply(f"\U0001F50A {target_orig} разглушён.")
                        return True
                except Exception:
                    pass
                reply(f"\u2757 @{target.lstrip('@')} не отвечал боту — нет ID. Ответьте на сообщение пользователя.")
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
                reply(f"\U0001F50A {target_orig} разглушён.")
            except Exception as e:
                reply(f"\u274C Ошибка: {e}")
            return True

        if cmd == "/kick":
            if not require(6): return True
            target = parse_user_ref(message, args)
            if not target:
                reply("Укажите пользователя.")
                return True
            target_orig = target
            if isinstance(target, str) and target.startswith("@"):
                resolved = resolve_username(token, target, chat_id)
                if resolved:
                    target = resolved
            if isinstance(target, str) and target.startswith("@"):
                try:
                    r = telegram_request(token, "banChatMember", {"chat_id": chat_id, "user_id": target})
                    if r and r.get("ok"):
                        telegram_request(token, "unbanChatMember", {"chat_id": chat_id, "user_id": target})
                        reply(f"\U0001F4A2 {target_orig} кикнут.")
                        return True
                except Exception:
                    pass
                reply(f"\u2757 @{target.lstrip('@')} не отвечал боту — нет ID. Ответьте на сообщение пользователя.")
                return True
            try:
                telegram_request(token, "banChatMember", {"chat_id": chat_id, "user_id": target})
                telegram_request(token, "unbanChatMember", {"chat_id": chat_id, "user_id": target})
                reason = cmd_text
                reply(f"\U0001F4A2 {target_orig} кикнут.{f' Причина: {reason}' if reason else ''}")
            except Exception as e:
                reply(f"\u274C Ошибка: {e}")
            return True

        if cmd == "/ban":
            if not require(6): return True
            target = parse_user_ref(message, args)
            if not target:
                reply("Укажите пользователя.")
                return True
            target_orig = target
            if isinstance(target, str) and target.startswith("@"):
                resolved = resolve_username(token, target, chat_id)
                if resolved:
                    target = resolved
            if isinstance(target, str) and target.startswith("@"):
                try:
                    r = telegram_request(token, "banChatMember", {"chat_id": chat_id, "user_id": target})
                    if r and r.get("ok"):
                        cd = get_chat_data(chat_id)
                        cd.setdefault("banned", [])
                        if target not in cd["banned"]:
                            cd["banned"].append(target)
                        save_data()
                        reply(f"\U0001F534 {target_orig} забанен.")
                        return True
                except Exception:
                    pass
                reply(f"\u2757 @{target.lstrip('@')} не отвечал боту — нет ID. Ответьте на сообщение пользователя.")
                return True
            try:
                telegram_request(token, "banChatMember", {"chat_id": chat_id, "user_id": target})
                cd = get_chat_data(chat_id)
                cd.setdefault("banned", [])
                if str(target) not in cd["banned"]:
                    cd["banned"].append(str(target))
                save_data()
                reason = cmd_text
                reply(f"\U0001F534 {target_orig} забанен.{f' Причина: {reason}' if reason else ''}")
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
                if len(args) < 3:
                    reply("Использование: /role add <название> <уровень (1-11)>")
                    return True
                if not require(8): return True
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
                if len(args) < 2:
                    reply("Использование: /role remove <название>")
                    return True
                if not require(8): return True
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
                if len(args) < 3:
                    reply("Использование: /role give <пользователь> <роль>")
                    return True
                if not require(8): return True
                target = parse_user_ref(message, args[1:])
                if not target:
                    reply("Укажите пользователя.")
                    return True
                target_orig = target
                if isinstance(target, str) and target.startswith("@"):
                    resolved = resolve_username(token, target, chat_id)
                    if resolved:
                        target = resolved
                role_name = args[-1]
                cd = get_chat_data(chat_id)
                if role_name not in cd["roles"]:
                    reply(f"Роль «{role_name}» не найдена. Создайте её через /role add")
                    return True
                sid = str(target)
                # remove old role for this user (same key or @username keys that resolve to same ID)
                for k in list(cd.get("users", {}).keys()):
                    if k == sid or (k.startswith("@") and resolve_username(token, k, chat_id) == target):
                        del cd["users"][k]
                cd["users"][sid] = role_name
                save_data()
                reply(f"\u2705 Пользователю {target_orig} выдана роль «{role_name}».")
                return True

            if sub == "take":
                if len(args) < 3:
                    reply("Использование: /role take <пользователь> <роль>")
                    return True
                if not require(8): return True
                target = parse_user_ref(message, args[1:])
                if not target:
                    reply("Укажите пользователя.")
                    return True
                target_orig = target
                if isinstance(target, str) and target.startswith("@"):
                    resolved = resolve_username(token, target, chat_id)
                    if resolved:
                        target = resolved
                role_name = args[-1]
                cd = get_chat_data(chat_id)
                sid = str(target)
                # check both exact key and any @username keys that might resolve
                found = cd.get("users", {}).get(sid)
                if not found:
                    for k, v in list(cd.get("users", {}).items()):
                        if k.startswith("@") and resolve_username(token, k, chat_id) == target:
                            found = v
                            sid = k
                            break
                if not found or found != role_name:
                    reply(f"У пользователя {target_orig} нет роли «{role_name}».")
                    return True
                del cd["users"][sid]
                save_data()
                reply(f"\u2705 У пользователя {target_orig} забрана роль «{role_name}».")
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
                            if isinstance(uid, str) and uid.startswith("@"):
                                names.append(uid)
                                continue
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

            if sub == "chapter":
                if len(args) < 2:
                    reply("Использование: /role chapter create/delete/list")
                    return True
                if not require(8): return True
                csub = args[1]
                cd = get_chat_data(chat_id)
                chapters = cd.setdefault("chapters", [])
                if csub == "create":
                    rest = " ".join(args[2:]) if len(args) > 2 else ""
                    m = __import__("re").match(r'"(.+)"\s*(\d+)-(\d+)', rest)
                    if not m:
                        reply('Использование: /role chapter create "Название" 1-5')
                        return True
                    ch_name = m.group(1)
                    lstart, lend = int(m.group(2)), int(m.group(3))
                    if any(c["name"].lower() == ch_name.lower() for c in chapters):
                        reply(f"Раздел «{ch_name}» уже существует.")
                        return True
                    chapters.append({"name": ch_name, "level_start": lstart, "level_end": lend})
                    save_data()
                    reply(f"✅ Раздел «{ch_name}» создан (уровни {lstart}-{lend}).")
                    return True
                if csub == "delete":
                    if len(args) < 3:
                        reply("Укажите название раздела.")
                        return True
                    ch_name = args[2]
                    for i, c in enumerate(chapters):
                        if c["name"].lower() == ch_name.lower():
                            chapters.pop(i)
                            save_data()
                            reply(f"✅ Раздел «{ch_name}» удалён.")
                            return True
                    reply(f"Раздел «{ch_name}» не найден.")
                    return True
                if csub == "list":
                    if not chapters:
                        reply("Нет созданных разделов.")
                        return True
                    lines = ["📂 Разделы:"]
                    for c in chapters:
                        lines.append(f"• {c['name']} — уровни {c['level_start']}-{c['level_end']}")
                    reply("\n".join(lines))
                    return True
                reply("Использование: /role chapter create/delete/list")
                return True

            reply("Подкоманда не распознана. Используйте: add, remove, give, take, list, info, chapter")
            return True

        if cmd == "/staff":
            cd = get_chat_data(chat_id)
            if not cd.get("users"):
                reply("\u0412 \u0447\u0430\u0442\u0435 \u043d\u0435\u0442 \u043d\u0430\u0437\u043d\u0430\u0447\u0435\u043d\u043d\u044b\u0445 \u0440\u043e\u043b\u0435\u0439.")
                return True
            roles_map = cd.get("roles", {})
            by_role = {}
            for uid, rname in cd["users"].items():
                by_role.setdefault(rname, []).append(uid)
            ROLE_EMOJI = {
                "\u0433\u043b\u0430\u0432\u043d\u044b\u0439 \u0432\u043b\u0430\u0434\u0435\u043b\u0435\u0446": "\u2b50",
                "\u0432\u043b\u0430\u0434\u0435\u043b\u0435\u0446": "\U0001F451",
                "\u0441\u043e.\u0432\u043b\u0430\u0434\u0435\u043b\u0435\u0446": "\U0001F91D",
                "\u0441\u043e\u0432\u043b\u0430\u0434\u0435\u043b\u0435\u0446": "\U0001F91D",
                "\u0437\u0430\u043c\u0435\u0441\u0442\u0438\u0442\u0435\u043b\u044c \u0432\u043b\u0430\u0434\u0435\u043b\u044c\u0446\u0430": "\U0001F451",
                "\u0433\u043b\u0430\u0432\u043d\u044b\u0439 \u0437\u0430\u043c\u0435\u0441\u0442\u0438\u0442\u0435\u043b\u044c": "\U0001F451",
                "\u043a\u043e\u0434\u0435\u0440": "\U0001F5A5",
                "\u043c\u043e\u0434\u0435\u0440\u0430\u0442\u043e\u0440": "\U0001F6E1",
                "\u0430\u0434\u043c\u0438\u043d": "\U0001F4A0",
                "\u043c\u0435\u043d\u0435\u0434\u0436\u0435\u0440": "\U0001F4BC",
                "\u043f\u043e\u043c\u043e\u0449\u043d\u0438\u043a": "\U0001F4A1",
                "\u0445\u0435\u043b\u043f\u0435\u0440": "\U0001F4A1",
                "\u0431\u0443\u0441\u0442\u0435\u0440": "\U0001F4A0",
                "\u0432\u0438\u043f": "\U0001F4BC",
                "\u044e\u0442\u0443\u0431\u0435\u0440": "\U0001F3AC",
                "\u0441\u0442\u0440\u0438\u043c\u0435\u0440": "\U0001F4FA",
                "\u0445\u0443\u0434\u043e\u0436\u043d\u0438\u043a": "\U0001F3A8",
                "\u0431\u0438\u043b\u0434\u0435\u0440": "\U0001F3D7",
                "\u0434\u0438\u0437\u0430\u0439\u043d\u0435\u0440": "\U0001F3A8",
                "\u043f\u0438\u0430\u0440": "\U0001F4E2",
                "\u0442\u0438\u043c\u043b\u0438\u0434": "\U0001F465",
                "\u043b\u0438\u0434\u0435\u0440": "\U0001F465",
                "\u043a\u043e\u043d\u0441\u0443\u043b\u044c\u0442\u0430\u043d\u0442": "\u2139",
                "\u0438\u043d\u0436\u0435\u043d\u0435\u0440": "\u2699",
                "\u0441\u043f\u043e\u043d\u0441\u043e\u0440": "\U0001F4B0",
                "\u0433\u0435\u0439\u043c\u0435\u0440": "\U0001F3AE",
                "\u0442\u0435\u0441\u0442\u0435\u0440": "\u2705",
                "\u0434\u043e\u043d\u0430\u0442\u0435\u0440": "\U0001F4B8",
            }
            def role_emoji(rname):
                key = rname.lower().strip()
                if key in ROLE_EMOJI:
                    return ROLE_EMOJI[key]
                for pattern, emoji in ROLE_EMOJI.items():
                    if pattern in key or key in pattern:
                        return emoji
                return "\U0001F539"
            def fmt_member(uid):
                if isinstance(uid, str) and uid.startswith("@"):
                    return f"\u2022 {uid}"
                try:
                    member = telegram_request(token, "getChatMember", {"chat_id": chat_id, "user_id": int(uid)})
                    u = member.get("result", {}).get("user", {})
                    username = u.get("username", "")
                    if username:
                        return f"\u2022 @{username}"
                    name = u.get("first_name") or f"id{uid}"
                    return f"\u2022 {name}"
                except Exception:
                    return f"\u2022 id{uid}"
            chapters = cd.get("chapters", [])
            sorted_roles = sorted(by_role.keys(), key=lambda x: -roles_map.get(x, 0))
            lines = ["\U0001F465 \u0421\u043e\u0441\u0442\u0430\u0432 \u043a\u043e\u043c\u0430\u043d\u0434\u044b:"]
            in_chapter = set()
            for ch in chapters:
                ch_roles = [r for r in sorted_roles if ch["level_start"] <= roles_map.get(r, 0) <= ch["level_end"]]
                in_chapter.update(ch_roles)
            sorted_chapters = sorted(chapters, key=lambda c: -c["level_end"])
            for ch in sorted_chapters:
                ch_roles = [r for r in sorted_roles if ch["level_start"] <= roles_map.get(r, 0) <= ch["level_end"]]
                if not ch_roles:
                    continue
                lines.append(f"\n\U0001F4C2 {ch['name']} (\u0443\u0440\u043e\u0432\u043d\u0438 {ch['level_start']}-{ch['level_end']}):")
                for rname in ch_roles:
                    uids = by_role[rname]
                    level = roles_map.get(rname, "?")
                    em = role_emoji(rname)
                    lines.append(f"{em} {rname} \u2014 \u0423\u0440\u043e\u0432\u0435\u043d\u044c {level}")
                    for uid in uids:
                        lines.append(fmt_member(uid))
                    lines.append("")
            remaining = [r for r in sorted_roles if r not in in_chapter]
            if remaining:
                if chapters:
                    lines.pop()
                max_lvl = max(roles_map.get(r, 1) for r in remaining)
                lines.append(f"\n\U0001F4C2 \u041D\u0435 \u0438\u0437\u0432\u0435\u0441\u0442\u0435\u043d (\u0443\u0440\u043e\u0432\u043d\u0438 1-{max_lvl}):")
                first = True
                for rname in remaining:
                    if not first:
                        lines.append("")
                    first = False
                    uids = by_role[rname]
                    level = roles_map.get(rname, "?")
                    em = role_emoji(rname)
                    lines.append(f"{em} {rname} \u2014 \u0423\u0440\u043e\u0432\u0435\u043d\u044c {level}")
                    for uid in uids:
                        lines.append(fmt_member(uid))
            reply("\n".join(lines).strip())
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
                payload = (msg.get("successful_payment") or {}).get("invoice_payload", "")
                if user_id:
                    if payload.startswith("tokens_"):
                        parts = payload.split("_")
                        if len(parts) >= 3:
                            try:
                                amount = int(parts[-1])
                                add_bonus_tokens(user_id, amount)
                                reply_message(token, msg["chat"]["id"],
                                    f"\U0001F916 Пополнение на {amount} токенов успешно!",
                                    msg.get("message_id"))
                            except ValueError:
                                pass
                    else:
                        add_pro_user(user_id)
                        reply_message(token, msg["chat"]["id"],
                            "\u2B50\uFE0F Поздравляю! Вы стали Pro-пользователем! "
                            "Теперь вы используете Cerebras Llama 3.1 70B.", msg.get("message_id"))
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
    uname = user.get("username", "")
    if uname:
        _username_cache[uname.lower()] = user_id

    if not chat_id:
        return
    # track new chat members (join events)
    new_members = message.get("new_chat_members")
    if new_members:
        for m in new_members:
            mid = m.get("id")
            muname = m.get("username", "")
            if mid and muname:
                _username_cache[muname.lower()] = mid
                try:
                    with db_cursor() as cur:
                        cur.execute(
                            "INSERT INTO users (user_id, balance, username) VALUES (%s, 0, %s) "
                            "ON CONFLICT (user_id) DO UPDATE SET username = %s WHERE users.username != %s",
                            (mid, muname, muname, muname)
                        )
                except Exception:
                    pass
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
        if rcon and not text.startswith(("/server", "/startbot", "/stopbot", "/project")):
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

    force_project = False
    if text.lower().startswith("/project"):
        description = text[len(text.split()[0]):].strip() if text.split() else ""
        if not description:
            reply_message(
                token,
                chat_id,
                "🚀 Опишите проект после команды.\n\nПример:\n/project Создай плагин PocketMine-MP 5 для системы авторизации с config.yml и README",
                message.get("message_id"),
                reply_markup=_menu_kb(is_pm=not is_group),
            )
            return
        force_project = True
        text = description  # strip /project prefix before AI processing

    # handle commands (skip /project since it's handled above)
    if text.startswith("/") and not text.lower().startswith("/project"):
        cmd_name = text.split()[0]
        if "@" in cmd_name:
            text = text.replace(cmd_name, cmd_name.split("@")[0], 1)
        if handle_command(token, message, chat, user, chat_id, user_id, text):
            return
        return  # don't send unknown commands to AI

    # handle reply keyboard buttons
    km = _menu_kb(is_pm=not is_group)
    if text == "🚀 Создать проект":
        reply_message(
            token,
            chat_id,
            "🚀 Напишите, что нужно создать. Укажите платформу, версию и функции.\n\nПример: `Создай плагин для PocketMine-MP 5.0 с командами /login и /register, конфигом и README`",
            message.get("message_id"),
            reply_markup=km,
        )
        return
    if text == "✨ Возможности":
        reply_message(
            token,
            chat_id,
            "✨ Возможности ZeroxAI\n\n"
            "• полноценные ZIP-проекты\n"
            "• MCBE/MCPE: PocketMine, Submarine, EnvyCore, Nukkit, Add-Ons\n"
            "• Minecraft Java: Paper, Purpur, Fabric, datapacks\n"
            "• сайты, игры, Telegram-боты и Android\n"
            "• исправление багов по коду и логам\n"
            "• адаптивный UI/UX и документация",
            message.get("message_id"),
            reply_markup=km,
        )
        return
    if text in ("⭐ Подписка", "🤖 Токены", "\u2B50 Подписка", "\U0001F916 Токены"):
        if text in ("⭐ Подписка", "\u2B50 Подписка"):
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

    if text == "🛒 Магазин":
        shop(token, chat_id, user_id)
        return

    if text == "🌍 VPN":
        vpn_menu(token, chat_id, user_id)
        return

    # custom token amount input
    if PENDING_TOKEN_AMOUNTS.get(user_id):
        del PENDING_TOKEN_AMOUNTS[user_id]
        try:
            amount = int(text.strip())
            if amount < 50:
                reply_message(token, chat_id, "\u274C Минимум 50 токенов.", message.get("message_id"), reply_markup=km)
                return
        except ValueError:
            reply_message(token, chat_id, "\u274C Введите число (например, 150).", message.get("message_id"), reply_markup=km)
            return
        stars = max(1, (amount + TOKEN_STARS_RATE - 1) // TOKEN_STARS_RATE)
        result = telegram_request(token, "sendInvoice", {
            "chat_id": chat_id,
            "title": f"\U0001F916 {amount} токенов",
            "description": f"Пополнение токенов ZeroxAI — {amount} токенов",
            "payload": f"tokens_{user_id}_{amount}",
            "provider_token": "",
            "currency": "XTR",
            "prices": [{"label": f"\U0001F916 {amount} токенов", "amount": stars}],
        })
        if not result.get("ok"):
            reply_message(token, chat_id, f"\u274C Ошибка: {result.get('description', 'неизвестно')}", message.get("message_id"), reply_markup=km)
        return

    # Build the AI request once. Project mode receives a stronger coding prompt.
    ai_messages = build_messages(
        chat_id,
        text,
        user.get("username"),
        user.get("first_name"),
        user_id,
        force_project=force_project,
    )
    project_mode = messages_are_project(ai_messages)
    project_kind = detect_project_kind(
        text,
        USER_HISTORIES.get(chat_id, [])[-MAX_HISTORY_MESSAGES:],
        force_project=force_project,
    ) or "generic"

    # estimate input token count (rough: 1 token ~ 4 chars)
    est_input = len(text) // 4
    est_output_limit = 1800 if project_mode else 500
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
        png_bytes = _make_thinking_png()
        r = telegram_upload(token, "sendSticker", {"chat_id": chat_id}, "sticker", png_bytes, "sticker.webp", "image/webp")
        if r.get("ok"):
            think_msg_id = r["result"]["message_id"]
    except Exception as e:
        print(f"Sticker error: {e}", flush=True)
        r = telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": ". . ."})
        if r.get("ok"):
            think_msg_id = r["result"]["message_id"]

    try:
        answer = call_ai(ai_messages, user_id)
        if project_mode:
            answer = repair_project_answer(ai_messages, answer, project_kind, user_id)
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
        blocks = parse_code_blocks(answer) if has_code_blocks(answer) else []
        project_name = extract_project_name(answer, text) if project_mode else "project"
        display_answer = project_display_text(answer, blocks, project_name) if project_mode and blocks else answer
        if project_mode and blocks:
            remaining_issues = validate_project_blocks(project_kind, blocks)
            if remaining_issues:
                display_answer += "\n\n⚠️ Проверка проекта:\n" + "\n".join(f"• {issue}" for issue in remaining_issues[:8])

        answer_msg_id = send_ai_formatted_response(
            token,
            chat_id,
            display_answer,
            message.get("message_id"),
        )

        if project_mode and blocks:
            archive_name = f"{_project_slug(project_name)}.zip"
            send_document(
                token,
                chat_id,
                create_project_zip(blocks, project_name, text),
                archive_name,
            )
        elif project_mode and not blocks:
            reply_message(
                token,
                chat_id,
                "⚠️ Модель не вернула файлы в формате архива. Попробуйте повторить запрос через /project и точнее указать платформу/версию.",
                answer_msg_id,
            )

        remember(chat_id, text, answer)

        # log and forward conversation to owner
        username = user.get("username") or user.get("first_name") or str(user_id)
        log_conversation(user_id, chat_id, username, text, answer)
        forward_to_owner(token, user_id, username, text, answer, chat_id)

        if answer_msg_id and blocks and not project_mode:
            prompt_result = send_code_prompt(token, chat_id, answer_msg_id)
            prompt_msg_id = prompt_result.get("result", {}).get("message_id")
            if prompt_msg_id:
                CODE_STORE[(chat_id, prompt_msg_id)] = {
                    "blocks": blocks,
                    "project_name": extract_project_name(answer, text),
                    "source_request": text,
                }
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
        telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": "\u200B", "reply_markup": _menu_kb(is_pm=not is_group)})
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
                "/index.html": ("index.html", "text/html"),
                "/src/app.js": ("src/app.js", "application/javascript"),
                "/src/styles.css": ("src/styles.css", "text/css"),
                "/assets/logo.svg": ("assets/logo.svg", "image/svg+xml"),
                "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
                "/service-worker.js": ("service-worker.js", "application/javascript"),
            }
            if path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(b"ok")
            elif path in static_files:
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
                        answer = call_ai(messages, int(user_id)) if user_id else call_openrouter(messages)
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

    # PID file lock — prevent multiple instances
    PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_pid.txt")
    try:
        if os.path.exists(PID_FILE):
            with open(PID_FILE) as f:
                old_pid_str = f.read().strip()
            if old_pid_str:
                try:
                    old_pid = int(old_pid_str)
                    if sys.platform == "win32":
                        r = subprocess.run(["tasklist", "/FI", f"PID eq {old_pid}"], capture_output=True, text=True, timeout=5)
                        if old_pid_str in r.stdout:
                            print(f"Bot already running with PID {old_pid}. Exiting.", flush=True)
                            sys.exit(0)
                    else:
                        os.kill(old_pid, 0)
                        print(f"Bot already running with PID {old_pid}. Exiting.", flush=True)
                        sys.exit(0)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except Exception as e:
        print(f"Warning: could not write PID file: {e}", file=sys.stderr)

    def signal_handler(sig, frame):
        print("Termination signal received, saving data...", flush=True)
        save_data()
        save_histories_to_db()
        sys.exit(0)

    try:
        signal.signal(signal.SIGTERM, signal_handler)
    except ValueError:
        pass  # not in main thread (e.g. started via app.py)

    init_db()
    load_data()

    token = get_env("TELEGRAM_BOT_TOKEN")
    global _BOT_TOKEN
    _BOT_TOKEN = token
    for admin_username in ADMIN_TICKET_TARGETS:
        resolved = resolve_username(token, admin_username)
        if resolved:
            _super_admin_ids.add(resolved)
    migrate_legacy_user_keys(token)

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
                        {"command": "project", "description": "Создать ZIP-проект"},
                        {"command": "vpn", "description": "🌍 VPN — безопасное подключение"},
                        {"command": "roles", "description": "Состав по ролям"},
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


    try:
        if _local_bot_alive():
            print("Local bot may be alive elsewhere. Waiting 30s...", flush=True)
            time.sleep(30)
    except Exception:
        pass

    try:
        if set_webhook(token):
            port = int(os.getenv("WEBHOOK_PORT", os.getenv("PORT", "8080")))
            try:
                server = ThreadingHTTPServer(("0.0.0.0", port), webhook_handler_factory(token))
            except OSError:
                print(f"Port {port} in use, falling back to polling mode", flush=True)
                _run_polling_bot(token)
                return
            print(f"Webhook server listening on port {port}", flush=True)
            server.serve_forever()
        else:
            _run_polling_bot(token)
    except BaseException as e:
        print(f"Bot crashed: {e}, restarting...", file=sys.stderr)
        import traceback
        traceback.print_exc()


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


# Run only via app.py (Railway deployment).
# Direct python telegram_bot.py is disabled to prevent running without a database.