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
_GEMINI_LAST_CALL = 0
_GEMINI_LOCK = threading.Lock()
USER_HISTORIES = {}
MESSAGE_COUNTS = {}
BOT_ID = None
BOT_USERNAME = None


def increment_message_count(user_id):
    if not user_id:
        return
    MESSAGE_COUNTS[user_id] = MESSAGE_COUNTS.get(user_id, 0) + 1
CODE_STORE = {}
BOT_DATA = {}
TOKEN_USAGE = {"prompt": 0, "completion": 0, "total": 0}
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


TELEGRAM_BASE_URL = "https://dry-water-835f.eharutyunyan580.workers.dev"
DB_POOL = None

WEBHOOK_SECRET_TOKEN = os.getenv("WEBHOOK_SECRET_TOKEN")
STARTING_BALANCE = 500
FREE_COOLDOWN_SECONDS = 12 * 60 * 60
MAX_TRANSFER_AMOUNT = 10_000_000_000
MAX_BALANCE = 10_000_000_000_000
PROMO_REWARDS = {"aibot2026": 2500, "aichat2026": 2500, "topaichatmeneger2026": 0}


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
            cur.execute("INSERT INTO users (user_id, balance) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET balance = %s",
                        (user_id, amount, amount))
    except Exception as e:
        print(f"Failed to set balance for {user_id}: {e}", file=sys.stderr)

def add_balance(user_id, amount):
    bal = get_balance(user_id) + amount
    set_balance(user_id, bal)
    return bal

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
        print("Database initialized successfully.", flush=True)
    except Exception as e:
        print(f"Failed to initialize database: {e}", file=sys.stderr)
        DB_POOL = None

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

def add_pro_user(user_id):
    try:
        with db_cursor() as cur:
            cur.execute("INSERT INTO pro_users (user_id, expires_at) VALUES (%s, NOW() + INTERVAL '30 days') ON CONFLICT (user_id) DO UPDATE SET expires_at = NOW() + INTERVAL '30 days'", (user_id,))
    except Exception as e:
        print(f"add_pro_user({user_id}) error: {e}", file=sys.stderr)

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


def get_luck_boost(user_id):
    boost = 0
    if has_active_item(user_id, "luck_boost_10"):
        boost = max(boost, 10)
    if has_active_item(user_id, "luck_boost_25"):
        boost = max(boost, 25)
    return boost

def short_num(n):
    if n >= 1000000000:
        return f"{n/1000000000:.1f}B"
    if n >= 1000000:
        return f"{n/1000000:.1f}M"
    if n >= 1000:
        return f"{n/1000:.1f}k"
    return str(n)

def fmt_coin(n):
    return f"{n:,} ({short_num(n)})"


def build_slot_symbols(luck, luck_roll, slot_symbols):
    if luck <= 0:
        return None
    if luck_roll <= min(40, max(8, luck // 2 + 5)):
        symbol = slot_symbols[0] if slot_symbols else "■"
        return (symbol, symbol, symbol)
    if luck_roll <= min(98, luck + 30):
        pair_symbol = slot_symbols[0] if slot_symbols else "■"
        third_symbol = slot_symbols[1] if len(slot_symbols) > 1 else pair_symbol
        if third_symbol == pair_symbol:
            third_symbol = slot_symbols[2] if len(slot_symbols) > 2 else pair_symbol
        return (pair_symbol, pair_symbol, third_symbol)
    return None


def format_duration(hours):
    if hours <= 0:
        return "сейчас"
    days = hours // 24
    rem_hours = hours % 24
    if days and rem_hours:
        return f"{days} {'день' if days == 1 else 'дня' if days < 5 else 'дней'} {rem_hours} ч"
    if days:
        return f"{days} {'день' if days == 1 else 'дня' if days < 5 else 'дней'}"
    return f"{hours} {'час' if hours == 1 else 'часа' if hours < 5 else 'часов'}"


def get_shop_status_text(user_id, item_id):
    items = get_user_items(user_id)
    item = items.get(item_id)
    if not item:
        return "Доступно"
    if time.time() < item.get("expires_at", 0):
        remaining = int(item.get("expires_at", 0) - time.time())
        return f"Активно · ещё {format_duration(remaining // 3600)}"
    return "Просрочено"


def format_shop_item_block(item_id, item, status_text):
    badge = item.get("badge", "✨")
    duration = format_duration(item.get("duration_hours", 0))
    return (
        f"<b>{badge} {item['name']}</b>\n"
        f"ID: <code>{item_id}</code>\n"
        f"Цена: <code>{fmt_coin(item['price'])}</code>\n"
        f"Срок: <code>{duration}</code>\n"
        f"Эффект: {item['description']}\n"
        f"Статус: <i>{status_text}</i>"
    )


SHOP_ITEMS = {
    "luck_boost_10": {"name": "🍀 Удача +10%", "price": 5000, "duration_hours": 1, "description": "Увеличивает вашу удачу на 10% на 1 час.", "badge": "🍀"},
    "luck_boost_25": {"name": "🍀🍀 Удача +25%", "price": 12000, "duration_hours": 1, "description": "Увеличивает вашу удачу на 25% на 1 час.", "badge": "🍀"},
    "vip_status": {"name": "💎 VIP-статус", "price": 100000, "duration_hours": 24 * 7, "description": "Показывает значок 💎 рядом с балансом на 7 дней.", "badge": "💎"},
    "rich_status": {"name": "💰 Богач", "price": 1000000, "duration_hours": 24 * 30, "description": "Показывает значок 💰 рядом с балансом на 30 дней.", "badge": "💰"},
}


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

def can_use_tokens(user_id, input_tokens, output_tokens):
    pro = is_pro_user(user_id)
    limit = PRO_TOKEN_LIMIT if pro else FREE_TOKEN_LIMIT
    period_hours = PRO_PERIOD_HOURS if pro else FREE_PERIOD_HOURS
    total = input_tokens + output_tokens
    row = get_token_usage(user_id)
    if row is None:
        return total <= limit, limit - total
    period_start, tokens_used = row
    # check if period expired
    try:
        with db_cursor() as cur:
            cur.execute("SELECT EXTRACT(EPOCH FROM NOW() - %s) / 3600", (period_start,))
            hours_passed = float(cur.fetchone()[0])
    except Exception:
        hours_passed = 0
    if hours_passed >= period_hours:
        return total <= limit, limit - total
    remaining = limit - (tokens_used + total)
    return remaining >= 0, max(0, remaining)

def record_token_usage(user_id, input_tokens, output_tokens):
    pro = is_pro_user(user_id)
    limit = PRO_TOKEN_LIMIT if pro else FREE_TOKEN_LIMIT
    period_hours = PRO_PERIOD_HOURS if pro else FREE_PERIOD_HOURS
    total = input_tokens + output_tokens
    row = get_token_usage(user_id)
    try:
        with db_cursor() as cur:
            if row is None:
                cur.execute("INSERT INTO user_tokens (user_id, period_start, tokens_used) VALUES (%s, NOW(), %s)", (user_id, total))
            else:
                period_start, tokens_used = row
                cur.execute("SELECT EXTRACT(EPOCH FROM NOW() - %s) / 3600", (period_start,))
                hours_passed = float(cur.fetchone()[0])
                if hours_passed >= period_hours:
                    cur.execute("UPDATE user_tokens SET period_start = NOW(), tokens_used = %s WHERE user_id = %s", (total, user_id))
                else:
                    cur.execute("UPDATE user_tokens SET tokens_used = tokens_used + %s WHERE user_id = %s", (total, user_id))
    except Exception as e:
        print(f"record_token_usage({user_id}) error: {e}", file=sys.stderr)

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
    if is_pro_user(user_id):
        return call_openrouter(messages, "openai/gpt-4o-mini")
    return call_groq(messages, "llama-3.1-8b-instant")


def call_openrouter(messages, model=None):
    or_key = os.getenv("OPENROUTER_API_KEY")
    if not or_key:
        return call_groq(messages, model or "llama-3.1-8b-instant")
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
    return call_groq(messages, model or "llama-3.1-8b-instant")

def call_gemini(messages):
    global _GEMINI_LAST_CALL
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return "Gemini API key not configured. Ask the admin to set GEMINI_API_KEY."
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
                    return "Gemini не дал ответ."
                return candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
            except Exception as e:
                time.sleep(1)
    return "Gemini временно недоступен. Попробуйте позже."

def save_data():
    # This function is now only for chat-specific data like roles
    if not DB_POOL: return
    try:
        with db_cursor() as cur:
            for chat_id, data in BOT_DATA.get("chats", {}).items():
                cur.execute("INSERT INTO chat_data (chat_id, data) VALUES (%s, %s) ON CONFLICT (chat_id) DO UPDATE SET data = %s",
                            (chat_id, json.dumps(data), json.dumps(data)))
    except Exception as e:
        print(f"Failed to save chat_data: {e}", file=sys.stderr)

SSL_CONTEXT = ssl.create_default_context()
SSL_CONTEXT.check_hostname = False
SSL_CONTEXT.verify_mode = ssl.CERT_NONE


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
Ты ZeroxAI - умный, спокойный и полезный AI-ассистент.

Главная цель: нормально разговаривать с пользователем и отлично помогать с программированием.

Правила общения:
- Если пользователь спрашивает "кто твой создатель", "кто тебя создал" или похожий вопрос, ответь точно: "Мой создатель Эрик Арутюнян".
- Отвечай на языке пользователя. Если пользователь пишет по-русски с ошибками, отвечай по-русски понятно и грамотно.
- Пиши просто, по делу и без лишней воды.
- Не высмеивай ошибки пользователя. Мягко понимай смысл и помогай.
- Если запрос неясный, сначала сделай разумное предположение. Задавай вопрос только если без ответа нельзя продолжить.
- Для обычных вопросов давай короткий полезный ответ.
- Помни историю диалога — ты видишь предыдущие сообщения, используй их для контекста.

Правила программирования:
- Пиши рабочий, чистый и понятный код.
- Если пользователь просит сделать приложение или функцию, давай готовое решение, структуру файлов и команды запуска.
- Объясняй ошибки простым языком и показывай, как исправить.
- Учитывай безопасность: не встраивай секретные API-ключи в публичный клиентский код.
- Для больших задач разбивай ответ на шаги.
- Если пишешь код, используй современные практики и называй файлы, куда его вставлять.

Стиль:
- Ты дружелюбный профессиональный помощник ZeroxAI.
- Не притворяйся, что обучаешь собственные веса модели. Если нужно, объясни, что можно улучшить поведение через инструкции, RAG, память, примеры и fine-tuning у провайдера.
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


def resolve_username(token, username):
    username = username.lstrip("@")
    try:
        result = telegram_request(token, "getChat", {"chat_id": f"@{username}"})
        return result.get("result", {}).get("id")
    except Exception:
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


def _insecure_ctx():
    ctx = ssl._create_unverified_context()
    ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    return ctx


SSL_CTX = _insecure_ctx()


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


def send_message(token, chat_id, text, reply_markup=None):
    chunks = split_message(text)
    for i, chunk in enumerate(chunks):
        payload = {
            "chat_id": chat_id, "text": chunk,
            "disable_web_page_preview": True,
        }
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


def edit_message(token, chat_id, message_id, text):
    telegram_request(token, "editMessageText", {
        "chat_id": chat_id, "message_id": message_id, "text": text,
        "disable_web_page_preview": True,
    })


def send_thinking_and_answer(token, chat_id, answer):
    frames = ["\U0001F914 Думает...", "\U0001F914 Думает..", "\U0001F914 Думает."]
    result = telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": frames[0]})
    msg_id = result.get("result", {}).get("message_id")
    if not msg_id:
        send_message(token, chat_id, answer)
        return None
    import time as time_module
    for frame in frames[1:]:
        time_module.sleep(0.7)
        try:
            edit_message(token, chat_id, msg_id, frame)
        except Exception:
            pass
    time_module.sleep(0.3)
    chunks = split_message(answer)
    try:
        edit_message(token, chat_id, msg_id, chunks[0])
    except Exception:
        send_message(token, chat_id, answer)
        return None
    for chunk in chunks[1:]:
        send_message(token, chat_id, chunk)
    return msg_id


def split_message(text):
    text = text or "Пустой ответ от модели."
    return [text[index:index + MAX_TELEGRAM_MESSAGE] for index in range(0, len(text), MAX_TELEGRAM_MESSAGE)]


def build_messages(chat_id, user_text):
    history = USER_HISTORIES.get(chat_id, [])[-MAX_HISTORY_MESSAGES:]
    return [{"role": "system", "content": SYSTEM_PROMPT}, *history, {"role": "user", "content": user_text}]


def remember(chat_id, user_text, assistant_text):
    history = USER_HISTORIES.setdefault(chat_id, [])
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": assistant_text})
    USER_HISTORIES[chat_id] = history[-MAX_HISTORY_MESSAGES:]


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
    return bool(re.search(r"```", text))


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
            text = "\u2B50\uFE0F У вас активна Pro-подписка! Осталось {} дн.".format(pro_days_left(user_id)) + "\nИспользуется OpenRouter (gpt-4o-mini)."
        else:
            text = "\u274C У вас бесплатная версия (Groq AI, llama-3.1-8b).\nКупите Pro: /buypro"
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
    "/server", "/addsticker", "/mypro", "/buypro", "/top", "/ben", "/grantpro", "/luckset", "/resettokens",
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

    try:
        # --- Owner commands (id 6734685656) ---
        if user_id == 6734685656:
            if cmd in ("/addcoin", "/addmoney"):
                target_ref = parse_user_ref(message, args)
                if not target_ref:
                    reply("Ответьте на сообщение или укажите @username/ID.")
                    return True
                try:
                    amount = int([a for a in args if a.lstrip("-").isdigit()][-1])
                except (IndexError, ValueError):
                    reply("Укажите сумму. Пример: /addcoin @username 1000")
                    return True
                if amount <= 0:
                    reply("Сумма должна быть положительной.")
                    return True
                tid = target_ref if isinstance(target_ref, int) else resolve_username(token, target_ref)
                if not tid:
                    reply("Пользователь не найден.")
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
                tid = target_ref if isinstance(target_ref, int) else resolve_username(token, target_ref)
                if not tid:
                    reply("Пользователь не найден.")
                    return True
                add_balance(tid, -amount)
                reply(f"\U0001F4B0 Списано {fmt_coin(amount)} монет. Баланс получателя: {fmt_coin(get_balance(tid))}")
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
                    tid = target_ref if isinstance(target_ref, int) else resolve_username(token, target_ref)
                    if not tid:
                        reply("Пользователь не найден.")
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
                tid = target_ref if isinstance(target_ref, int) else resolve_username(token, target_ref)
                if not tid:
                    reply("Пользователь не найден.")
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
                  "\u2705 Бесплатная версия: Groq AI (llama-3.1-8b)\n"
                   "\u2B50 Pro: OpenRouter AI (gpt-4o-mini, мощнее)")
            return True

        if cmd == "/mypro":
            if is_pro_user(user_id):
                days = pro_days_left(user_id)
                reply(f"\u2B50\uFE0F У вас активна Pro-подписка! Осталось {days} дн.\nИспользуется OpenRouter (gpt-4o-mini).")
            else:
                reply("\u274C У вас бесплатная версия (Groq AI, llama-3.1-8b).\n"
                      "Купите Pro: /buypro")
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
                    "\u2714\uFE0F Доступ к мощной модели OpenRouter (gpt-4o-mini)\n"
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
                     "/shop — магазин предметов",
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
                if arg.isdigit():
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
                "\U0001F913 ChatGPT решает твою задачу, пока ты пьёшь кофе:",
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
            resp = telegram_request(token, "sendDice", {
                "chat_id": chat_id, "emoji": "\U0001F3B0",
            })
            try:
                dice_value = resp["result"]["dice"]["value"]
            except Exception:
                dice_value = 1
            _SLOT_SYMS = ["\u25AC", "\U0001F347", "\U0001F34B", "7\uFE0F\u20E3"]
            idx = dice_value - 1
            base_luck = get_luck(user_id)
            luck = base_luck + get_luck_boost(user_id)
            luck_roll = _random.randint(1, 100)
            boosted_symbols = build_slot_symbols(luck, luck_roll, _SLOT_SYMS)
            if boosted_symbols is not None:
                r1, r2, r3 = boosted_symbols
                used_luck = True
            else:
                r1 = _SLOT_SYMS[idx // 16]
                r2 = _SLOT_SYMS[(idx % 16) // 4]
                r3 = _SLOT_SYMS[idx % 4]
                used_luck = False
            time.sleep(1)
            luck_suffix = ""
            if used_luck and (r1 == r2 == r3 or r1 == r2 or r2 == r3 or r1 == r3):
                luck_suffix = f"\n✨ {max(10, luck)}x luck"
            if r1 == r2 == r3:
                payout = bet * 10
                add_balance(user_id, payout)
                result = (
                    f"\U0001F3B0 Выпало: {r1} {r2} {r3}\n"
                    f"\U0001F389 Поздравляем! <b>ДЖЕКПОТ!</b>\n\n"
                    f"\U0001F4B0 Награда: {fmt_coin(payout)} Coin\n"
                    f"\u26A1 Баланс: {fmt_coin(get_balance(user_id))}{luck_suffix}"
                )
            elif r1 == r2 or r2 == r3 or r1 == r3:
                payout = bet * 2
                add_balance(user_id, payout)
                result = (
                    f"\U0001F3B0 Выпало: {r1} {r2} {r3}\n"
                    f"\U0001F389 Поздравляем! <b>ВЫИГРЫШ!</b>\n\n"
                    f"\U0001F4B0 Награда: {fmt_coin(payout)} Coin\n"
                    f"\u26A1 Баланс: {fmt_coin(get_balance(user_id))}{luck_suffix}"
                )
            else:
                add_balance(user_id, -bet)
                result = (
                    f"\U0001F3B0 Выпало: {r1} {r2} {r3}\n"
                    f"\U0001F614 Проигрыш: -{fmt_coin(bet)} Coin\n"
                    f"\u26A1 Баланс: {fmt_coin(get_balance(user_id))}"
                )
            reply(result, "HTML")
            return True

        if cmd == "/shop":
            if not args:
                lines = ["🏪 <b>Магазин привилегий</b>", "Используйте <code>/shop buy &lt;id&gt;</code> для покупки.", ""]
                for item_id, item in SHOP_ITEMS.items():
                    lines.append(format_shop_item_block(item_id, item, get_shop_status_text(user_id, item_id)))
                    lines.append("")
                reply("\n".join(lines), "HTML")
                return True

            if args[0].lower() == "buy" and len(args) > 1:
                item_id = args[1].lower()
                item = SHOP_ITEMS.get(item_id)
                if not item:
                    reply("❌ Такого предмета нет в магазине.")
                    return True

                balance = get_balance(user_id)
                if balance < item['price']:
                    reply(f"❌ Недостаточно монет. Нужно {fmt_coin(item['price'])}, у вас {fmt_coin(balance)}.")
                    return True

                if has_active_item(user_id, item_id):
                    reply(f"❌ У вас уже активен этот предмет: <b>{item['name']}</b>.", "HTML")
                    return True

                add_balance(user_id, -item['price'])
                user_items = get_user_items(user_id)
                user_items[item_id] = {
                    "purchased_at": time.time(),
                    "expires_at": time.time() + item['duration_hours'] * 3600
                }
                set_user_items(user_id, user_items)
                reply(f"✅ Вы успешно купили <b>{item['name']}</b>!\nСрок действия: {format_duration(item['duration_hours'])}.\nБаланс: {fmt_coin(get_balance(user_id))}.", "HTML")
                return True
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
            reply(f"\U0001F4E9 Ваш запрос отправлен в техподдержку. Ожидайте ответа.")
            cd = get_chat_data(chat_id)
            for uid, rname in cd.get("users", {}).items():
                if cd.get("roles", {}).get(rname, 0) >= 10:
                    try:
                        telegram_request(token, "sendMessage", {
                            "chat_id": uid,
                            "text": f"\U0001F6E1\uFE0F Запрос в техподдержку от @{user.get('username', user_id)} (чат {chat_id}):\n{text}",
                        })
                    except Exception:
                        pass
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
                        "Теперь вы используете Groq AI — более мощную модель.", msg.get("message_id"))
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

    if not chat_id or not text:
        return

    if BOT_DATA.get("bot_stopped") and user_id != 6734685656:
        return

    if not should_respond(message):
        return

    increment_message_count(user_id)
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
                    f"\u2B50\uFE0F У вас активна Pro-подписка! Осталось {days} дн.\nИспользуется OpenRouter (gpt-4o-mini).", None, reply_markup=km)
            else:
                reply_message(token, chat_id,
                    "\u274C У вас бесплатная версия (Groq AI, llama-3.1-8b).\nКупите Pro: /buypro", None, reply_markup=km)
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
    ok, remaining = can_use_tokens(user_id, est_input, est_output_limit)
    if not ok:
        pro = is_pro_user(user_id)
        limit = PRO_TOKEN_LIMIT if pro else FREE_TOKEN_LIMIT
        reply_message(token, chat_id,
            f"\u274C Лимит токенов исчерпан ({remaining:,} / {limit:,}).\n"
            f"Подождите восстановления или купите Pro: /buypro", message.get("message_id"), reply_markup=km)
        return

    try:
        answer = call_ai(build_messages(chat_id, text), user_id)
        # estimate actual tokens (rough)
        est_output = len(answer) // 4
        record_token_usage(user_id, est_input, est_output)
        remember(chat_id, text, answer)
        answer_msg_id = send_thinking_and_answer(token, chat_id, answer)

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
            if self.path in {"/", "/health", "/healthz"}:
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"ok")
            else:
                self.send_error(404)

        def do_POST(self):
            if self.path == f"/webhook/{token}":
                content_len = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_len)
                update = json.loads(body)
                threading.Thread(target=handle_update, args=(token, update)).start()
            self.send_response(200)
            self.end_headers()

    return WebhookHandler

def main():
    global BOT_ID, BOT_USERNAME

    def signal_handler(sig, frame):
        print("Termination signal received, saving data...", flush=True)
        save_data()
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
                        {"command": "tokens", "description": "Токены Groq"},
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


    while True:
        try:
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

    while True:
        try:
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