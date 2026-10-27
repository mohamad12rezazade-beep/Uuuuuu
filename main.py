import os
import re
import random
import logging
import asyncio
from decimal import Decimal, InvalidOperation
from contextlib import closing, contextmanager
from html import escape as html_escape
import psycopg2
import psycopg2.extras
import psycopg2.pool

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, ReplyKeyboardRemove,
)
from telegram.constants import ChatType
from telegram.error import TimedOut, NetworkError
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = 8935601841
OWNER_IDS = {OWNER_ID}

CHANNEL_USERNAME = "@BET_1XZX"
CHANNEL_URL = "https://t.me/BET_1XZX"
GROUP_USERNAME = "@GAP_BAZIN1"
GROUP_URL = "https://t.me/GAP_BAZIN1"
GAME_CHAT_USERNAME = "GAP_BAZIN1"

# ⚠️ لینک دیتابیس Supabase (رمز جدیدت رو اینجا بذار)
DATABASE_URL = "postgresql://postgres.pqvuttwpkmhjhnccflxi:Ali1365277356@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"

CURRENCY = "TRX"
MIN_GAME_BET = Decimal("0.1")
MAX_GAME_BET = None
MAX_GAME_COUNT = 3
DEFAULT_REFERRAL_REWARD = Decimal("0.05")
MIN_WITHDRAWAL = Decimal("2")
COMMISSION_RATE = Decimal("0.10")
WIN_MULTIPLIER = Decimal("1.8")
GAME_TIMEOUT_MINUTES = 30

PENDING_PROOF: dict = {}
PENDING_CAPTCHA: dict = {}
DOT_CHARS = [".", "۔", "•", "·", "⋅", "٫", "٬", "。", "｡", "…"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("BET_BT")

# ============================================================
# DATABASE (POSTGRESQL WITH POOLING)
# ============================================================

db_pool = None

def init_db_pool():
    global db_pool
    if db_pool is None:
        try:
            db_pool = psycopg2.pool.SimpleConnectionPool(1, 15, DATABASE_URL, sslmode='require')
            logger.info("Database pool created.")
        except Exception as e:
            logger.error(f"Error creating DB pool: {e}")

@contextmanager
def get_db():
    global db_pool
    if db_pool is None: init_db_pool()
    conn = db_pool.getconn()
    try:
        yield conn
    finally:
        db_pool.putconn(conn)

def init_db():
    with get_db() as db:
        with db.cursor() as cursor:
            cursor.execute("CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, username TEXT DEFAULT '', first_name TEXT DEFAULT '', balance TEXT NOT NULL DEFAULT '0', referrer_id BIGINT DEFAULT NULL, captcha_verified INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
            cursor.execute("CREATE TABLE IF NOT EXISTS transactions (id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL, type TEXT NOT NULL, amount TEXT NOT NULL, description TEXT DEFAULT '', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
            cursor.execute("CREATE TABLE IF NOT EXISTS referrals (id SERIAL PRIMARY KEY, referrer_id BIGINT NOT NULL, referred_id BIGINT NOT NULL UNIQUE, reward TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
            cursor.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            cursor.execute("CREATE TABLE IF NOT EXISTS games (id SERIAL PRIMARY KEY, chat_id BIGINT NOT NULL, creator_id BIGINT NOT NULL, opponent_id BIGINT, game_type TEXT NOT NULL, count INTEGER NOT NULL, bet TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'waiting', creator_total INTEGER DEFAULT 0, opponent_total INTEGER DEFAULT 0, creator_rolls INTEGER DEFAULT 0, opponent_rolls INTEGER DEFAULT 0, message_id BIGINT DEFAULT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, finished_at TIMESTAMP DEFAULT NULL)")
            cursor.execute("CREATE TABLE IF NOT EXISTS withdrawals (id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL, amount TEXT NOT NULL, wallet TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, processed_at TIMESTAMP DEFAULT NULL, processed_by BIGINT DEFAULT NULL)")
            cursor.execute("CREATE TABLE IF NOT EXISTS banned_users (user_id BIGINT PRIMARY KEY, reason TEXT DEFAULT '', banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, banned_by BIGINT DEFAULT NULL)")
            
            cursor.execute("INSERT INTO settings(key, value) VALUES ('bot_enabled', '1') ON CONFLICT (key) DO NOTHING")
            cursor.execute("INSERT INTO settings(key, value) VALUES ('referral_reward', %s) ON CONFLICT (key) DO NOTHING", (str(DEFAULT_REFERRAL_REWARD),))
            db.commit()

# ============================================================
# HELPERS
# ============================================================

def normalize_digits(text):
    if text is None: return ""
    table = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    return str(text).translate(table)

def parse_amount(text):
    text = normalize_digits(str(text)).strip().replace("٫", ".").replace(",", ".")
    try: value = Decimal(text)
    except (InvalidOperation, ValueError): return None
    if value <= 0: return None
    return value

def money(value):
    value = Decimal(str(value))
    if value == value.to_integral(): return str(int(value))
    return f"{value:.8f}".rstrip("0").rstrip(".")

def safe_name(name):
    return html_escape(str(name or ""))

def is_owner(user_id):
    try: return int(user_id) in OWNER_IDS
    except: return False

def is_pv_chat_id(chat_id):
    try: return int(chat_id) > 0
    except: return False

def is_valid_trx_address(addr):
    if not addr: return False
    return bool(re.match(r"^T[A-Za-z0-9]{33}$", str(addr).strip()))

def is_banned(user_id):
    with get_db() as db:
        with db.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
            cursor.execute("SELECT user_id FROM banned_users WHERE user_id = %s", (user_id,))
            return bool(cursor.fetchone())

def has_invalid_name(user):
    name = (user.first_name or "").strip()
    if not name: return True
    for d in DOT_CHARS:
        if d in name: return True
    return False

def get_ban_reason(user):
    name = (user.first_name or "").strip()
    if not name: return "بدون اسم"
    for d in DOT_CHARS:
        if d in name: return f"اسم شامل نقطه ({d})"
    return "نامعتبر"

def ban_user(user_id, reason="", banned_by=None, zero_balance=False):
    with get_db() as db:
        try:
            with db.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
                cursor.execute("SELECT balance FROM users WHERE user_id = %s", (user_id,))
                row = cursor.fetchone()
                old_balance = Decimal(row["balance"]) if row else Decimal("0")
                
                cursor.execute("INSERT INTO users(user_id, balance) VALUES (%s, '0') ON CONFLICT (user_id) DO NOTHING", (user_id,))
                cursor.execute("INSERT INTO banned_users(user_id, reason, banned_by) VALUES (%s, %s, %s) ON CONFLICT (user_id) DO UPDATE SET reason = EXCLUDED.reason", (user_id, reason, banned_by))
                
                if zero_balance and old_balance > 0:
                    cursor.execute("UPDATE users SET balance = '0' WHERE user_id = %s", (user_id,))
                    cursor.execute("INSERT INTO transactions(user_id, type, amount, description) VALUES (%s, 'ban_zero', %s, %s)", (user_id, str(-old_balance), f"Banned: {reason}"))
                db.commit()
                return old_balance
        except Exception:
            db.rollback()
            logger.exception("ban_user failed")
            return None

def unban_user(user_id):
    with get_db() as db:
        with db.cursor() as cursor:
            cursor.execute("DELETE FROM banned_users WHERE user_id = %s", (user_id,))
            db.commit()

def auto_ban_invalid_users():
    with get_db() as db:
        with db.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
            cursor.execute("SELECT user_id, first_name, username FROM users")
            rows = cursor.fetchall()

    count = 0
    for r in rows:
        uid = int(r["user_id"])
        if is_owner(uid) or is_banned(uid): continue
        name = (r["first_name"] or "").strip()
        reason = None
        if not name: reason = "بدون اسم"
        else:
            for d in DOT_CHARS:
                if d in name: reason = f"اسم شامل نقطه ({d})"; break
        if reason:
            ban_user(uid, reason=f"auto: {reason}", zero_balance=False)
            count += 1
    logger.info("Auto-ban: %s user(s) banned.", count)

def ensure_user(user):
    if not user: return
    with get_db() as db:
        with db.cursor() as cursor:
            cursor.execute("INSERT INTO users(user_id, username, first_name, balance) VALUES (%s, %s, %s, '0') ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username, first_name = EXCLUDED.first_name", (user.id, user.username or "", user.first_name or ""))
            db.commit()

def ensure_user_id(user_id, first_name="", username=""):
    with get_db() as db:
        with db.cursor() as cursor:
            cursor.execute("INSERT INTO users(user_id, username, first_name, balance) VALUES (%s, %s, %s, '0') ON CONFLICT (user_id) DO NOTHING", (user_id, username or "", first_name or ""))
            db.commit()

def get_balance(user_id):
    with get_db() as db:
        with db.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
            cursor.execute("SELECT balance FROM users WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
            return Decimal(row["balance"]) if row else Decimal("0")

def get_user_display_name(user_id):
    with get_db() as db:
        with db.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
            cursor.execute("SELECT first_name, username FROM users WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
            if row:
                if row["first_name"]: return row["first_name"]
                if row["username"]: return f"@{row['username']}"
            return f"کاربر {user_id}"

def user_tag(user_id):
    return f'<a href="tg://user?id={user_id}">{safe_name(get_user_display_name(user_id))}</a>'

def is_captcha_verified(user_id):
    with get_db() as db:
        with db.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
            cursor.execute("SELECT captcha_verified FROM users WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
            return bool(row and row["captcha_verified"] == 1)

def set_captcha_verified(user_id):
    with get_db() as db:
        with db.cursor() as cursor:
            cursor.execute("UPDATE users SET captcha_verified = 1 WHERE user_id = %s", (user_id,))
            db.commit()

def get_referral_reward():
    with get_db() as db:
        with db.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
            cursor.execute("SELECT value FROM settings WHERE key = 'referral_reward'")
            row = cursor.fetchone()
            if row:
                try: return Decimal(row["value"])
                except: pass
    return DEFAULT_REFERRAL_REWARD

def set_referral_reward(value):
    with get_db() as db:
        with db.cursor() as cursor:
            cursor.execute("INSERT INTO settings(key, value) VALUES ('referral_reward', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (str(value),))
            db.commit()

def change_balance(user_id, amount, transaction_type, description=""):
    amount = Decimal(str(amount))
    with get_db() as db:
        try:
            with db.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
                cursor.execute("SELECT balance FROM users WHERE user_id = %s FOR UPDATE", (user_id,))
                row = cursor.fetchone()
                if not row: raise ValueError("user_not_found")
                current = Decimal(row["balance"])
                new_balance = current + amount
                if new_balance < 0: raise ValueError("insufficient_balance")
                cursor.execute("UPDATE users SET balance = %s WHERE user_id = %s", (str(new_balance), user_id))
                cursor.execute("INSERT INTO transactions(user_id, type, amount, description) VALUES (%s, %s, %s, %s)", (user_id, transaction_type, str(amount), description))
                db.commit()
                return new_balance
        except Exception:
            db.rollback()
            raise

def bot_enabled():
    with get_db() as db:
        with db.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
            cursor.execute("SELECT value FROM settings WHERE key = 'bot_enabled'")
            row = cursor.fetchone()
            return bool(row and row["value"] == "1")

def set_bot_enabled(enabled):
    with get_db() as db:
        with db.cursor() as cursor:
            cursor.execute("INSERT INTO settings(key, value) VALUES ('bot_enabled', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", ("1" if enabled else "0",))
            db.commit()

# ============================================================
# SAFE TELEGRAM & MEMBERSHIP
# ============================================================

async def safe_send_message(bot, chat_id, text, **kwargs):
    rm = kwargs.get("reply_markup")
    if isinstance(rm, ReplyKeyboardMarkup) and not is_pv_chat_id(chat_id): kwargs.pop("reply_markup", None)
    for attempt in range(6):
        try: return await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        except (TimedOut, NetworkError):
            if attempt < 5: await asyncio.sleep(0.35)
        except: return None
    return None

async def safe_delete_message(bot, chat_id, message_id):
    try: await bot.delete_message(chat_id=chat_id, message_id=message_id); return True
    except: return False

async def safe_send_dice(bot, chat_id, emoji):
    for attempt in range(6):
        try: return await bot.send_dice(chat_id=chat_id, emoji=emoji)
        except (TimedOut, NetworkError):
            if attempt < 5: await asyncio.sleep(0.35)
        except: return None
    return None

async def check_membership(user_id, context):
    allowed = {"member", "administrator", "creator"}
    try:
        c = await context.bot.get_chat_member(CHANNEL_USERNAME, user_id)
        g = await context.bot.get_chat_member(GROUP_USERNAME, user_id)
        return c.status in allowed and g.status in allowed
    except: return False

def join_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 عضویت در کانال BET_1XZX", url=CHANNEL_URL)],
        [InlineKeyboardButton("💬 عضویت در گپ GAP_BAZIN1", url=GROUP_URL)],
        [InlineKeyboardButton("✅ بررسی عضویت", callback_data="check_membership")],
    ])

async def send_join_required(update, context):
    text = f"🔒 <b>برای استفاده از ربات، ابتدا عضو شوید</b>\n\n📢 کانال: {CHANNEL_USERNAME}\n💬 گپ: {GROUP_USERNAME}\n\nپس از عضویت، روی دکمه «✅ بررسی عضویت» بزنید."
    keyboard = join_keyboard()
    if update.message: await safe_send_message(context.bot, update.effective_chat.id, text, parse_mode="HTML", reply_markup=keyboard)
    elif update.callback_query:
        try: await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
        except: await context.bot.send_message(chat_id=update.callback_query.message.chat_id, text=text, parse_mode="HTML", reply_markup=keyboard)

async def send_captcha(update, context):
    user = update.effective_user
    if not user: return
    chat = update.callback_query.message.chat if update.callback_query else update.effective_chat
    if chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP): return

    a, b = random.randint(1, 9), random.randint(1, 9)
    op = random.choice(["+", "-"])
    answer = a + b if op == "+" else (a - b if a >= b else b - a)
    options = {answer}
    while len(options) < 4:
        wrong = answer + random.randint(-5, 5)
        if wrong != answer and wrong >= 0: options.add(wrong)
    options = list(options)
    random.shuffle(options)
    PENDING_CAPTCHA[user.id] = {"answer": answer, "options": options}

    buttons, row = [], []
    for idx, opt in enumerate(options):
        row.append(InlineKeyboardButton(str(opt), callback_data=f"cap_{user.id}_{idx}"))
        if len(row) == 2: buttons.append(row); row = []
    if row: buttons.append(row)

    text = f"🔐 <b>تأیید امنیتی</b>\n\nبرای استفاده از ربات، پاسخ صحیح را انتخاب کن:\n\n❓ <b>{a} {op} {b} = ?</b>"
    keyboard = InlineKeyboardMarkup(buttons)
    if update.callback_query:
        try: await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard); return
        except: pass
        await safe_send_message(context.bot, update.callback_query.message.chat_id, text, parse_mode="HTML", reply_markup=keyboard)
    else: await safe_send_message(context.bot, update.effective_chat.id, text, parse_mode="HTML", reply_markup=keyboard)

async def require_captcha_and_membership(update, context):
    user = update.effective_user
    if not user or is_owner(user.id): return True
    chat = update.callback_query.message.chat if update.callback_query else update.effective_chat
    if is_banned(user.id):
        if chat and chat.type == ChatType.PRIVATE: await safe_send_message(context.bot, user.id, "🚫 <b>شما از ربات بن شده‌اید.</b>", parse_mode="HTML")
        return False
    if has_invalid_name(user) and not is_banned(user.id):
        reason = get_ban_reason(user)
        ban_user(user.id, reason=reason, banned_by=None, zero_balance=False)
        if chat and chat.type == ChatType.PRIVATE: await safe_send_message(context.bot, user.id, f"🚫 <b>شما بن شدید!</b>\n\n📌 دلیل: <b>{reason}</b>", parse_mode="HTML")
        return False
    if chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP): return True
    if not is_captcha_verified(user.id): await send_captcha(update, context); return False
    if not await check_membership(user.id, context): await send_join_required(update, context); return False
    return True

async def membership_callback(update, context):
    query = update.callback_query
    try: await query.answer()
    except: pass
    user = query.from_user
    if is_banned(user.id) or has_invalid_name(user):
        if not is_banned(user.id): ban_user(user.id, reason=get_ban_reason(user), banned_by=None, zero_balance=False)
        try: await query.answer("🚫 بن شدید یا اسم غیرمجاز دارید.", show_alert=True)
        except: pass
        return
    if await check_membership(user.id, context):
        if not is_owner(user.id) and not is_captcha_verified(user.id): await send_captcha(update, context); return
        try: await query.edit_message_text("✅ <b>عضویت شما تأیید شد.</b>", parse_mode="HTML")
        except: pass
        if not is_owner(user.id): await try_grant_referral(user.id, context)
        await send_start_menu(context, query.message.chat_id, user.id)
    else:
        try: await query.answer("❌ هنوز عضو نیستید!", show_alert=True)
        except: pass

async def captcha_callback(update, context):
    query = update.callback_query
    user = query.from_user
    try: _, uid, idx = query.data.split("_"); target_uid = int(uid); idx = int(idx)
    except: return
    if target_uid != user.id: return
    data = PENDING_CAPTCHA.get(user.id)
    if not data or not (0 <= idx < len(data["options"])): return
    if data["options"][idx] != data["answer"]:
        try: await query.answer("❌ پاسخ اشتباه!", show_alert=True)
        except: pass
        return
    PENDING_CAPTCHA.pop(user.id, None)
    set_captcha_verified(user.id)
    try: await query.answer("✅ تأیید شد")
    except: pass
    if not is_owner(user.id) and not await check_membership(user.id, context):
        try: await query.edit_message_text("✅ تأیید امنیتی انجام شد.\n\n🔒 حالا باید عضو شوید.", parse_mode="HTML")
        except: pass
        await send_join_required(update, context)
        return
    try: await query.edit_message_text("✅ <b>تأیید امنیتی انجام شد.</b>", parse_mode="HTML")
    except: pass
    if not is_owner(user.id): await try_grant_referral(user.id, context)
    await send_start_menu(context, query.message.chat_id, user.id)

async def try_grant_referral(user_id, context):
    with get_db() as db:
        with db.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
            cursor.execute("SELECT referrer_id FROM users WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
            if not row or row["referrer_id"] is None: return
            referrer_id = int(row["referrer_id"])
            cursor.execute("SELECT id FROM referrals WHERE referred_id = %s", (user_id,))
            if cursor.fetchone(): return
    if not is_captcha_verified(user_id) or not await check_membership(user_id, context): return
    reward_amount = get_referral_reward()
    with get_db() as db:
        try:
            with db.cursor() as cursor:
                cursor.execute("INSERT INTO referrals(referrer_id, referred_id, reward) VALUES (%s, %s, %s) ON CONFLICT (referred_id) DO NOTHING", (referrer_id, user_id, str(reward_amount)))
                cursor.execute("UPDATE users SET balance = CAST(balance AS NUMERIC) + %s WHERE user_id = %s", (str(reward_amount), referrer_id))
                cursor.execute("INSERT INTO transactions(user_id, type, amount, description) VALUES (%s, 'referral', %s, %s)", (referrer_id, str(reward_amount), f"Referral {user_id}"))
                db.commit()
        except Exception:
            db.rollback(); return
    new_bal = get_balance(referrer_id)
    await safe_send_message(context.bot, referrer_id, f"🎁 <b>زیرمجموعه جدید!</b>\n\n💰 پاداش: <b>{money(reward_amount)} {CURRENCY}</b>\n💳 موجودی جدید: <b>{money(new_bal)} {CURRENCY}</b>", parse_mode="HTML", reply_markup=main_keyboard())

def is_game_chat(chat):
    return bool(chat.username and chat.username.lower() == GAME_CHAT_USERNAME.lower())

def main_keyboard():
    return ReplyKeyboardMarkup([["💰 موجودی", "👥 زیرمجموعه"], ["🏧 برداشت TRX", "🆔 آیدی من"], ["📚 راهنما"]], resize_keyboard=True, is_persistent=True)

def admin_reply_keyboard():
    status = "🟢 روشن" if bot_enabled() else "🔴 خاموش"
    return ReplyKeyboardMarkup([["📊 آمار ربات", status], ["➕ شارژ کاربر", "➖ کسر کاربر"], ["🏧 برداشت‌های در انتظار"], ["💰 موجودی کاربران"], ["🚫 بن کاربر", "✅ آنبن کاربر"], ["➕ افزایش پاداش", "➖ کاهش پاداش"], ["🏠 منوی اصلی"]], resize_keyboard=True, is_persistent=True)

async def send_start_menu(context, chat_id, user_id):
    text = f"🤖 BET_BT\n\n💰 موجودی: {money(get_balance(user_id))} {CURRENCY}\n🆔 آیدی شما: <code>{user_id}</code>\n\nاز دکمه‌های پایین صفحه استفاده کنید 👇\n\n🎮 بازی فقط در گپ:\n{GROUP_URL}\n\nمثال: <code>3 تاس 0.5</code>"
    kwargs = {"parse_mode": "HTML"}
    if is_pv_chat_id(chat_id): kwargs["reply_markup"] = main_keyboard()
    await safe_send_message(context.bot, chat_id, text, **kwargs)

async def start(update, context):
    user = update.effective_user
    if not user: return
    ensure_user(user)
    chat = update.effective_chat
    if not is_owner(user.id):
        if is_banned(user.id): return
        if has_invalid_name(user):
            ban_user(user.id, reason=get_ban_reason(user), banned_by=None, zero_balance=False)
            return
    if context.args and context.args[0].startswith("ref_"):
        try: referrer_id = int(context.args[0].replace("ref_", ""))
        except: referrer_id = None
        if referrer_id and referrer_id != user.id:
            with get_db() as db:
                with db.cursor() as cursor:
                    cursor.execute("UPDATE users SET referrer_id = %s WHERE user_id = %s AND referrer_id IS NULL", (referrer_id, user.id))
                    db.commit()
    if chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP): return
    if not is_owner(user.id) and not is_captcha_verified(user.id): await send_captcha(update, context); return
    if not is_owner(user.id):
        if not await check_membership(user.id, context): await send_join_required(update, context); return
        await try_grant_referral(user.id, context)
    await send_start_menu(context, update.effective_chat.id, user.id)

async def show_balance(update, context, target=None):
    user = update.effective_user
    ensure_user(user)
    text = f"💰 موجودی شما\n\n{money(get_balance(user.id))} {CURRENCY}"
    if target: await safe_send_message(context.bot, target, text)
    elif update.callback_query:
        try: await update.callback_query.edit_message_text(text)
        except: pass
    else:
        kwargs = {"reply_to_message_id": update.message.message_id}
        if update.message.chat.type != ChatType.PRIVATE: kwargs["reply_markup"] = ReplyKeyboardRemove()
        await update.message.reply_text(text, **kwargs)

async def show_my_id(update, context, target=None):
    user = update.effective_user
    ensure_user(user)
    text = f"🆔 آیدی عددی شما\n\n<code>{user.id}</code>\n\nمثال انتقال:\n<code>انتقال 0.5 {user.id}</code>"
    if target: await safe_send_message(context.bot, target, text, parse_mode="HTML")
    elif update.callback_query:
        try: await update.callback_query.edit_message_text(text, parse_mode="HTML")
        except: pass
    else:
        kwargs = {"reply_to_message_id": update.message.message_id, "parse_mode": "HTML"}
        if update.message.chat.type != ChatType.PRIVATE: kwargs["reply_markup"] = ReplyKeyboardRemove()
        await update.message.reply_text(text, **kwargs)

async def show_referral(update, context, target=None):
    user = update.effective_user
    ensure_user(user)
    try: username = (await context.bot.get_me()).username
    except: username = None
    link = f"https://t.me/{username}?start=ref_{user.id}" if username else "لینک در دسترس نیست."
    with get_db() as db:
        with db.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = %s", (user.id,))
            total = cursor.fetchone()[0]
    text = f"👥 زیرمجموعه\n\n🔗 لینک دعوت:\n{link}\n\n👤 تعداد: {total}\n🎁 پاداش هر نفر: {money(get_referral_reward())} {CURRENCY}"
    if target: await safe_send_message(context.bot, target, text)
    elif update.callback_query:
        try: await update.callback_query.edit_message_text(text)
        except: pass
    else:
        kwargs = {"reply_to_message_id": update.message.message_id}
        if update.message.chat.type != ChatType.PRIVATE: kwargs["reply_markup"] = ReplyKeyboardRemove()
        await update.message.reply_text(text, **kwargs)

async def show_help(update, context, target=None):
    text = f"📚 راهنما\n\n💰 موجودی — نمایش موجودی\n👥 زیرمجموعه — پاداش {money(get_referral_reward())} {CURRENCY}\n🏧 برداشت TRX — حداقل {money(MIN_WITHDRAWAL)} {CURRENCY}\n\n🔄 انتقال: <code>انتقال 0.5</code> یا <code>انتقال 0.5 123456789</code>\n\n🎮 بازی: <code>3 تاس 0.5</code>\nحداقل شرط: {money(MIN_GAME_BET)} {CURRENCY}"
    if target: await safe_send_message(context.bot, target, text, parse_mode="HTML")
    elif update.callback_query:
        try: await update.callback_query.edit_message_text(text, parse_mode="HTML")
        except: pass
    else:
        kwargs = {"reply_to_message_id": update.message.message_id, "parse_mode": "HTML"}
        if update.message.chat.type != ChatType.PRIVATE: kwargs["reply_markup"] = ReplyKeyboardRemove()
        await update.message.reply_text(text, **kwargs)

async def transfer(update, context, amount):
    user, message = update.effective_user, update.message
    if not message.reply_to_message:
        await message.reply_text("❌ روی پیام کاربر Reply کنید.\n<code>انتقال 0.5</code>", parse_mode="HTML"); return
    target = message.reply_to_message.from_user
    if not target or target.id == user.id or target.is_bot or is_banned(target.id):
        await message.reply_text("❌ انتقال نامعتبر."); return
    ensure_user(target)
    try:
        change_balance(user.id, -amount, "transfer_out", f"To {target.id}")
        change_balance(target.id, amount, "transfer_in", f"From {user.id}")
        await message.reply_text(f"✅ انتقال انجام شد.\nمبلغ: {money(amount)} {CURRENCY}\nموجودی شما: {money(get_balance(user.id))} {CURRENCY}")
    except ValueError: await message.reply_text("❌ موجودی شما کافی نیست.")

async def transfer_by_id(update, context, amount, target_id):
    user, message = update.effective_user, update.message
    if target_id == user.id or target_id <= 0 or is_banned(target_id):
        await message.reply_text("❌ انتقال نامعتبر."); return
    with get_db() as db:
        with db.cursor() as cursor:
            cursor.execute("SELECT user_id FROM users WHERE user_id = %s", (target_id,))
            if not cursor.fetchone(): await message.reply_text("❌ کاربر ربات را استارت نکرده."); return
    try:
        change_balance(user.id, -amount, "transfer_out", f"To {target_id}")
        change_balance(target_id, amount, "transfer_in", f"From {user.id}")
        await message.reply_text(f"✅ انتقال انجام شد.\nمبلغ: {money(amount)} {CURRENCY}\nگیرنده: {target_id}\nموجودی شما: {money(get_balance(user.id))} {CURRENCY}")
        await safe_send_message(context.bot, target_id, f"💸 انتقال دریافت کردید!\nمبلغ: {money(amount)} {CURRENCY}\nموجودی: {money(get_balance(target_id))} {CURRENCY}", reply_markup=main_keyboard())
    except ValueError: await message.reply_text("❌ موجودی شما کافی نیست.")

async def show_withdraw_info(update, context, target=None):
    user = update.effective_user
    ensure_user(user)
    balance = get_balance(user.id)
    if balance < MIN_WITHDRAWAL:
        text = f"🏧 برداشت TRX\n\n💰 موجودی شما: {money(balance)} {CURRENCY}\n📉 حداقل برداشت: {money(MIN_WITHDRAWAL)} {CURRENCY}\n\n❌ موجودی شما برای برداشت کافی نیست."
        chat_id = target or update.effective_chat.id
        await safe_send_message(context.bot, chat_id, text, reply_markup=main_keyboard()); return
    context.user_data["withdraw_step"] = "amount"
    context.user_data.pop("withdraw_amount", None)
    text = f"🏧 برداشت TRX\n\n💰 موجودی شما: {money(balance)} {CURRENCY}\n📉 حداقل برداشت: {money(MIN_WITHDRAWAL)} {CURRENCY}\n\n💵 لطفاً <b>مبلغ مورد نظر</b> را به TRX ارسال کنید.\nمثال: <code>5</code>\n\nبرای لغو: <code>لغو</code>"
    chat_id = target or update.effective_chat.id
    await safe_send_message(context.bot, chat_id, text, parse_mode="HTML", reply_markup=main_keyboard())

async def handle_withdraw_amount(update, context, text):
    message, user = update.message, update.effective_user
    amount = parse_amount(text)
    if amount is None:
        await message.reply_text("❌ مبلغ وارد شده صحیح نیست.\nمثال: <code>5</code>\n\nبرای لغو: <code>لغو</code>", parse_mode="HTML"); return
    if amount < MIN_WITHDRAWAL:
        await message.reply_text(f"❌ حداقل برداشت {money(MIN_WITHDRAWAL)} {CURRENCY} است.\n\nبرای لغو: <code>لغو</code>", parse_mode="HTML"); return
    balance = get_balance(user.id)
    if amount > balance:
        await message.reply_text(f"❌ موجودی شما کافی نیست.\n💰 موجودی: {money(balance)} {CURRENCY}\n\nبرای لغو: <code>لغو</code>", parse_mode="HTML"); return
    context.user_data["withdraw_step"] = "wallet"
    context.user_data["withdraw_amount"] = str(amount)
    await message.reply_text("📮 حالا <b>آدرس ولت TRX</b> خود را ارسال کنید.\n\n⚠️ آدرس باید با حرف <b>T</b> شروع شود و ۳۴ کاراکتر باشد.\nمثال:\n<code>TXYZ...abc</code>\n\nبرای لغو: <code>لغو</code>", parse_mode="HTML")

async def handle_withdraw_wallet(update, context, wallet):
    message, user = update.message, update.effective_user
    wallet = wallet.strip()
    if not is_valid_trx_address(wallet):
        await message.reply_text("❌ آدرس ولت TRX معتبر نیست.\nباید با <b>T</b> شروع شود و ۳۴ کاراکتر باشد.\n\nبرای لغو: <code>لغو</code>", parse_mode="HTML"); return
    try: amount = Decimal(context.user_data.get("withdraw_amount", "0"))
    except: amount = None
    if not amount or amount <= 0:
        context.user_data.pop("withdraw_step", None); context.user_data.pop("withdraw_amount", None)
        await message.reply_text("❌ خطا. دوباره تلاش کنید."); return
    context.user_data.pop("withdraw_step", None); context.user_data.pop("withdraw_amount", None)
    with get_db() as db:
        with db.cursor() as cursor:
            cursor.execute("SELECT id FROM withdrawals WHERE user_id = %s AND status = 'pending' LIMIT 1", (user.id,))
            if cursor.fetchone(): await message.reply_text("❌ شما یک درخواست برداشت در انتظار دارید.\nلطفاً تا بررسی آن صبر کنید.", reply_markup=main_keyboard()); return
    try: change_balance(user.id, -amount, "withdraw", f"Withdraw {wallet[:10]}...")
    except ValueError:
        await message.reply_text("❌ موجودی کافی نیست.", reply_markup=main_keyboard()); return
    except Exception:
        logger.exception("Withdraw deduction failed"); await message.reply_text("❌ خطا در برداشت.", reply_markup=main_keyboard()); return
    try:
        with get_db() as db:
            with db.cursor() as cursor:
                cursor.execute("INSERT INTO withdrawals(user_id, amount, wallet) VALUES (%s, %s, %s) RETURNING id", (user.id, str(amount), wallet))
                wid = cursor.fetchone()[0]; db.commit()
    except Exception:
        logger.exception("Withdraw insert failed")
        try: change_balance(user.id, amount, "withdraw_rollback", "Insert failed")
        except: logger.exception("Withdraw rollback failed")
        await message.reply_text("❌ برداشت ثبت نشد.", reply_markup=main_keyboard()); return
    short_wallet = wallet if len(wallet) <= 40 else wallet[:20] + "..." + wallet[-15:]
    for owner in OWNER_IDS:
        try:
            await safe_send_message(context.bot, owner, f"🏧 <b>درخواست برداشت جدید</b>\n\n🆔 شماره: <code>{wid}</code>\n👤 نام: {safe_name(user.first_name or '-')}\n🔗 یوزر: @{safe_name(user.username or '-')}\n🆔 آیدی: <code>{user.id}</code>\n💰 مبلغ: <b>{money(amount)} {CURRENCY}</b>\n📮 ولت:\n<code>{safe_name(short_wallet)}</code>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ تأیید", callback_data=f"wd_ok_{wid}"), InlineKeyboardButton("❌ رد", callback_data=f"wd_no_{wid}")]]))
        except: pass
    await message.reply_text(f"✅ درخواست برداشت ثبت شد.\n\n🆔 شماره: {wid}\n💰 مبلغ: {money(amount)} {CURRENCY}\n📮 ولت: <code>{safe_name(short_wallet)}</code>\n\n⏳ در انتظار تأیید مدیریت.", parse_mode="HTML", reply_markup=main_keyboard())

async def admin_panel(update, context):
    user = update.effective_user
    if not is_owner(user.id): await update.message.reply_text("❌ شما مالک ربات نیستید."); return
    status = "🟢 روشن" if bot_enabled() else "🔴 خاموش"
    await update.message.reply_text(f"👑 <b>پنل مدیریت BET_BT</b>\n\nوضعیت ربات: {status}\n🎁 پاداش زیرمجموعه: {money(get_referral_reward())} {CURRENCY}\n💱 واحد پول: {CURRENCY}\n\nاز دکمه‌های پایین صفحه استفاده کنید 👇", parse_mode="HTML", reply_markup=admin_reply_keyboard())

async def ask_admin_balance(update, context, operation):
    user = update.effective_user
    context.user_data["admin_operation"] = operation; context.user_data["admin_waiting"] = True
    title = "➕ شارژ موجودی" if operation == "charge" else "➖ کسر موجودی"
    kwargs = {"parse_mode": "HTML"}
    if update.message.chat.type != ChatType.PRIVATE: kwargs["reply_markup"] = ReplyKeyboardRemove()
    await update.message.reply_text(f"{title}\n\nفرمت ارسال:\n<code>آیدی_عددی مبلغ</code>\n\nمثال:\n<code>8935601841 100</code>\n\nبرای لغو: <code>لغو</code>", **kwargs)

async def admin_balance_by_id(update, context):
    user = update.effective_user
    if not user or not is_owner(user.id): return False
    operation = context.user_data.get("admin_operation"); waiting = context.user_data.get("admin_waiting")
    if not operation or not waiting: return False
    text = normalize_digits(update.message.text.strip())
    if text == "لغو":
        context.user_data.pop("admin_operation", None); context.user_data.pop("admin_waiting", None)
        kwargs = {"reply_markup": admin_reply_keyboard()}
        if update.message.chat.type != ChatType.PRIVATE: kwargs["reply_markup"] = ReplyKeyboardRemove()
        await update.message.reply_text("❌ عملیات لغو شد.", **kwargs); return True
    match = re.match(r"^(\d+)\s+([0-9]+(?:\.[0-9]+)?)$", text)
    if not match:
        await update.message.reply_text("❌ فرمت اشتباه.\n\nمثال:\n<code>8935601841 100</code>\n\nبرای لغو: <code>لغو</code>", parse_mode="HTML"); return True
    target_id = int(match.group(1)); amount = parse_amount(match.group(2))
    if amount is None: await update.message.reply_text("❌ مبلغ صحیح نیست."); return True
    if target_id <= 0: await update.message.reply_text("❌ آیدی کاربر صحیح نیست."); return True
    ensure_user_id(target_id)
    context.user_data.pop("admin_operation", None); context.user_data.pop("admin_waiting", None)
    try:
        if operation == "charge": new_balance = change_balance(target_id, amount, "admin_charge", f"Owner {user.id}"); title = "شارژ"
        else: new_balance = change_balance(target_id, -amount, "admin_remove", f"Owner {user.id}"); title = "کسر"
    except ValueError:
        kwargs = {"reply_markup": admin_reply_keyboard()}
        if update.message.chat.type != ChatType.PRIVATE: kwargs["reply_markup"] = ReplyKeyboardRemove()
        await update.message.reply_text("❌ موجودی کاربر برای کسر کافی نیست.", **kwargs); return True
    except Exception:
        logger.exception("Admin balance by ID failed"); await update.message.reply_text("❌ خطا در تغییر موجودی."); return True
    kwargs = {"reply_markup": admin_reply_keyboard()}
    if update.message.chat.type != ChatType.PRIVATE: kwargs["reply_markup"] = ReplyKeyboardRemove()
    await update.message.reply_text(f"✅ {title} انجام شد.\n\n🆔 آیدی: {target_id}\n💰 مبلغ: {money(amount)} {CURRENCY}\n💳 موجودی جدید: {money(new_balance)} {CURRENCY}", **kwargs); return True

async def admin_change_by_reply(update, context, operation, amount):
    user, message = update.effective_user, update.message
    if not is_owner(user.id): return
    if not message.reply_to_message: await message.reply_text("❌ باید روی پیام کاربر Reply کنید.", reply_markup=admin_reply_keyboard()); return
    target = message.reply_to_message.from_user
    if not target or target.is_bot: await message.reply_text("❌ نامعتبر."); return
    ensure_user(target)
    try:
        if operation == "charge": new_balance = change_balance(target.id, amount, "admin_charge", f"Owner {user.id}"); title = "شارژ"
        else: new_balance = change_balance(target.id, -amount, "admin_remove", f"Owner {user.id}"); title = "کسر"
    except ValueError:
        await message.reply_text("❌ موجودی کاربر برای کسر کافی نیست."); return
    except Exception:
        logger.exception("Admin reply balance failed"); await message.reply_text("❌ عملیات انجام نشد."); return
    kwargs = {}
    if message.chat.type != ChatType.PRIVATE: kwargs["reply_markup"] = ReplyKeyboardRemove()
    await message.reply_text(f"✅ {title} انجام شد.\n\n👤 کاربر: {safe_name(target.first_name or target.id)}\n🆔 آیدی: {target.id}\n💰 مبلغ: {money(amount)} {CURRENCY}\n💳 موجودی جدید: {money(new_balance)} {CURRENCY}", **kwargs)
    try: await safe_send_message(context.bot, target.id, f"💳 <b>موجودی شما تغییر کرد</b>\n\n{'➕' if operation == 'charge' else '➖'} مبلغ: <b>{money(amount)} {CURRENCY}</b>\n💰 موجودی جدید: <b>{money(new_balance)} {CURRENCY}</b>", parse_mode="HTML", reply_markup=main_keyboard())
    except: pass

async def send_admin_stats(update, context):
    user = update.effective_user
    if not is_owner(user.id): return
    with get_db() as db:
        with db.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
            cursor.execute("SELECT COUNT(*) AS c FROM users"); users = cursor.fetchone()["c"]
            cursor.execute("SELECT COALESCE(SUM(CAST(balance AS NUMERIC)), 0) AS total FROM users"); total = float(cursor.fetchone()["total"] or 0)
            cursor.execute("SELECT COUNT(*) AS c FROM referrals"); referrals = cursor.fetchone()["c"]
            cursor.execute("SELECT COUNT(*) AS c FROM games"); games = cursor.fetchone()["c"]
            cursor.execute("SELECT COUNT(*) AS c FROM withdrawals WHERE status = 'pending'"); pending_wd = cursor.fetchone()["c"]
            cursor.execute("SELECT COUNT(*) AS c FROM users WHERE captcha_verified = 1"); verified = cursor.fetchone()["c"]
            cursor.execute("SELECT COUNT(*) AS c FROM banned_users"); banned_count = cursor.fetchone()["c"]
    await update.message.reply_text(f"📊 <b>آمار BET_BT</b>\n\n👤 کاربران: {users}\n✅ تأیید‌شده (کپچا): {verified}\n🚫 بن‌شده‌ها: {banned_count}\n💰 مجموع موجودی: {total:.4f} {CURRENCY}\n👥 زیرمجموعه‌ها: {referrals}\n🎁 پاداش زیرمجموعه: {money(get_referral_reward())} {CURRENCY}\n🎮 بازی‌ها: {games}\n🏧 برداشت در انتظار: {pending_wd}\n\nوضعیت: {'🟢 روشن' if bot_enabled() else '🔴 خاموش'}", parse_mode="HTML", reply_markup=admin_reply_keyboard())

async def send_admin_withdrawals(update, context):
    user = update.effective_user
    if not is_owner(user.id): return
    with get_db() as db:
        with db.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
            cursor.execute("SELECT * FROM withdrawals WHERE status = 'pending' ORDER BY id ASC LIMIT 10")
            rows = cursor.fetchall()
    if not rows:
        await update.message.reply_text("🏧 برداشت‌های در انتظار\n\n✅ هیچ درخواست در انتظاری نیست.", reply_markup=admin_reply_keyboard()); return
    for wd in rows:
        await safe_send_message(context.bot, update.effective_chat.id, f"🏧 درخواست #{wd['id']}\n👤 کاربر: <code>{wd['user_id']}</code>\n💰 مبلغ: {money(Decimal(wd['amount']))} {CURRENCY}\n📮 ولت: <code>{safe_name(wd['wallet'])}</code>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ تأیید", callback_data=f"wd_ok_{wd['id']}"), InlineKeyboardButton("❌ رد", callback_data=f"wd_no_{wd['id']}")]]))
    await update.message.reply_text(f"🏧 {len(rows)} درخواست در انتظار — به بالا اسکرول کنید.", reply_markup=admin_reply_keyboard())

async def send_admin_balances(update, context):
    user = update.effective_user
    if not is_owner(user.id): return
    with get_db() as db:
        with db.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
            cursor.execute("SELECT user_id, username, first_name, balance FROM users ORDER BY CAST(balance AS NUMERIC) DESC, user_id ASC")
            rows = cursor.fetchall()
    if not rows: await update.message.reply_text("💰 موجودی کاربران\n\n❌ هنوز کاربری ثبت نشده.", reply_markup=admin_reply_keyboard()); return
    lines = [f"💰 <b>موجودی کاربران ({len(rows)})</b>\n"]
    for i, r in enumerate(rows, 1):
        name = f"@{r['username']}" if r["username"] else (r["first_name"] or str(r["user_id"]))
        lines.append(f"{i}_ {safe_name(name)} <b>{money(Decimal(r['balance']))}</b>")
    text = "\n".join(lines)
    if len(text) <= 3800: await update.message.reply_text(text, parse_mode="HTML", reply_markup=admin_reply_keyboard()); return
    chunk = ""
    chunks = []
    for line in lines:
        if len(chunk) + len(line) + 1 > 3500: chunks.append(chunk); chunk = line + "\n"
        else: chunk += line + "\n"
    if chunk: chunks.append(chunk)
    for i, ch in enumerate(chunks): await safe_send_message(context.bot, update.effective_chat.id, ch, parse_mode="HTML", reply_markup=admin_reply_keyboard() if i == len(chunks) - 1 else None)

async def ban_command(update, context):
    user, message = update.effective_user, update.message
    if not is_owner(user.id): await message.reply_text("❌ دسترسی ندارید."); return
    target_id = message.reply_to_message.from_user.id if message.reply_to_message and message.reply_to_message.from_user else None
    if not target_id and context.args:
        try: target_id = int(normalize_digits(context.args[0]))
        except: pass
    if not target_id: await message.reply_text("❌ استفاده:\n<code>/ban 123456789</code>\nیا روی پیام کاربر ریپلای کنید.", parse_mode="HTML"); return
    if target_id == user.id: await message.reply_text("❌ نمی‌توانید خودتان را بن کنید."); return
    if is_banned(target_id): await message.reply_text("ℹ️ این کاربر قبلاً بن شده است."); return
    old_balance = ban_user(target_id, reason="owner_ban", banned_by=user.id, zero_balance=True)
    if old_balance is None: await message.reply_text("❌ خطا در بن."); return
    await message.reply_text(f"🚫 <b>کاربر از ربات بن شد!</b>\n\n👤 {safe_name(get_user_display_name(target_id))}\n🆔 <code>{target_id}</code>\n💰 موجودی صفر شد: <b>{money(old_balance)} {CURRENCY}</b>\n\n✅ از ربات بن شد و موجودی صفر شد.", parse_mode="HTML")
    try: await safe_send_message(context.bot, target_id, f"🚫 <b>شما از ربات بن شدید!</b>\n\n💰 موجودی شما ({money(old_balance)} {CURRENCY}) صفر شد.\nدسترسی شما به ربات مسدود است.", parse_mode="HTML")
    except: pass

async def unban_command(update, context):
    user, message = update.effective_user, update.message
    if not is_owner(user.id): await message.reply_text("❌ دسترسی ندارید."); return
    target_id = message.reply_to_message.from_user.id if message.reply_to_message and message.reply_to_message.from_user else None
    if not target_id and context.args:
        try: target_id = int(normalize_digits(context.args[0]))
        except: pass
    if not target_id: await message.reply_text("❌ استفاده:\n<code>/unban 123456789</code>\nیا روی پیام کاربر ریپلای کنید.", parse_mode="HTML"); return
    if not is_banned(target_id): await message.reply_text("ℹ️ این کاربر بن نیست."); return
    unban_user(target_id)
    await message.reply_text(f"✅ کاربر <code>{target_id}</code> از بن خارج شد.\n\n⚠️ توجه: اگر هنوز اسم نداشته باشد، دوباره بن می‌شود.", parse_mode="HTML")
    try: await safe_send_message(context.bot, target_id, "✅ <b>شما از بن خارج شدید!</b>\n\nحالا می‌توانید از ربات استفاده کنید.\n⚠️ اگر اسم نداشته باشید، دوباره بن می‌شوید.", parse_mode="HTML", reply_markup=main_keyboard())
    except: pass

async def banned_list_command(update, context):
    user = update.effective_user
    if not is_owner(user.id): return
    with get_db() as db:
        with db.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
            cursor.execute("SELECT b.user_id, b.reason, b.banned_at, u.first_name, u.username FROM banned_users b LEFT JOIN users u ON u.user_id = b.user_id ORDER BY b.banned_at DESC LIMIT 50")
            rows = cursor.fetchall()
    if not rows: await update.message.reply_text("🚫 هیچ کاربری بن نیست."); return
    lines = [f"🚫 <b>لیست بن‌شده‌ها ({len(rows)})</b>\n"]
    for r in rows: lines.append(f"• <code>{r['user_id']}</code> — {safe_name(r['first_name'] or r['username'] or '-')} ({safe_name(r['reason'] or '-')})")
    text = "\n".join(lines)
    if len(text) <= 3800: await update.message.reply_text(text, parse_mode="HTML"); return
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 3500: await safe_send_message(context.bot, update.effective_chat.id, chunk, parse_mode="HTML"); chunk = line + "\n"
        else: chunk += line + "\n"
    if chunk: await safe_send_message(context.bot, update.effective_chat.id, chunk, parse_mode="HTML")

async def withdraw_callback(update, context):
    query, user = update.callback_query, update.callback_query.from_user
    if not is_owner(user.id):
        try: await query.answer("❌ دسترسی ندارید.", show_alert=True)
        except: pass
        return
    try: _, action, wd_id = query.data.split("_"); wd_id = int(wd_id)
    except: return
    with get_db() as db:
        try:
            with db.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
                cursor.execute("SELECT * FROM withdrawals WHERE id = %s FOR UPDATE", (wd_id,))
                wd = cursor.fetchone()
                if not wd or wd["status"] != "pending":
                    db.rollback(); await query.answer("❌ درخواست در دسترس نیست.", show_alert=True); return
                amount, wd_uid, wd_wallet = Decimal(wd["amount"]), int(wd["user_id"]), wd["wallet"]
                if action == "ok":
                    cursor.execute("UPDATE withdrawals SET status = 'approved', processed_at = CURRENT_TIMESTAMP, processed_by = %s WHERE id = %s", (user.id, wd_id))
                    db.commit()
                    try: await query.answer("✅ تأیید شد."); await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📤 ارسال مدرک پرداخت", callback_data=f"wd_proof_{wd_id}")]]))
                    except: pass
                    await safe_send_message(context.bot, wd_uid, f"✅ <b>درخواست برداشت شما تأیید شد.</b>\n\n🆔 شماره: {wd_id}\n💰 مبلغ: {money(amount)} {CURRENCY}\n📮 ولت: <code>{safe_name(wd_wallet)}</code>\n\n⏳ مبلغ ارسال خواهد شد.", parse_mode="HTML")
                else:
                    cursor.execute("UPDATE withdrawals SET status = 'rejected', processed_at = CURRENT_TIMESTAMP, processed_by = %s WHERE id = %s", (user.id, wd_id))
                    cursor.execute("UPDATE users SET balance = CAST(balance AS NUMERIC) + %s WHERE user_id = %s", (str(amount), wd_uid))
                    cursor.execute("INSERT INTO transactions(user_id, type, amount, description) VALUES (%s, 'withdraw_rejected_refund', %s, %s)", (wd_uid, str(amount), f"Withdraw rejected {wd_id}"))
                    db.commit()
                    try: await query.answer("❌ رد شد."); await query.edit_message_reply_markup(reply_markup=None)
                    except: pass
                    await safe_send_message(context.bot, wd_uid, f"❌ <b>درخواست برداشت شما رد شد.</b>\n\n🆔 شماره: {wd_id}\n💰 مبلغ: {money(amount)} {CURRENCY}\n💳 مبلغ به موجودی شما برگشت داده شد.", parse_mode="HTML")
        except Exception:
            db.rollback(); logger.exception("Withdraw callback failed")

async def wd_proof_callback(update, context):
    query, user = update.callback_query, update.callback_query.from_user
    if not is_owner(user.id):
        try: await query.answer("❌ دسترسی ندارید.", show_alert=True)
        except: pass
        return
    try: wd_id = int(query.data.replace("wd_proof_", ""))
    except: return
    with get_db() as db:
        with db.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
            cursor.execute("SELECT * FROM withdrawals WHERE id = %s", (wd_id,))
            wd = cursor.fetchone()
    if not wd or wd["status"] != "approved":
        try: await query.answer("❌ درخواست تأیید نشده.", show_alert=True)
        except: pass
        return
    target_id = int(wd["user_id"]); PENDING_PROOF[user.id] = target_id
    try: await query.answer("✅ در انتظار مدرک از شما")
    except: pass
    await safe_send_message(context.bot, query.message.chat_id, f"📤 <b>ارسال مدرک پرداخت</b>\n\n👤 گیرنده: <code>{target_id}</code>\n💰 مبلغ: {money(Decimal(wd['amount']))} {CURRENCY}\n\nحالا هر پیام (متن، عکس، ویس، ویدیو، سند) که بفرستی،\nمستقیم برای کاربر فوروارد می‌شود.\n\nبرای لغو: <code>لغو مدرک</code>", parse_mode="HTML")

async def owner_proof_handler(update, context):
    user, message = update.effective_user, update.message
    if not user or not message or not is_owner(user.id) or user.id not in PENDING_PROOF or message.chat.type != ChatType.PRIVATE: return
    if message.text and message.text.strip() == "لغو مدرک":
        target = PENDING_PROOF.pop(user.id, None); await message.reply_text(f"❌ ارسال مدرک لغو شد.\n(گیرنده: {target})"); return
    target_id = PENDING_PROOF[user.id]
    try:
        await message.copy(chat_id=target_id); await message.reply_text(f"✅ مدرک برای کاربر <code>{target_id}</code> ارسال شد.\n\nبرای لغو: <code>لغو مدرک</code>", parse_mode="HTML")
    except:
        logger.exception("Proof forward failed"); await message.reply_text("❌ ارسال ناموفق بود."); PENDING_PROOF.pop(user.id, None)

GAME_INFO = {"dice": ("🎲", "تاس"), "darts": ("🎯", "دارت"), "bowling": ("🎳", "بولینگ"), "basketball": ("🏀", "بسکتبال")}
GAME_NAMES = {"تاس": "dice", "دارت": "darts", "دارتس": "darts", "بولینگ": "bowling", "بولينگ": "bowling", "بسکتبال": "basketball", "dice": "dice", "darts": "darts", "bowling": "bowling", "basketball": "basketball"}
GAME_EMOJI = {"dice": "🎲", "darts": "🎯", "bowling": "🎳", "basketball": "🏀"}

def parse_game(text):
    text = normalize_digits(text.strip())
    match = re.match(r"^(\d+)\s+([^\s]+)\s+([0-9]+(?:\.[0-9]+)?)$", text)
    if not match: return None
    count = int(match.group(1)); name = match.group(2).lower(); bet = parse_amount(match.group(3))
    if count < 1: return None
    if MAX_GAME_COUNT is not None and count > MAX_GAME_COUNT: return "count_too_high"
    if bet is None or name not in GAME_NAMES: return None
    return (count, GAME_NAMES[name], bet)

def get_active_game_for_user(chat_id, user_id):
    with get_db() as db:
        with db.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
            cursor.execute("SELECT * FROM games WHERE chat_id = %s AND status IN ('waiting', 'creator_turn', 'opponent_turn', 'bot_turn', 'finishing') AND (creator_id = %s OR opponent_id = %s) ORDER BY id DESC LIMIT 1", (chat_id, user_id, user_id))
            return cursor.fetchone()

def get_game(game_id):
    with get_db() as db:
        with db.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
            cursor.execute("SELECT * FROM games WHERE id = %s", (game_id,))
            return cursor.fetchone()

def dice_to_points(game_type, dice_value):
    if game_type == "basketball": return 1 if int(dice_value) >= 4 else 0
    return int(dice_value)

def format_score(game_type, score):
    if game_type == "basketball": return f"{score} گل"
    return str(score)

async def create_game(update, context, count, game_type, bet):
    user, chat = update.effective_user, update.effective_chat
    ensure_user(user)
    if not await require_captcha_and_membership(update, context): return
    if not bot_enabled(): await update.message.reply_text("🔴 ربات خاموش است."); return
    if bet < MIN_GAME_BET: await update.message.reply_text(f"❌ حداقل شرط {money(MIN_GAME_BET)} {CURRENCY} است."); return
    if MAX_GAME_BET is not None and bet > MAX_GAME_BET: await update.message.reply_text(f"❌ حداکثر شرط {money(MAX_GAME_BET)} {CURRENCY} است."); return
    
    # ✏️ تغییر: پیام مناسب وقتی کاربر بازی فعال دارد
    active = get_active_game_for_user(chat.id, user.id)
    if active: 
        await update.message.reply_text(
            "⏳ <b>شما در حال حاضر در یک بازی مشغول هستید.</b>\n\n"
            "پس از تمام شدن بازی فعلی، می‌توانید بازی جدید بسازید.",
            parse_mode="HTML"
        )
        return
        
    try: change_balance(user.id, -bet, "game_bet", f"Game {game_type}")
    except ValueError:
        await update.message.reply_text(f"❌ موجودی شما کافی نیست.\n\n💰 موجودی: {money(get_balance(user.id))} {CURRENCY}"); return
    with get_db() as db:
        try:
            with db.cursor() as cursor:
                cursor.execute("INSERT INTO games(chat_id, creator_id, game_type, count, bet, mode, status, creator_total, opponent_total, creator_rolls, opponent_rolls) VALUES (%s, %s, %s, %s, %s, 'friends', 'waiting', 0, 0, 0, 0) RETURNING id", (chat.id, user.id, game_type, count, str(bet)))
                game_id = cursor.fetchone()[0]; db.commit()
        except:
            db.rollback()
            try: change_balance(user.id, bet, "game_create_rollback", f"Game create rollback {game_type}")
            except: pass
            await update.message.reply_text("❌ ساخت بازی انجام نشد."); return
    emoji, name = GAME_INFO[game_type]
    sent = await update.message.reply_text(f"🎮 بازی جدید\n\n{emoji} {name}\n🔢 تعداد: {count}\n💰 شرط: {money(bet)} {CURRENCY}\n👤 سازنده: {safe_name(user.first_name or user.id)}\n\nیکی از گزینه‌ها را انتخاب کنید.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👥 بازی با دوستان", callback_data=f"join_{game_id}")], [InlineKeyboardButton("🤖 بازی با ربات", callback_data=f"bot_{game_id}")], [InlineKeyboardButton("❌ لغو بازی", callback_data=f"cancel_{game_id}")]]))
    if sent:
        with get_db() as db:
            with db.cursor() as cursor: cursor.execute("UPDATE games SET message_id = %s WHERE id = %s", (sent.message_id, game_id)); db.commit()

async def join_game_callback(update, context):
    query, user = update.callback_query, update.callback_query.from_user
    if not await require_captcha_and_membership(update, context):
        try: await query.answer("❌ ابتدا تأیید امنیتی و عضویت را کامل کنید.", show_alert=True)
        except: pass
        return
    try: game_id = int(query.data.replace("join_", ""))
    except: return
    ensure_user(user)
    with get_db() as db:
        try:
            with db.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
                cursor.execute("SELECT * FROM games WHERE id = %s FOR UPDATE", (game_id,))
                game = cursor.fetchone()
                if not game or game["status"] != "waiting":
                    db.rollback(); await query.answer("❌ بازی دیگر فعال نیست.", show_alert=True); return
                if game["creator_id"] == user.id:
                    db.rollback(); await query.answer("❌ نمی‌توانید با خودتان بازی کنید.", show_alert=True); return
                
                # ✏️ چک کردن اینکه کاربر دیگری بازی فعال نداشته باشد
                cursor.execute("SELECT id FROM games WHERE chat_id = %s AND status IN ('waiting', 'creator_turn', 'opponent_turn', 'bot_turn', 'finishing') AND (creator_id = %s OR opponent_id = %s) LIMIT 1", (game["chat_id"], user.id, user.id))
                if cursor.fetchone():
                    db.rollback(); await query.answer("⏳ شما در یک بازی دیگر مشغول هستید!", show_alert=True); return
                
                bet = Decimal(game["bet"])
                cursor.execute("SELECT balance FROM users WHERE user_id = %s FOR UPDATE", (user.id,))
                row = cursor.fetchone()
                if not row or Decimal(row["balance"]) < bet:
                    db.rollback(); await query.answer("❌ موجودی شما کافی نیست.", show_alert=True); return
                cursor.execute("UPDATE users SET balance = CAST(balance AS NUMERIC) - %s WHERE user_id = %s", (str(bet), user.id))
                cursor.execute("INSERT INTO transactions(user_id, type, amount, description) VALUES (%s, 'game_bet', %s, %s)", (user.id, str(-bet), f"Join game {game_id}"))
                cursor.execute("UPDATE games SET opponent_id = %s, mode = 'friends', status = 'creator_turn', creator_rolls = 0, opponent_rolls = 0, creator_total = 0, opponent_total = 0 WHERE id = %s", (user.id, game_id))
                db.commit()
        except Exception:
            db.rollback(); logger.exception("Join game failed"); await query.answer("❌ پیوستن انجام نشد.", show_alert=True); return
    await safe_delete_message(context.bot, query.message.chat_id, query.message.message_id)
    emoji, name = GAME_INFO[game["game_type"]]
    sent = await safe_send_message(context.bot, query.message.chat_id, f"🎮 بازی دوستان شروع شد!\n\n{emoji} {name}\n🔢 تعداد پرتاب: {game['count']}\n💰 شرط هر نفر: {money(bet)} {CURRENCY}\n\n👤 {user_tag(game['creator_id'])}\n👤 {user_tag(user.id)}\n\n🎯 نوبت {user_tag(game['creator_id'])} است.\n{emoji} را {game['count']} بار بفرستید.", parse_mode="HTML")
    if sent:
        with get_db() as db:
            with db.cursor() as cursor: cursor.execute("UPDATE games SET message_id = %s WHERE id = %s", (sent.message_id, game_id)); db.commit()

async def bot_game_callback(update, context):
    query, user = update.callback_query, update.callback_query.from_user
    if not await require_captcha_and_membership(update, context):
        try: await query.answer("❌ ابتدا تأیید امنیتی و عضویت.", show_alert=True)
        except: pass
        return
    try: game_id = int(query.data.replace("bot_", ""))
    except: return
    with get_db() as db:
        try:
            with db.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
                cursor.execute("SELECT * FROM games WHERE id = %s FOR UPDATE", (game_id,))
                game = cursor.fetchone()
                if not game or game["status"] != "waiting" or game["creator_id"] != user.id:
                    db.rollback(); await query.answer("❌ بازی فعال نیست یا سازنده نیستید.", show_alert=True); return
                cursor.execute("UPDATE games SET opponent_id = -1, mode = 'bot', status = 'creator_turn', creator_rolls = 0, opponent_rolls = 0, creator_total = 0, opponent_total = 0 WHERE id = %s", (game_id,))
                db.commit()
        except:
            db.rollback(); return
    await safe_delete_message(context.bot, query.message.chat_id, query.message.message_id)
    emoji, name = GAME_INFO[game["game_type"]]
    sent = await safe_send_message(context.bot, query.message.chat_id, f"🤖 بازی با ربات شروع شد!\n\n{emoji} {name}\n🔢 تعداد پرتاب: {game['count']}\n💰 شرط: {money(Decimal(game['bet']))} {CURRENCY}\n\n🎯 نوبت {user_tag(user.id)} است.\n\n{emoji} را {game['count']} بار بفرستید.", parse_mode="HTML")
    if sent:
        with get_db() as db:
            with db.cursor() as cursor: cursor.execute("UPDATE games SET message_id = %s WHERE id = %s", (sent.message_id, game_id)); db.commit()

async def cancel_game_callback(update, context):
    query, user = update.callback_query, update.callback_query.from_user
    try: game_id = int(query.data.replace("cancel_", ""))
    except: return
    with get_db() as db:
        try:
            with db.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
                cursor.execute("SELECT * FROM games WHERE id = %s FOR UPDATE", (game_id,))
                game = cursor.fetchone()
                if not game or game["status"] != "waiting" or (game["creator_id"] != user.id and not is_owner(user.id)):
                    db.rollback(); await query.answer("❌ بازی قابل لغو نیست.", show_alert=True); return
                bet = Decimal(game["bet"])
                cursor.execute("UPDATE users SET balance = CAST(balance AS NUMERIC) + %s WHERE user_id = %s", (str(bet), game["creator_id"]))
                cursor.execute("INSERT INTO transactions(user_id, type, amount, description) VALUES (%s, 'game_refund', %s, %s)", (game["creator_id"], str(bet), f"Cancel game {game_id}"))
                cursor.execute("UPDATE games SET status = 'cancelled', finished_at = CURRENT_TIMESTAMP WHERE id = %s", (game_id,))
                db.commit()
        except:
            db.rollback(); await query.answer("❌ لغو انجام نشد.", show_alert=True); return
    try: await query.answer("بازی لغو شد.")
    except: pass
    await safe_delete_message(context.bot, query.message.chat_id, query.message.message_id)
    await safe_send_message(context.bot, query.message.chat_id, f"❌ بازی لغو شد.\n\n💰 {money(bet)} {CURRENCY} به سازنده برگشت داده شد.")

def dice_matches_game(dice_emoji, game_type): return dice_emoji == GAME_EMOJI[game_type]

async def user_game_dice_handler(update, context):
    message, user, chat = update.message, update.effective_user, update.effective_chat
    if not message or not user or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP) or not is_game_chat(chat) or not message.dice or is_banned(user.id): return
    game = get_active_game_for_user(chat.id, user.id)
    if not game or game["status"] not in ("creator_turn", "opponent_turn") or not dice_matches_game(message.dice.emoji, game["game_type"]): return

    if game["status"] == "creator_turn":
        if user.id != game["creator_id"]: return
        with get_db() as db:
            try:
                with db.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
                    cursor.execute("SELECT * FROM games WHERE id = %s FOR UPDATE", (game["id"],))
                    fresh = cursor.fetchone()
                    if not fresh or fresh["status"] != "creator_turn" or int(fresh["creator_rolls"]) >= int(fresh["count"]):
                        db.rollback(); return
                    new_rolls = int(fresh["creator_rolls"]) + 1
                    new_total = int(fresh["creator_total"]) + dice_to_points(fresh["game_type"], message.dice.value)
                    new_status = "bot_turn" if (new_rolls >= int(fresh["count"]) and fresh["mode"] == "bot") else ("opponent_turn" if new_rolls >= int(fresh["count"]) else "creator_turn")
                    cursor.execute("UPDATE games SET creator_rolls = %s, creator_total = %s, status = %s WHERE id = %s", (new_rolls, new_total, new_status, game["id"]))
                    db.commit()
            except:
                db.rollback(); return
        if game["mode"] == "bot" and new_rolls >= int(game["count"]):
            if await bot_roll_game(context, game["id"]) is not None: await finish_game(context, game["id"])
            return
        if game["mode"] == "friends" and new_rolls >= int(game["count"]):
            fresh = get_game(game["id"])
            if fresh: await safe_send_message(context.bot, chat.id, f"✅ پرتاب‌های سازنده تمام شد.\n\n🎯 حالا نوبت {user_tag(fresh['opponent_id'])} است.\n{GAME_EMOJI[game['game_type']]} را {game['count']} بار بفرستید.", parse_mode="HTML")
        return

    if game["status"] == "opponent_turn":
        if user.id != game["opponent_id"]: return
        with get_db() as db:
            try:
                with db.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
                    cursor.execute("SELECT * FROM games WHERE id = %s FOR UPDATE", (game["id"],))
                    fresh = cursor.fetchone()
                    if not fresh or fresh["status"] != "opponent_turn" or int(fresh["opponent_rolls"]) >= int(fresh["count"]):
                        db.rollback(); return
                    new_rolls = int(fresh["opponent_rolls"]) + 1
                    new_total = int(fresh["opponent_total"]) + dice_to_points(fresh["game_type"], message.dice.value)
                    new_status = "finishing" if new_rolls >= int(fresh["count"]) else "opponent_turn"
                    cursor.execute("UPDATE games SET opponent_rolls = %s, opponent_total = %s, status = %s WHERE id = %s", (new_rolls, new_total, new_status, game["id"]))
                    db.commit()
            except:
                db.rollback(); return
        if new_rolls >= int(game["count"]): await finish_game(context, game["id"])

async def bot_roll_game(context, game_id):
    game = get_game(game_id)
    if not game or game["mode"] != "bot" or game["status"] != "bot_turn": return None
    total = 0
    for _ in range(int(game["count"])):
        fresh = get_game(game_id)
        if not fresh or fresh["status"] != "bot_turn": return None
        msg = await safe_send_dice(context.bot, game["chat_id"], GAME_EMOJI[game["game_type"]])
        if not msg:
            await reset_game_internal(context, game_id, reason="bot_roll_failed"); return None
        try: total += dice_to_points(game["game_type"], msg.dice.value)
        except: pass
    with get_db() as db:
        try:
            with db.cursor() as cursor:
                cursor.execute("UPDATE games SET opponent_total = %s, opponent_rolls = %s, status = 'finishing' WHERE id = %s AND status = 'bot_turn'", (total, int(game["count"]), game_id))
                db.commit()
        except:
            db.rollback(); return None
    return total

async def finish_game(context, game_id):
    chat_id = game_type = creator_id = opponent_id = mode = result_type = winner_id = loser_id = None
    bet = payout_value = commission_value = refund_value = Decimal("0")
    creator_total = opponent_total = 0
    with get_db() as db:
        try:
            with db.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
                cursor.execute("SELECT * FROM games WHERE id = %s FOR UPDATE", (game_id,))
                game = cursor.fetchone()
                if not game or game["status"] != "finishing":
                    db.rollback(); return
                cursor.execute("UPDATE games SET status = 'finished', finished_at = CURRENT_TIMESTAMP WHERE id = %s", (game_id,))
                chat_id, game_type, creator_id, opponent_id = game["chat_id"], game["game_type"], int(game["creator_id"]), game["opponent_id"]
                mode, bet, creator_total, opponent_total = game["mode"], Decimal(game["bet"]), int(game["creator_total"]), int(game["opponent_total"])
                pool = bet * 2; payout = (bet * WIN_MULTIPLIER).quantize(Decimal("0.00000001")); commission = pool - payout

                def credit(uid, amt, ttype, desc):
                    if not isinstance(uid, int) or uid <= 0: return
                    cursor.execute("INSERT INTO users(user_id, balance) VALUES (%s, '0') ON CONFLICT (user_id) DO NOTHING", (uid,))
                    cursor.execute("UPDATE users SET balance = CAST(balance AS NUMERIC) + %s WHERE user_id = %s", (str(amt), uid))
                    cursor.execute("INSERT INTO transactions(user_id, type, amount, description) VALUES (%s, %s, %s, %s)", (uid, ttype, str(amt), desc))

                if mode == "bot":
                    if creator_total == opponent_total:
                        credit(creator_id, bet, "game_draw_refund", f"Bot draw {game_id}"); result_type = "bot_draw"; refund_value = bet
                    elif creator_total > opponent_total:
                        credit(creator_id, payout, "game_win", f"Bot game win {game_id}"); result_type = "bot_user_win"; winner_id = creator_id; payout_value = payout
                    else: result_type = "bot_bot_win"
                elif creator_total == opponent_total:
                    credit(creator_id, bet, "game_draw_refund", f"Draw {game_id}")
                    if opponent_id and int(opponent_id) > 0: credit(int(opponent_id), bet, "game_draw_refund", f"Draw {game_id}")
                    result_type = "two_draw"; refund_value = bet
                else:
                    winner = creator_id if creator_total > opponent_total else (int(opponent_id) if opponent_id and int(opponent_id) > 0 else None)
                    loser = (int(opponent_id) if opponent_id and int(opponent_id) > 0 else None) if creator_total > opponent_total else creator_id
                    if winner is None: db.rollback(); await reset_game_internal(context, game_id, reason="winner_missing"); return
                    credit(winner, payout, "game_win", f"Game win {game_id}"); credit(OWNER_ID, commission, "commission", f"Commission game {game_id}")
                    result_type = "two_win"; winner_id = winner; loser_id = loser; payout_value = payout; commission_value = commission
                db.commit()
        except Exception:
            db.rollback(); logger.exception("Finish game failed"); return

    emoji, name = GAME_INFO[game_type]
    creator_display = format_score(game_type, creator_total); opponent_display = format_score(game_type, opponent_total)
    creator_tag = user_tag(creator_id); opponent_tag = "🤖 ربات" if mode == "bot" else (user_tag(int(opponent_id)) if opponent_id else "?")

    if result_type in ("bot_draw", "two_draw"):
        await safe_send_message(context.bot, chat_id, f"🏁 <b>نتیجه بازی</b>\n\n{emoji} {name}\n\n👤 {creator_tag}: <b>{creator_display}</b>\n👤 {opponent_tag}: <b>{opponent_display}</b>\n\n🤝 مساوی شد.\n💰 {money(refund_value)} {CURRENCY} به هر نفر برگشت.", parse_mode="HTML")
        await send_pv_result(context, creator_id, name, "🤝 مساوی شدی!", f"💰 {money(refund_value)} {CURRENCY} برگشت.")
        if mode == "friends" and opponent_id and int(opponent_id) > 0: await send_pv_result(context, int(opponent_id), name, "🤝 مساوی شدی!", f"💰 {money(refund_value)} {CURRENCY} برگشت.")
        return
    if result_type == "bot_bot_win":
        await safe_send_message(context.bot, chat_id, f"🏁 <b>نتیجه بازی</b>\n\n{emoji} {name}\n\n👤 {creator_tag}: <b>{creator_display}</b>\n👤 🤖 ربات: <b>{opponent_display}</b>\n\n😔 {creator_tag} باخت.\n💰 {money(bet)} {CURRENCY} از دست رفت.", parse_mode="HTML")
        await send_pv_result(context, creator_id, name, "😔 باختی!", f"💰 {money(bet)} {CURRENCY} از دست رفت."); return
    if result_type == "bot_user_win":
        await safe_send_message(context.bot, chat_id, f"🏁 <b>نتیجه بازی</b>\n\n{emoji} {name}\n\n👤 {creator_tag}: <b>{creator_display}</b>\n👤 🤖 ربات: <b>{opponent_display}</b>\n\n🏆 {creator_tag} برنده شد!\n💰 جایزه: <b>{money(payout_value)} {CURRENCY}</b>", parse_mode="HTML")
        await send_pv_result(context, creator_id, name, "🏆 برنده شدی!", f"💰 جایزه: <b>{money(payout_value)} {CURRENCY}</b>"); return
    winner_tag = user_tag(winner_id); loser_tag = user_tag(loser_id) if loser_id else "?"
    await safe_send_message(context.bot, chat_id, f"🏁 <b>نتیجه بازی</b>\n\n{emoji} {name}\n\n👤 {creator_tag}: <b>{creator_display}</b>\n👤 {opponent_tag}: <b>{opponent_display}</b>\n\n🏆 {winner_tag} برنده شد!\n😔 {loser_tag} باخت.\n\n💰 جایزه برنده: <b>{money(payout_value)} {CURRENCY}</b>", parse_mode="HTML")
    await send_pv_result(context, winner_id, name, "🏆 برنده شدی!", f"💰 جایزه: <b>{money(payout_value)} {CURRENCY}</b>")
    if loser_id: await send_pv_result(context, loser_id, name, "😔 باختی!", f"💰 {money(bet)} {CURRENCY} از دست رفت.")

async def send_pv_result(context, user_id, game_name, header, detail):
    text = f"{header}\n\n🎮 بازی: {safe_name(game_name)}\n{detail}\n\n💰 موجودی: {money(get_balance(user_id))} {CURRENCY}"
    try: await safe_send_message(context.bot, user_id, text, parse_mode="HTML", reply_markup=main_keyboard())
    except: pass

async def reset_game_internal(context, game_id, reason="manual"):
    with get_db() as db:
        try:
            with db.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
                cursor.execute("SELECT * FROM games WHERE id = %s FOR UPDATE", (game_id,))
                game = cursor.fetchone()
                if not game or game["status"] in ("finished", "cancelled", "reset"):
                    db.rollback(); return False
                bet = Decimal(game["bet"])
                cursor.execute("UPDATE users SET balance = CAST(balance AS NUMERIC) + %s WHERE user_id = %s", (str(bet), game["creator_id"]))
                cursor.execute("INSERT INTO transactions(user_id, type, amount, description) VALUES (%s, 'game_reset_refund', %s, %s)", (game["creator_id"], str(bet), f"Reset {game_id}: {reason}"))
                if game["mode"] == "friends" and game["opponent_id"] and int(game["opponent_id"]) > 0:
                    cursor.execute("UPDATE users SET balance = CAST(balance AS NUMERIC) + %s WHERE user_id = %s", (str(bet), game["opponent_id"]))
                    cursor.execute("INSERT INTO transactions(user_id, type, amount, description) VALUES (%s, 'game_reset_refund', %s, %s)", (game["opponent_id"], str(bet), f"Reset {game_id}: {reason}"))
                cursor.execute("UPDATE games SET status = 'reset', finished_at = CURRENT_TIMESTAMP WHERE id = %s", (game_id,))
                db.commit()
        except:
            db.rollback(); logger.exception("Reset game failed"); return False
    return True

async def reset_game_command(update, context):
    user, chat = update.effective_user, update.effective_chat
    if not is_owner(user.id): await update.message.reply_text("❌ فقط مالک می‌تواند ریست کند."); return
    if context.args:
        try: game_id = int(normalize_digits(context.args[0]))
        except: await update.message.reply_text("❌ آیدی بازی صحیح نیست.\nمثال:\n/resetgame 25"); return
    else:
        with get_db() as db:
            with db.cursor() as cursor:
                cursor.execute("SELECT id FROM games WHERE chat_id = %s AND status NOT IN ('finished', 'cancelled', 'reset') ORDER BY id DESC LIMIT 1", (chat.id,))
                row = cursor.fetchone()
        if not row: await update.message.reply_text("❌ بازی فعالی در این گپ نیست."); return
        game_id = int(row["id"])
    if await reset_game_internal(context, game_id, reason=f"Owner {user.id}"): await update.message.reply_text(f"♻️ بازی ریست شد.\n🎮 Game ID: {game_id}")
    else: await update.message.reply_text("❌ بازی پیدا نشد یا بسته شده.")

async def reset_all_games_command(update, context):
    user = update.effective_user
    if not is_owner(user.id): await update.message.reply_text("❌ فقط مالک."); return
    with get_db() as db:
        with db.cursor() as cursor:
            cursor.execute("SELECT id FROM games WHERE status NOT IN ('finished', 'cancelled', 'reset')")
            rows = cursor.fetchall()
    count = sum([1 for row in rows if await reset_game_internal(context, int(row["id"]), reason=f"Owner all {user.id}")])
    await update.message.reply_text(f"♻️ ریست انجام شد.\n🎮 تعداد: {count}")

async def cleanup_old_games(context):
    with get_db() as db:
        with db.cursor() as cursor:
            cursor.execute("SELECT id FROM games WHERE status IN ('waiting', 'creator_turn', 'opponent_turn', 'bot_turn', 'finishing') AND created_at < NOW() - INTERVAL '%s minutes'", (GAME_TIMEOUT_MINUTES,))
            rows = cursor.fetchall()
    for row in rows: await reset_game_internal(context, int(row["id"]), reason="timeout")

async def cleanup_job(context):
    try: await cleanup_old_games(context)
    except: logger.exception("Cleanup job failed")

async def text_handler(update, context):
    message, user = update.message, update.effective_user
    if not message or not user or not message.text: return
    ensure_user(user)
    if not await require_captcha_and_membership(update, context): return
    raw_text = message.text.strip(); text = normalize_digits(raw_text).strip()
    step = context.user_data.get("withdraw_step")
    if step == "amount":
        if text == "لغو" or raw_text == "لغو": context.user_data.pop("withdraw_step", None); context.user_data.pop("withdraw_amount", None); await message.reply_text("❌ برداشت لغو شد.", reply_markup=main_keyboard()); return
        await handle_withdraw_amount(update, context, text); return
    if step == "wallet":
        if text == "لغو" or raw_text == "لغو": context.user_data.pop("withdraw_step", None); context.user_data.pop("withdraw_amount", None); await message.reply_text("❌ برداشت لغو شد.", reply_markup=main_keyboard()); return
        await handle_withdraw_wallet(update, context, text); return
    if is_owner(user.id) and await admin_balance_by_id(update, context): return
    if is_owner(user.id):
        m = re.match(r"^شارژ\s+([0-9]+(?:\.[0-9]+)?)$", text)
        if m: await admin_change_by_reply(update, context, "charge", parse_amount(m.group(1)) or Decimal("0")); return
        m = re.match(r"^کسر\s+([0-9]+(?:\.[0-9]+)?)$", text)
        if m: await admin_change_by_reply(update, context, "remove", parse_amount(m.group(1)) or Decimal("0")); return
    if raw_text == "بن" or raw_text.startswith("بن "):
        if not is_owner(user.id): await message.reply_text("❌ فقط مالک ربات می‌تونه بن کنه."); return
        target_id = message.reply_to_message.from_user.id if message.reply_to_message and message.reply_to_message.from_user else None
        if not target_id:
            m = re.match(r"^بن\s+(\d{5,15})$", text)
            if m: target_id = int(m.group(1))
        if not target_id: await message.reply_text("❌ روش استفاده:\n1️⃣ ریپلای + <code>بن</code>\n2️⃣ <code>بن 123456789</code>", parse_mode="HTML"); return
        if target_id == user.id or is_banned(target_id): await message.reply_text("❌ نامعتبر یا قبلا بن شده."); return
        old_balance = ban_user(target_id, reason="owner_ban", banned_by=user.id, zero_balance=True)
        if old_balance is None: await message.reply_text("❌ خطا در بن."); return
        await message.reply_text(f"🚫 <b>کاربر بن شد!</b>\n\n🆔 <code>{target_id}</code>\n💰 موجودی صفر شد: <b>{money(old_balance)} {CURRENCY}</b>", parse_mode="HTML")
        try: await safe_send_message(context.bot, target_id, "🚫 <b>شما از ربات بن شدید!</b>", parse_mode="HTML")
        except: pass
        return
    if raw_text.startswith("حذف بن"):
        if not is_owner(user.id): await message.reply_text("❌ فقط مالک."); return
        target_id = message.reply_to_message.from_user.id if message.reply_to_message and message.reply_to_message.from_user else None
        if not target_id:
            m = re.match(r"^حذف\s*بن\s+(\d{5,15})$", text)
            if m: target_id = int(m.group(1))
        if not target_id or not is_banned(target_id): await message.reply_text("❌ کاربر بن نیست یا آیدی نامعتبر."); return
        unban_user(target_id)
        await message.reply_text(f"✅ کاربر <code>{target_id}</code> از بن خارج شد.", parse_mode="HTML")
        try: await safe_send_message(context.bot, target_id, "✅ <b>شما از بن خارج شدید!</b>", parse_mode="HTML", reply_markup=main_keyboard())
        except: pass
        return
    if is_owner(user.id) and message.chat.type == ChatType.PRIVATE:
        if raw_text in ("📊 آمار ربات", "📊 آمار"): await send_admin_stats(update, context); return
        if raw_text == "🟢 روشن": set_bot_enabled(True); await message.reply_text("🟢 ربات روشن شد.", reply_markup=admin_reply_keyboard()); return
        if raw_text == "🔴 خاموش": set_bot_enabled(False); await message.reply_text("🔴 ربات خاموش شد.", reply_markup=admin_reply_keyboard()); return
        if raw_text in ("➕ شارژ کاربر", "➕ شارژ"): await ask_admin_balance(update, context, "charge"); return
        if raw_text in ("➖ کسر کاربر", "➖ کسر"): await ask_admin_balance(update, context, "remove"); return
        if raw_text == "🏧 برداشت‌های در انتظار": await send_admin_withdrawals(update, context); return
        if raw_text == "💰 موجودی کاربران": await send_admin_balances(update, context); return
        if raw_text == "➕ افزایش پاداش": set_referral_reward(get_referral_reward() + Decimal("0.01")); await message.reply_text(f"✅ پاداش افزایش یافت: {money(get_referral_reward())} {CURRENCY}", reply_markup=admin_reply_keyboard()); return
        if raw_text.startswith("➖ کاهش پاداش"): set_referral_reward(max(get_referral_reward() - Decimal("0.01"), Decimal("0"))); await message.reply_text(f"✅ پاداش کاهش یافت: {money(get_referral_reward())} {CURRENCY}", reply_markup=admin_reply_keyboard()); return
        if raw_text in ("🏠 منوی اصلی", "منوی اصلی"): await send_start_menu(context, message.chat.id, user.id); return
    if is_owner(user.id) and message.chat.type != ChatType.PRIVATE:
        if raw_text in ["📊 آمار ربات", "📊 آمار", "🟢 روشن", "🔴 خاموش", "➕ شارژ کاربر", "➕ شارژ", "➖ کسر کاربر", "➖ کسر", "🏧 برداشت‌های در انتظار", "💰 موجودی کاربران", "🚫 بن کاربر", "✅ آنبن کاربر", "➕ افزایش پاداش", "🏠 منوی اصلی"] or raw_text.startswith("➖ کاهش پاداش"):
            await message.reply_text("❌ پنل مدیریت فقط در پیوی کار می‌کند.", reply_markup=ReplyKeyboardRemove()); return
    if raw_text in ("💰 موجودی", "موجودی", "م", "موجودی من", "balance"): await show_balance(update, context); return
    if raw_text in ("👥 زیرمجموعه", "زیرمجموعه", "زیر مجموعه", "رفرال", "referral"): await show_referral(update, context); return
    if raw_text in ("🏧 برداشت TRX", "برداشت"): await show_withdraw_info(update, context); return
    if raw_text in ("🆔 آیدی من", "آیدی", "ایدی", "myid", "id"): await show_my_id(update, context); return
    if raw_text in ("📚 راهنما", "راهنما", "کمک", "help"): await show_help(update, context); return
    if text.startswith("انتقال"):
        match_id = re.match(r"^انتقال\s+([0-9]+(?:\.[0-9]+)?)\s+(\d{5,15})$", text); match_reply = re.match(r"^انتقال\s+([0-9]+(?:\.[0-9]+)?)$", text)
        if match_id: await transfer_by_id(update, context, parse_amount(match_id.group(1)) or Decimal("0"), int(match_id.group(2))); return
        if match_reply: await transfer(update, context, parse_amount(match_reply.group(1)) or Decimal("0")); return
        await message.reply_text("❌ فرمت صحیح:\n1️⃣ <code>انتقال 0.5</code>\n2️⃣ <code>انتقال 0.5 123456789</code>", parse_mode="HTML"); return
    game = parse_game(text)
    if game == "count_too_high": await message.reply_text(f"❌ حداکثر تعداد پرتاب {MAX_GAME_COUNT} است.\nمثال:\n3 تاس 0.5"); return
    if game:
        if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP): await message.reply_text("❌ بازی‌ها فقط داخل گپ انجام می‌شوند."); return
        if not is_game_chat(message.chat): await message.reply_text(f"❌ بازی فقط در گپ زیر مجاز است:\n{GROUP_URL}"); return
        count, game_type, bet = game; await create_game(update, context, count, game_type, bet); return

async def admin_command(update, context):
    user = update.effective_user
    if not is_owner(user.id): await update.message.reply_text("❌ دسترسی ندارید."); return
    if update.message.chat.type != ChatType.PRIVATE: await update.message.reply_text("❌ پنل مدیریت فقط در پیوی در دسترسه."); return
    await admin_panel(update, context)

async def balance_command(update, context): await show_balance(update, context)
async def referral_command(update, context): await show_referral(update, context)
async def help_command(update, context): await show_help(update, context)

async def error_handler(update, context):
    error = context.error
    if isinstance(error, (TimedOut, NetworkError)): return
    logger.exception("Unhandled exception", exc_info=error)

def main():
    if not BOT_TOKEN: raise RuntimeError("BOT_TOKEN تنظیم نشده است.")
    init_db_pool() 
    init_db()
    auto_ban_invalid_users()
    application = (Application.builder().token(BOT_TOKEN).connect_timeout(30).read_timeout(30).write_timeout(30).pool_timeout(30).build())
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("referral", referral_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("resetgame", reset_game_command))
    application.add_handler(CommandHandler("resetgames", reset_all_games_command))
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("unban", unban_command))
    application.add_handler(CommandHandler("banned", banned_list_command))
    application.add_handler(CallbackQueryHandler(membership_callback, pattern=r"^check_membership$"))
    application.add_handler(CallbackQueryHandler(captcha_callback, pattern=r"^cap_\d+_\d+$"))
    application.add_handler(CallbackQueryHandler(withdraw_callback, pattern=r"^wd_(ok|no)_\d+$"))
    application.add_handler(CallbackQueryHandler(wd_proof_callback, pattern=r"^wd_proof_\d+$"))
    application.add_handler(CallbackQueryHandler(join_game_callback, pattern=r"^join_\d+$"))
    application.add_handler(CallbackQueryHandler(bot_game_callback, pattern=r"^bot_\d+$"))
    application.add_handler(CallbackQueryHandler(cancel_game_callback, pattern=r"^cancel_\d+$"))
    application.add_handler(MessageHandler(filters.Dice.ALL, user_game_dice_handler))
    application.add_handler(MessageHandler((filters.PHOTO | filters.VOICE | filters.VIDEO | filters.Document.ALL | filters.AUDIO) & filters.ChatType.PRIVATE, owner_proof_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    application.add_error_handler(error_handler)
    if application.job_queue: application.job_queue.run_repeating(cleanup_job, interval=300, first=60)
    logger.info("BET_BT started successfully.")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)

if __name__ == "__main__":
    main()
