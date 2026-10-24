import os
import re
import random
import sqlite3
import logging
import asyncio
from decimal import Decimal, InvalidOperation
from contextlib import closing
from html import escape as html_escape

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from telegram.constants import ChatType
from telegram.error import TimedOut, NetworkError, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
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

DB_FILE = "bot.db"

CURRENCY = "TRX"

MIN_GAME_BET = Decimal("0.1")
MAX_GAME_BET = None
MAX_GAME_COUNT = 3

DEFAULT_REFERRAL_REWARD = Decimal("0.05")
MIN_WITHDRAWAL = Decimal("3.2")

COMMISSION_RATE = Decimal("0.10")
WIN_MULTIPLIER = Decimal("1.8")

GAME_TIMEOUT_MINUTES = 30

PENDING_PROOF: dict = {}
PENDING_CAPTCHA: dict = {}

# نشانه‌های نقطه که باعث بن می‌شوند
DOT_CHARS = [".", "۔", "•", "·", "⋅", "٫", "٬", "。", "｡", "…"]


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("BET_BT")


# ============================================================
# DATABASE
# ============================================================

def get_db():
    db = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    return db


def init_db():
    with closing(get_db()) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT DEFAULT '',
                first_name TEXT DEFAULT '',
                balance TEXT NOT NULL DEFAULT '0',
                referrer_id INTEGER DEFAULT NULL,
                captcha_verified INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                amount TEXT NOT NULL,
                description TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referred_id INTEGER NOT NULL UNIQUE,
                reward TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                creator_id INTEGER NOT NULL,
                opponent_id INTEGER,
                game_type TEXT NOT NULL,
                count INTEGER NOT NULL,
                bet TEXT NOT NULL,
                mode TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'waiting',
                creator_total INTEGER DEFAULT 0,
                opponent_total INTEGER DEFAULT 0,
                creator_rolls INTEGER DEFAULT 0,
                opponent_rolls INTEGER DEFAULT 0,
                message_id INTEGER DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMP DEFAULT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount TEXT NOT NULL,
                wallet TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_at TIMESTAMP DEFAULT NULL,
                processed_by INTEGER DEFAULT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS banned_users (
                user_id INTEGER PRIMARY KEY,
                reason TEXT DEFAULT '',
                banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                banned_by INTEGER DEFAULT NULL
            )
        """)

        user_columns = {
            row["name"]
            for row in db.execute("PRAGMA table_info(users)").fetchall()
        }
        if "captcha_verified" not in user_columns:
            try:
                db.execute(
                    "ALTER TABLE users ADD COLUMN captcha_verified INTEGER DEFAULT 0"
                )
            except Exception:
                logger.exception("Migration captcha_verified failed")

        columns = {
            row["name"]
            for row in db.execute("PRAGMA table_info(games)").fetchall()
        }
        migrations = {
            "creator_rolls": "ALTER TABLE games ADD COLUMN creator_rolls INTEGER DEFAULT 0",
            "opponent_rolls": "ALTER TABLE games ADD COLUMN opponent_rolls INTEGER DEFAULT 0",
            "message_id": "ALTER TABLE games ADD COLUMN message_id INTEGER DEFAULT NULL",
            "finished_at": "ALTER TABLE games ADD COLUMN finished_at TIMESTAMP DEFAULT NULL",
        }
        for column, sql in migrations.items():
            if column not in columns:
                try:
                    db.execute(sql)
                except Exception:
                    logger.exception("Migration failed: %s", column)

        db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('bot_enabled', '1')")
        db.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES ('referral_reward', ?)",
            (str(DEFAULT_REFERRAL_REWARD),)
        )
        db.commit()


# ============================================================
# HELPERS
# ============================================================

def normalize_digits(text):
    if text is None:
        return ""
    table = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789"
    )
    return str(text).translate(table)


def parse_amount(text):
    text = normalize_digits(str(text)).strip()
    text = text.replace("٫", ".").replace(",", ".")
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if value <= 0:
        return None
    return value


def money(value):
    value = Decimal(str(value))
    if value == value.to_integral():
        return str(int(value))
    return f"{value:.8f}".rstrip("0").rstrip(".")


def safe_name(name):
    return html_escape(str(name or ""))


def is_owner(user_id):
    try:
        return int(user_id) in OWNER_IDS
    except Exception:
        return False


def is_pv_chat_id(chat_id):
    try:
        return int(chat_id) > 0
    except Exception:
        return False


def is_valid_trx_address(addr):
    if not addr:
        return False
    a = str(addr).strip()
    return bool(re.match(r"^T[A-Za-z0-9]{33}$", a))


# ============================================================
# BAN LOGIC
# ============================================================

def is_banned(user_id):
    with closing(get_db()) as db:
        row = db.execute(
            "SELECT user_id FROM banned_users WHERE user_id = ?",
            (user_id,)
        ).fetchone()
    return bool(row)


def has_invalid_name(user):
    """
    True اگر:
      • اسم خالی باشد
      • یوزرنیم (پروفایل) خالی باشد
      • اسم شامل نقطه باشد
    """
    name = (user.first_name or "").strip()
    username = (user.username or "").strip()

    if not name:
        return True
    if not username:
        return True
    for d in DOT_CHARS:
        if d in name:
            return True
    return False


def get_ban_reason(user):
    name = (user.first_name or "").strip()
    username = (user.username or "").strip()

    if not name and not username:
        return "بدون اسم و بدون پروفایل"
    if not name:
        return "بدون اسم"
    if not username:
        return "بدون پروفایل (یوزرنیم)"

    for d in DOT_CHARS:
        if d in name:
            return f"اسم شامل نقطه ({d})"

    return "نامعتبر"


def ban_user(user_id, reason="", banned_by=None):
    """کاربر را بن می‌کند و موجودی‌اش را صفر می‌کند. موجودی قبلی را برمی‌گرداند."""
    with closing(get_db()) as db:
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT balance FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            old_balance = Decimal(row["balance"]) if row else Decimal("0")

            if not row:
                db.execute("""
                    INSERT OR IGNORE INTO users(user_id, balance)
                    VALUES (?, '0')
                """, (user_id,))

            db.execute("""
                INSERT OR REPLACE INTO banned_users(user_id, reason, banned_by)
                VALUES (?, ?, ?)
            """, (user_id, reason, banned_by))

            db.execute(
                "UPDATE users SET balance = '0' WHERE user_id = ?",
                (user_id,)
            )

            if old_balance > 0:
                db.execute("""
                    INSERT INTO transactions(user_id, type, amount, description)
                    VALUES (?, 'ban_zero', ?, ?)
                """, (user_id, str(-old_balance), f"Banned: {reason}"))

            db.commit()
            return old_balance
        except Exception:
            db.rollback()
            logger.exception("ban_user failed")
            return None


def unban_user(user_id):
    with closing(get_db()) as db:
        db.execute("DELETE FROM banned_users WHERE user_id = ?", (user_id,))
        db.commit()


def auto_ban_invalid_users():
    """کاربران قبلی که شرایط بن را دارند، بن می‌شوند."""
    with closing(get_db()) as db:
        rows = db.execute(
            "SELECT user_id, first_name, username FROM users"
        ).fetchall()

    count = 0
    for r in rows:
        uid = int(r["user_id"])
        if is_owner(uid):
            continue
        if is_banned(uid):
            continue

        name = (r["first_name"] or "").strip()
        username = (r["username"] or "").strip()

        reason = None
        if not name and not username:
            reason = "بدون اسم و بدون پروفایل"
        elif not name:
            reason = "بدون اسم"
        elif not username:
            reason = "بدون پروفایل (یوزرنیم)"
        else:
            for d in DOT_CHARS:
                if d in name:
                    reason = f"اسم شامل نقطه ({d})"
                    break

        if reason:
            ban_user(uid, reason=f"auto: {reason}")
            count += 1

    logger.info("Auto-ban: %s user(s) banned.", count)
    return count


# ============================================================
# USER HELPERS
# ============================================================

def ensure_user(user):
    if not user:
        return
    with closing(get_db()) as db:
        db.execute("""
            INSERT OR IGNORE INTO users(user_id, username, first_name, balance)
            VALUES (?, ?, ?, '0')
        """, (user.id, user.username or "", user.first_name or ""))
        db.execute("""
            UPDATE users SET username = ?, first_name = ?
            WHERE user_id = ?
        """, (user.username or "", user.first_name or "", user.id))
        db.commit()


def ensure_user_id(user_id, first_name="", username=""):
    with closing(get_db()) as db:
        db.execute("""
            INSERT OR IGNORE INTO users(user_id, username, first_name, balance)
            VALUES (?, ?, ?, '0')
        """, (user_id, username or "", first_name or ""))
        db.commit()


def get_balance(user_id):
    with closing(get_db()) as db:
        row = db.execute(
            "SELECT balance FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    if not row:
        return Decimal("0")
    try:
        return Decimal(row["balance"])
    except Exception:
        return Decimal("0")


def get_user_display_name(user_id):
    with closing(get_db()) as db:
        row = db.execute(
            "SELECT first_name, username FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()
    if row:
        if row["first_name"]:
            return row["first_name"]
        if row["username"]:
            return f"@{row['username']}"
    return f"کاربر {user_id}"


def user_tag(user_id):
    name = safe_name(get_user_display_name(user_id))
    return f'<a href="tg://user?id={user_id}">{name}</a>'


def is_captcha_verified(user_id):
    with closing(get_db()) as db:
        row = db.execute(
            "SELECT captcha_verified FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    return bool(row and row["captcha_verified"] == 1)


def set_captcha_verified(user_id):
    with closing(get_db()) as db:
        db.execute(
            "UPDATE users SET captcha_verified = 1 WHERE user_id = ?",
            (user_id,)
        )
        db.commit()


def get_referral_reward():
    with closing(get_db()) as db:
        row = db.execute(
            "SELECT value FROM settings WHERE key = 'referral_reward'"
        ).fetchone()
    if row:
        try:
            return Decimal(row["value"])
        except Exception:
            pass
    return DEFAULT_REFERRAL_REWARD


def set_referral_reward(value):
    with closing(get_db()) as db:
        db.execute("""
            INSERT INTO settings(key, value)
            VALUES ('referral_reward', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (str(value),))
        db.commit()


# ============================================================
# ATOMIC BALANCE
# ============================================================

def change_balance(user_id, amount, transaction_type, description=""):
    amount = Decimal(str(amount))
    with closing(get_db()) as db:
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT balance FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if not row:
                raise ValueError("user_not_found")
            current = Decimal(row["balance"])
            new_balance = current + amount
            if new_balance < 0:
                raise ValueError("insufficient_balance")
            db.execute(
                "UPDATE users SET balance = ? WHERE user_id = ?",
                (str(new_balance), user_id)
            )
            db.execute("""
                INSERT INTO transactions(user_id, type, amount, description)
                VALUES (?, ?, ?, ?)
            """, (user_id, transaction_type, str(amount), description))
            db.commit()
            return new_balance
        except Exception:
            db.rollback()
            raise


# ============================================================
# BOT STATUS
# ============================================================

def bot_enabled():
    with closing(get_db()) as db:
        row = db.execute(
            "SELECT value FROM settings WHERE key = 'bot_enabled'"
        ).fetchone()
    return bool(row and row["value"] == "1")


def set_bot_enabled(enabled):
    with closing(get_db()) as db:
        db.execute("""
            INSERT INTO settings(key, value)
            VALUES ('bot_enabled', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, ("1" if enabled else "0",))
        db.commit()


# ============================================================
# SAFE TELEGRAM
# ============================================================

async def safe_send_message(bot, chat_id, text, **kwargs):
    rm = kwargs.get("reply_markup")
    if isinstance(rm, ReplyKeyboardMarkup) and not is_pv_chat_id(chat_id):
        kwargs.pop("reply_markup", None)

    for attempt in range(6):
        try:
            return await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        except (TimedOut, NetworkError) as e:
            logger.warning("Telegram network error: %s", e)
            if attempt < 5:
                await asyncio.sleep(0.35)
        except TelegramError as e:
            logger.error("Telegram error: %s", e)
            return None
        except Exception as e:
            logger.exception("Unexpected Telegram error: %s", e)
            return None
    return None


async def safe_delete_message(bot, chat_id, message_id):
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except Exception as e:
        logger.warning("Delete message failed: %s", e)
        return False


async def safe_send_dice(bot, chat_id, emoji):
    for attempt in range(6):
        try:
            return await bot.send_dice(chat_id=chat_id, emoji=emoji)
        except (TimedOut, NetworkError) as e:
            logger.warning("Dice network error: %s", e)
            if attempt < 5:
                await asyncio.sleep(0.35)
        except TelegramError as e:
            logger.error("Dice Telegram error: %s", e)
            return None
        except Exception as e:
            logger.exception("Dice error: %s", e)
            return None
    return None


# ============================================================
# MEMBERSHIP
# ============================================================

async def check_membership(user_id, context):
    allowed = {"member", "administrator", "creator"}
    try:
        channel_member = await context.bot.get_chat_member(CHANNEL_USERNAME, user_id)
        group_member = await context.bot.get_chat_member(GROUP_USERNAME, user_id)
        return (
            channel_member.status in allowed
            and group_member.status in allowed
        )
    except Exception as e:
        logger.warning("Membership error: %s", e)
        return False


def join_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 عضویت در کانال BET_1XZX", url=CHANNEL_URL)],
        [InlineKeyboardButton("💬 عضویت در گپ GAP_BAZIN1", url=GROUP_URL)],
        [InlineKeyboardButton("✅ بررسی عضویت", callback_data="check_membership")],
    ])


async def send_join_required(update, context):
    text = (
        "🔒 <b>برای استفاده از ربات، ابتدا عضو شوید</b>\n\n"
        f"📢 کانال: {CHANNEL_USERNAME}\n"
        f"💬 گپ: {GROUP_USERNAME}\n\n"
        "پس از عضویت، روی دکمه «✅ بررسی عضویت» بزنید."
    )
    keyboard = join_keyboard()

    if update.message:
        await safe_send_message(
            context.bot, update.effective_chat.id, text,
            parse_mode="HTML", reply_markup=keyboard
        )
    elif update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, parse_mode="HTML", reply_markup=keyboard
            )
        except Exception:
            try:
                await context.bot.send_message(
                    chat_id=update.callback_query.message.chat_id,
                    text=text, parse_mode="HTML", reply_markup=keyboard
                )
            except Exception:
                pass


async def send_captcha(update, context):
    user = update.effective_user
    if not user:
        return

    chat = None
    if update.callback_query:
        chat = update.callback_query.message.chat
    elif update.effective_chat:
        chat = update.effective_chat
    if chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    a = random.randint(1, 9)
    b = random.randint(1, 9)
    op = random.choice(["+", "-"])
    if op == "+":
        answer = a + b
    else:
        if a < b:
            a, b = b, a
        answer = a - b

    options = {answer}
    while len(options) < 4:
        wrong = answer + random.randint(-5, 5)
        if wrong != answer and wrong >= 0:
            options.add(wrong)
    options = list(options)
    random.shuffle(options)

    PENDING_CAPTCHA[user.id] = {"answer": answer, "options": options}

    buttons = []
    row = []
    for idx, opt in enumerate(options):
        row.append(InlineKeyboardButton(
            str(opt), callback_data=f"cap_{user.id}_{idx}"
        ))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    text = (
        "🔐 <b>تأیید امنیتی</b>\n\n"
        "برای استفاده از ربات، پاسخ صحیح را انتخاب کن:\n\n"
        f"❓ <b>{a} {op} {b} = ?</b>"
    )
    keyboard = InlineKeyboardMarkup(buttons)

    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, parse_mode="HTML", reply_markup=keyboard
            )
            return
        except Exception:
            pass
        await safe_send_message(
            context.bot, update.callback_query.message.chat_id, text,
            parse_mode="HTML", reply_markup=keyboard
        )
    else:
        await safe_send_message(
            context.bot, update.effective_chat.id, text,
            parse_mode="HTML", reply_markup=keyboard
        )


async def require_captcha_and_membership(update, context):
    user = update.effective_user
    if not user:
        return False
    if is_owner(user.id):
        return True

    chat = update.effective_chat
    if update.callback_query:
        chat = update.callback_query.message.chat

    # 🚫 بن
    if is_banned(user.id):
        if chat and chat.type == ChatType.PRIVATE:
            await safe_send_message(
                context.bot, user.id,
                "🚫 <b>شما از ربات بن شده‌اید.</b>\n\n"
                "دسترسی شما به ربات مسدود است.",
                parse_mode="HTML"
            )
        return False

    # 🚫 اسم/پروفایل نامعتبر → بن خودکار
    if has_invalid_name(user):
        if not is_banned(user.id):
            reason = get_ban_reason(user)
            old = ban_user(user.id, reason=reason, banned_by=None)

            if chat and chat.type == ChatType.PRIVATE:
                await safe_send_message(
                    context.bot, user.id,
                    "🚫 <b>شما از ربات بن شدید!</b>\n\n"
                    f"📌 دلیل: <b>{reason}</b>\n\n"
                    "برای رفع بن:\n"
                    "• در تنظیمات تلگرام یک <b>اسم</b> بدون نقطه بگذارید\n"
                    "• یک <b>یوزرنیم (پروفایل)</b> تنظیم کنید\n\n"
                    "سپس دوباره /start بزنید.",
                    parse_mode="HTML"
                )

            if old and old > 0:
                try:
                    await safe_send_message(
                        context.bot, OWNER_ID,
                        f"🚫 <b>بن خودکار</b>\n\n"
                        f"👤 {safe_name(user.first_name or '-')}\n"
                        f"🔗 @{safe_name(user.username or '-')}\n"
                        f"🆔 <code>{user.id}</code>\n"
                        f"📌 دلیل: {reason}\n"
                        f"💰 موجودی صفر شد: {money(old)} {CURRENCY}",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
        else:
            if chat and chat.type == ChatType.PRIVATE:
                await safe_send_message(
                    context.bot, user.id,
                    "🚫 شما بن هستید.\n"
                    "ابتدا اسم (بدون نقطه) و پروفایل خود را تنظیم کنید.",
                    parse_mode="HTML"
                )
        return False

    # در گپ‌ها کپچا لازم نیست
    if chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        return True

    if not is_captcha_verified(user.id):
        await send_captcha(update, context)
        return False
    if not await check_membership(user.id, context):
        await send_join_required(update, context)
        return False
    return True


async def membership_callback(update, context):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    user = query.from_user

    if is_banned(user.id):
        try:
            await query.answer("🚫 شما بن هستید.", show_alert=True)
        except Exception:
            pass
        return

    if has_invalid_name(user):
        if not is_banned(user.id):
            reason = get_ban_reason(user)
            ban_user(user.id, reason=reason, banned_by=None)
            try:
                await query.answer("🚫 بن شدید.", show_alert=True)
            except Exception:
                pass
        return

    if await check_membership(user.id, context):
        if not is_owner(user.id) and not is_captcha_verified(user.id):
            await send_captcha(update, context)
            return
        try:
            await query.edit_message_text(
                "✅ <b>عضویت شما تأیید شد.</b>\n\n"
                "حالا می‌تونید از ربات استفاده کنید.",
                parse_mode="HTML"
            )
        except Exception:
            pass
        if not is_owner(user.id):
            await try_grant_referral(user.id, context)
        await send_start_menu(context, query.message.chat_id, user.id)
    else:
        try:
            await query.answer(
                "❌ هنوز عضو کانال و گپ نیستید!", show_alert=True
            )
        except Exception:
            pass


async def captcha_callback(update, context):
    query = update.callback_query
    user = query.from_user

    try:
        parts = query.data.split("_")
        target_uid = int(parts[1])
        idx = int(parts[2])
    except Exception:
        return

    if target_uid != user.id:
        try:
            await query.answer("❌ این کپچا برای شما نیست.", show_alert=True)
        except Exception:
            pass
        return

    data = PENDING_CAPTCHA.get(user.id)
    if not data:
        try:
            await query.answer(
                "❌ کپچا منقضی شده. /start را بزنید.", show_alert=True
            )
        except Exception:
            pass
        return

    options = data.get("options") or []
    if idx < 0 or idx >= len(options):
        return

    if options[idx] != data["answer"]:
        try:
            await query.answer(
                "❌ پاسخ اشتباه! دوباره تلاش کن.", show_alert=True
            )
        except Exception:
            pass
        return

    PENDING_CAPTCHA.pop(user.id, None)
    set_captcha_verified(user.id)

    try:
        await query.answer("✅ تأیید شد")
    except Exception:
        pass

    if not is_owner(user.id) and not await check_membership(user.id, context):
        try:
            await query.edit_message_text(
                "✅ تأیید امنیتی انجام شد.\n\n"
                "🔒 حالا باید در کانال و گپ عضو شوید.",
                parse_mode="HTML"
            )
        except Exception:
            pass
        await send_join_required(update, context)
        return

    try:
        await query.edit_message_text(
            "✅ <b>تأیید امنیتی انجام شد.</b>",
            parse_mode="HTML"
        )
    except Exception:
        pass

    if not is_owner(user.id):
        await try_grant_referral(user.id, context)

    await send_start_menu(context, query.message.chat_id, user.id)


# ============================================================
# REFERRAL GRANT (با پیام تضمینی)
# ============================================================

async def try_grant_referral(user_id, context):
    with closing(get_db()) as db:
        user_row = db.execute(
            "SELECT referrer_id FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not user_row or user_row["referrer_id"] is None:
            return
        referrer_id = int(user_row["referrer_id"])

        already = db.execute(
            "SELECT id FROM referrals WHERE referred_id = ?", (user_id,)
        ).fetchone()
        if already:
            return

    if not is_captcha_verified(user_id):
        return
    try:
        if not await check_membership(user_id, context):
            return
    except Exception:
        return

    reward_amount = get_referral_reward()

    with closing(get_db()) as db:
        try:
            db.execute("BEGIN IMMEDIATE")
            already = db.execute(
                "SELECT id FROM referrals WHERE referred_id = ?", (user_id,)
            ).fetchone()
            if already:
                db.rollback()
                return
            ref_exists = db.execute(
                "SELECT user_id FROM users WHERE user_id = ?", (referrer_id,)
            ).fetchone()
            if not ref_exists:
                db.rollback()
                return

            db.execute("""
                INSERT INTO referrals(referrer_id, referred_id, reward)
                VALUES (?, ?, ?)
            """, (referrer_id, user_id, str(reward_amount)))

            bal_row = db.execute(
                "SELECT balance FROM users WHERE user_id = ?", (referrer_id,)
            ).fetchone()
            old_balance = Decimal(bal_row["balance"])
            new_balance = old_balance + reward_amount
            db.execute(
                "UPDATE users SET balance = ? WHERE user_id = ?",
                (str(new_balance), referrer_id)
            )
            db.execute("""
                INSERT INTO transactions(user_id, type, amount, description)
                VALUES (?, 'referral', ?, ?)
            """, (referrer_id, str(reward_amount), f"Referral {user_id}"))
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Referral insert failed")
            return

    # 🔔 پیام به معرف
    try:
        with closing(get_db()) as db:
            r = db.execute(
                "SELECT first_name, username FROM users WHERE user_id = ?",
                (user_id,)
            ).fetchone()
        if r and r["first_name"]:
            ref_name = r["first_name"]
        elif r and r["username"]:
            ref_name = f"@{r['username']}"
        else:
            ref_name = str(user_id)
        ref_name = safe_name(ref_name)

        new_bal = get_balance(referrer_id)
        await safe_send_message(
            context.bot, referrer_id,
            "🎁 <b>زیرمجموعه جدید!</b>\n\n"
            f"👤 {ref_name} به زیرمجموعه‌های شما اضافه شد.\n\n"
            f"💰 پاداش: <b>{money(reward_amount)} {CURRENCY}</b>\n"
            f"💳 موجودی جدید: <b>{money(new_bal)} {CURRENCY}</b>",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
    except Exception:
        logger.warning("Referral PV notify failed")

    # 🔔 پیام به کاربر جدید
    try:
        await safe_send_message(
            context.bot, user_id,
            "🎁 <b>پاداش زیرمجموعه ثبت شد!</b>\n\n"
            "شما با لینک دعوت وارد شدید و پاداش برای معرف شما ثبت شد.",
            parse_mode="HTML",
        )
    except Exception:
        pass


# ============================================================
# GAME CHAT CHECK
# ============================================================

def is_game_chat(chat):
    try:
        username = (chat.username or "").lower()
        return username == GAME_CHAT_USERNAME.lower()
    except Exception:
        return False


# ============================================================
# REPLY KEYBOARDS
# ============================================================

def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["💰 موجودی", "👥 زیرمجموعه"],
            ["🏧 برداشت TRX", "🆔 آیدی من"],
            ["📚 راهنما"],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def admin_reply_keyboard():
    status = "🟢 روشن" if bot_enabled() else "🔴 خاموش"
    reward = get_referral_reward()
    return ReplyKeyboardMarkup(
        [
            ["📊 آمار ربات", status],
            ["➕ شارژ کاربر", "➖ کسر کاربر"],
            ["🏧 برداشت‌های در انتظار"],
            ["💰 موجودی کاربران"],
            ["🚫 بن کاربر", "✅ آنبن کاربر"],
            [
                f"➖ کاهش پاداش ({money(reward)})",
                "➕ افزایش پاداش",
            ],
            ["🏠 منوی اصلی"],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


# ============================================================
# START MENU
# ============================================================

async def send_start_menu(context, chat_id, user_id):
    text = (
        "🤖 BET_BT\n\n"
        f"💰 موجودی: {money(get_balance(user_id))} {CURRENCY}\n"
        f"🆔 آیدی شما: <code>{user_id}</code>\n\n"
        "از دکمه‌های پایین صفحه استفاده کنید 👇\n\n"
        "🎮 بازی فقط در گپ:\n"
        f"{GROUP_URL}\n\n"
        "مثال: <code>3 تاس 0.5</code>\n"
        f"• حداقل شرط: {money(MIN_GAME_BET)} {CURRENCY}\n"
        "• حداکثر شرط: بدون سقف\n"
        f"• حداکثر پرتاب: {MAX_GAME_COUNT}\n\n"
        "📌 نتیجه بازی در گپ ارسال می‌شود."
    )
    kwargs = {"parse_mode": "HTML"}
    if is_pv_chat_id(chat_id):
        kwargs["reply_markup"] = main_keyboard()
    await safe_send_message(context.bot, chat_id, text, **kwargs)


# ============================================================
# START
# ============================================================

async def start(update, context):
    user = update.effective_user
    if not user:
        return
    ensure_user(user)

    chat = update.effective_chat

    # 🚫 بن / اسم نامعتبر
    if not is_owner(user.id):
        if is_banned(user.id):
            if chat and chat.type == ChatType.PRIVATE:
                await safe_send_message(
                    context.bot, update.effective_chat.id,
                    "🚫 <b>شما از ربات بن شده‌اید.</b>",
                    parse_mode="HTML"
                )
            return

        if has_invalid_name(user):
            if not is_banned(user.id):
                reason = get_ban_reason(user)
                old = ban_user(user.id, reason=reason, banned_by=None)

                if chat and chat.type == ChatType.PRIVATE:
                    await safe_send_message(
                        context.bot, update.effective_chat.id,
                        "🚫 <b>شما از ربات بن شدید!</b>\n\n"
                        f"📌 دلیل: <b>{reason}</b>\n\n"
                        "اسم بدون نقطه و پروفایل تنظیم کنید و دوباره /start بزنید.",
                        parse_mode="HTML"
                    )

                if old and old > 0:
                    try:
                        await safe_send_message(
                            context.bot, OWNER_ID,
                            f"🚫 <b>بن خودکار</b>\n\n"
                            f"👤 {safe_name(user.first_name or '-')}\n"
                            f"🔗 @{safe_name(user.username or '-')}\n"
                            f"🆔 <code>{user.id}</code>\n"
                            f"📌 دلیل: {reason}\n"
                            f"💰 موجودی صفر شد: {money(old)} {CURRENCY}",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
            return

    # ثبت رفرال
    if context.args:
        arg = context.args[0]
        if arg.startswith("ref_"):
            try:
                referrer_id = int(arg.replace("ref_", ""))
            except Exception:
                referrer_id = None

            if referrer_id and referrer_id != user.id:
                with closing(get_db()) as db:
                    row = db.execute(
                        "SELECT referrer_id FROM users WHERE user_id = ?",
                        (user.id,)
                    ).fetchone()
                    if row and row["referrer_id"] is None:
                        ref_exists = db.execute(
                            "SELECT user_id FROM users WHERE user_id = ?",
                            (referrer_id,)
                        ).fetchone()
                        if ref_exists:
                            db.execute("""
                                UPDATE users SET referrer_id = ?
                                WHERE user_id = ?
                            """, (referrer_id, user.id))
                            db.commit()

    if chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    if not is_owner(user.id) and not is_captcha_verified(user.id):
        await send_captcha(update, context)
        return

    if not is_owner(user.id):
        if not await check_membership(user.id, context):
            await send_join_required(update, context)
            return
        await try_grant_referral(user.id, context)

    await send_start_menu(context, update.effective_chat.id, user.id)


# ============================================================
# USER SHOW FUNCTIONS
# ============================================================

async def show_balance(update, context, target=None):
    user = update.effective_user
    if not user:
        return
    ensure_user(user)
    text = (
        "💰 موجودی شما\n\n"
        f"{money(get_balance(user.id))} {CURRENCY}"
    )
    if target:
        await safe_send_message(context.bot, target, text, parse_mode="HTML")
    elif update.callback_query:
        try:
            await update.callback_query.edit_message_text(text)
        except Exception:
            pass
    else:
        kwargs = {"reply_to_message_id": update.message.message_id}
        if update.message.chat.type != ChatType.PRIVATE:
            kwargs["reply_markup"] = ReplyKeyboardRemove()
        await update.message.reply_text(text, **kwargs)


async def show_my_id(update, context, target=None):
    user = update.effective_user
    ensure_user(user)
    text = (
        "🆔 آیدی عددی شما\n\n"
        f"<code>{user.id}</code>\n\n"
        "این آیدی رو می‌تونی به دوستات بدی تا\n"
        "بدون ریپلای، برات TRX بفرستن:\n"
        f"<code>انتقال 0.5 {user.id}</code>"
    )
    if target:
        await safe_send_message(
            context.bot, target, text, parse_mode="HTML"
        )
    elif update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, parse_mode="HTML")
        except Exception:
            pass
    else:
        kwargs = {"reply_to_message_id": update.message.message_id, "parse_mode": "HTML"}
        if update.message.chat.type != ChatType.PRIVATE:
            kwargs["reply_markup"] = ReplyKeyboardRemove()
        await update.message.reply_text(text, **kwargs)


async def show_referral(update, context, target=None):
    user = update.effective_user
    if not user:
        return
    ensure_user(user)

    try:
        bot = await context.bot.get_me()
        username = bot.username
    except Exception:
        username = None

    if username:
        link = f"https://t.me/{username}?start=ref_{user.id}"
    else:
        link = "لینک دعوت در دسترس نیست."

    with closing(get_db()) as db:
        row = db.execute("""
            SELECT COUNT(*) AS total FROM referrals WHERE referrer_id = ?
        """, (user.id,)).fetchone()

    total = row["total"] if row else 0
    reward = get_referral_reward()

    text = (
        "👥 زیرمجموعه\n\n"
        f"🔗 لینک دعوت:\n{link}\n\n"
        f"👤 تعداد: {total}\n"
        f"🎁 پاداش هر نفر: {money(reward)} {CURRENCY}"
    )
    if target:
        await safe_send_message(context.bot, target, text)
    elif update.callback_query:
        try:
            await update.callback_query.edit_message_text(text)
        except Exception:
            pass
    else:
        kwargs = {"reply_to_message_id": update.message.message_id}
        if update.message.chat.type != ChatType.PRIVATE:
            kwargs["reply_markup"] = ReplyKeyboardRemove()
        await update.message.reply_text(text, **kwargs)


async def show_help(update, context, target=None):
    reward = get_referral_reward()
    text = (
        "📚 راهنمای BET_BT\n\n"
        "از دکمه‌های پایین صفحه استفاده کن.\n\n"
        "💰 موجودی — نمایش موجودی\n"
        f"👥 زیرمجموعه — پاداش {money(reward)} {CURRENCY}\n"
        f"🏧 برداشت TRX — حداقل {money(MIN_WITHDRAWAL)} {CURRENCY}\n"
        "🆔 آیدی من — آیدی عددی شما\n\n"
        "🔄 انتقال در گپ:\n"
        "با ریپلای: <code>انتقال 0.5</code>\n"
        "با آیدی: <code>انتقال 0.5 123456789</code>\n\n"
        "🎮 بازی (فقط در گپ GAP_BAZIN1):\n"
        "فرمت: <code>تعداد نوع شرط</code>\n"
        "مثال: <code>3 تاس 0.5</code>\n\n"
        f"• حداقل شرط: {money(MIN_GAME_BET)} {CURRENCY}\n"
        "• حداکثر شرط: بدون سقف\n"
        f"• حداکثر تعداد پرتاب: {MAX_GAME_COUNT}\n\n"
        "انواع بازی:\n"
        "🎲 تاس\n🎯 دارت\n🎳 بولینگ\n🏀 بسکتبال"
    )
    if target:
        await safe_send_message(context.bot, target, text, parse_mode="HTML")
    elif update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, parse_mode="HTML")
        except Exception:
            pass
    else:
        kwargs = {"reply_to_message_id": update.message.message_id, "parse_mode": "HTML"}
        if update.message.chat.type != ChatType.PRIVATE:
            kwargs["reply_markup"] = ReplyKeyboardRemove()
        await update.message.reply_text(text, **kwargs)


# ============================================================
# TRANSFER
# ============================================================

async def transfer(update, context, amount):
    user = update.effective_user
    message = update.message

    if not message.reply_to_message:
        await message.reply_text(
            "❌ برای انتقال باید روی پیام کاربر Reply کنید.\n\n"
            "مثال:\n<code>انتقال 0.5</code>",
            parse_mode="HTML"
        )
        return

    target = message.reply_to_message.from_user
    if not target:
        await message.reply_text("❌ گیرنده پیدا نشد.")
        return
    if target.id == user.id:
        await message.reply_text("❌ نمی‌توانید به خودتان انتقال دهید.")
        return
    if target.is_bot:
        await message.reply_text("❌ انتقال به ربات امکان‌پذیر نیست.")
        return
    if is_banned(target.id):
        await message.reply_text("❌ این کاربر بن است.")
        return

    ensure_user(target)
    error_code = None

    with closing(get_db()) as db:
        try:
            db.execute("BEGIN IMMEDIATE")
            sender = db.execute(
                "SELECT balance FROM users WHERE user_id = ?", (user.id,)
            ).fetchone()
            receiver = db.execute(
                "SELECT balance FROM users WHERE user_id = ?", (target.id,)
            ).fetchone()

            if not sender or not receiver:
                raise ValueError("user_not_found")

            sender_balance = Decimal(sender["balance"])
            receiver_balance = Decimal(receiver["balance"])

            if sender_balance < amount:
                raise ValueError("insufficient_balance")

            new_sender = sender_balance - amount
            new_receiver = receiver_balance + amount

            db.execute(
                "UPDATE users SET balance = ? WHERE user_id = ?",
                (str(new_sender), user.id)
            )
            db.execute(
                "UPDATE users SET balance = ? WHERE user_id = ?",
                (str(new_receiver), target.id)
            )
            db.execute("""
                INSERT INTO transactions(user_id, type, amount, description)
                VALUES (?, ?, ?, ?)
            """, (user.id, "transfer_out", str(-amount), f"To {target.id}"))
            db.execute("""
                INSERT INTO transactions(user_id, type, amount, description)
                VALUES (?, ?, ?, ?)
            """, (target.id, "transfer_in", str(amount), f"From {user.id}"))
            db.commit()
        except ValueError as e:
            db.rollback()
            error_code = str(e)
        except Exception:
            db.rollback()
            logger.exception("Transfer failed")
            error_code = "internal"

    if error_code == "insufficient_balance":
        await message.reply_text("❌ موجودی شما کافی نیست.")
        return
    if error_code == "user_not_found":
        await message.reply_text("❌ کاربر پیدا نشد.")
        return
    if error_code:
        await message.reply_text("❌ انتقال انجام نشد.")
        return

    await message.reply_text(
        "✅ انتقال انجام شد.\n\n"
        f"💸 مبلغ: {money(amount)} {CURRENCY}\n"
        f"👤 گیرنده: {safe_name(target.first_name or target.id)}\n"
        f"💰 موجودی شما: {money(get_balance(user.id))} {CURRENCY}"
    )


async def transfer_by_id(update, context, amount, target_id):
    user = update.effective_user
    message = update.message

    if target_id == user.id:
        await message.reply_text("❌ نمی‌توانید به خودتان انتقال دهید.")
        return
    if target_id <= 0:
        await message.reply_text("❌ آیدی عددی صحیح نیست.")
        return
    if is_banned(target_id):
        await message.reply_text("❌ این کاربر بن است.")
        return

    # چک وجود کاربر واقعی
    with closing(get_db()) as db:
        row = db.execute(
            "SELECT user_id FROM users WHERE user_id = ?", (target_id,)
        ).fetchone()
    if not row:
        await message.reply_text(
            "❌ کاربر مورد نظر در ربات ثبت نشده است.\n"
            "باید اول ربات را استارت کند."
        )
        return

    error_code = None
    with closing(get_db()) as db:
        try:
            db.execute("BEGIN IMMEDIATE")
            sender = db.execute(
                "SELECT balance FROM users WHERE user_id = ?", (user.id,)
            ).fetchone()
            receiver = db.execute(
                "SELECT balance FROM users WHERE user_id = ?", (target_id,)
            ).fetchone()

            if not sender or not receiver:
                raise ValueError("user_not_found")

            sender_balance = Decimal(sender["balance"])
            receiver_balance = Decimal(receiver["balance"])

            if sender_balance < amount:
                raise ValueError("insufficient_balance")

            new_sender = sender_balance - amount
            new_receiver = receiver_balance + amount

            db.execute(
                "UPDATE users SET balance = ? WHERE user_id = ?",
                (str(new_sender), user.id)
            )
            db.execute(
                "UPDATE users SET balance = ? WHERE user_id = ?",
                (str(new_receiver), target_id)
            )
            db.execute("""
                INSERT INTO transactions(user_id, type, amount, description)
                VALUES (?, ?, ?, ?)
            """, (user.id, "transfer_out", str(-amount), f"To {target_id}"))
            db.execute("""
                INSERT INTO transactions(user_id, type, amount, description)
                VALUES (?, ?, ?, ?)
            """, (target_id, "transfer_in", str(amount), f"From {user.id}"))
            db.commit()
        except ValueError as e:
            db.rollback()
            error_code = str(e)
        except Exception:
            db.rollback()
            logger.exception("Transfer by ID failed")
            error_code = "internal"

    if error_code == "insufficient_balance":
        await message.reply_text("❌ موجودی شما کافی نیست.")
        return
    if error_code == "user_not_found":
        await message.reply_text("❌ کاربر پیدا نشد.")
        return
    if error_code:
        await message.reply_text("❌ انتقال انجام نشد.")
        return

    target_name = safe_name(get_user_display_name(target_id))

    await message.reply_text(
        "✅ انتقال انجام شد.\n\n"
        f"💸 مبلغ: {money(amount)} {CURRENCY}\n"
        f"🆔 گیرنده: {target_id}\n"
        f"👤 نام: {target_name}\n"
        f"💰 موجودی شما: {money(get_balance(user.id))} {CURRENCY}"
    )

    try:
        await safe_send_message(
            context.bot, target_id,
            "💸 انتقال دریافت کردید!\n\n"
            f"👤 از: {safe_name(user.first_name or user.id)}\n"
            f"💰 مبلغ: {money(amount)} {CURRENCY}\n"
            f"💰 موجودی: {money(get_balance(target_id))} {CURRENCY}",
            reply_markup=main_keyboard()
        )
    except Exception:
        logger.warning("Could not notify receiver %s", target_id)


# ============================================================
# WITHDRAW FLOW
# ============================================================

async def show_withdraw_info(update, context, target=None):
    user = update.effective_user
    ensure_user(user)

    balance = get_balance(user.id)

    if balance < MIN_WITHDRAWAL:
        text = (
            "🏧 برداشت TRX\n\n"
            f"💰 موجودی شما: {money(balance)} {CURRENCY}\n"
            f"📉 حداقل برداشت: {money(MIN_WITHDRAWAL)} {CURRENCY}\n\n"
            "❌ موجودی شما برای برداشت کافی نیست."
        )
        chat_id = target or update.effective_chat.id
        await safe_send_message(
            context.bot, chat_id, text, reply_markup=main_keyboard()
        )
        return

    context.user_data["withdraw_step"] = "amount"
    context.user_data.pop("withdraw_amount", None)

    text = (
        "🏧 برداشت TRX\n\n"
        f"💰 موجودی شما: {money(balance)} {CURRENCY}\n"
        f"📉 حداقل برداشت: {money(MIN_WITHDRAWAL)} {CURRENCY}\n\n"
        "💵 لطفاً <b>مبلغ مورد نظر</b> را به TRX ارسال کنید.\n"
        "مثال: <code>5</code>\n\n"
        "برای لغو: <code>لغو</code>"
    )
    chat_id = target or update.effective_chat.id
    await safe_send_message(
        context.bot, chat_id, text,
        parse_mode="HTML", reply_markup=main_keyboard()
    )


async def handle_withdraw_amount(update, context, text):
    message = update.message
    user = update.effective_user

    amount = parse_amount(text)

    if amount is None:
        await message.reply_text(
            "❌ مبلغ وارد شده صحیح نیست.\n"
            "مثال: <code>5</code>\n\n"
            "برای لغو: <code>لغو</code>",
            parse_mode="HTML"
        )
        return

    if amount < MIN_WITHDRAWAL:
        await message.reply_text(
            f"❌ حداقل برداشت {money(MIN_WITHDRAWAL)} {CURRENCY} است.\n\n"
            "برای لغو: <code>لغو</code>",
            parse_mode="HTML"
        )
        return

    balance = get_balance(user.id)
    if amount > balance:
        await message.reply_text(
            f"❌ موجودی شما کافی نیست.\n"
            f"💰 موجودی: {money(balance)} {CURRENCY}\n\n"
            "برای لغو: <code>لغو</code>",
            parse_mode="HTML"
        )
        return

    context.user_data["withdraw_step"] = "wallet"
    context.user_data["withdraw_amount"] = str(amount)

    await message.reply_text(
        "📮 حالا <b>آدرس ولت TRX</b> خود را ارسال کنید.\n\n"
        "⚠️ آدرس باید با حرف <b>T</b> شروع شود و ۳۴ کاراکتر باشد.\n"
        "مثال:\n<code>TXYZ...abc</code>\n\n"
        "برای لغو: <code>لغو</code>",
        parse_mode="HTML"
    )


async def handle_withdraw_wallet(update, context, wallet):
    message = update.message
    user = update.effective_user

    wallet = wallet.strip()

    if not is_valid_trx_address(wallet):
        await message.reply_text(
            "❌ آدرس ولت TRX معتبر نیست.\n"
            "باید با <b>T</b> شروع شود و ۳۴ کاراکتر باشد.\n\n"
            "برای لغو: <code>لغو</code>",
            parse_mode="HTML"
        )
        return

    try:
        amount = Decimal(context.user_data.get("withdraw_amount", "0"))
    except Exception:
        amount = None

    if not amount or amount <= 0:
        context.user_data.pop("withdraw_step", None)
        context.user_data.pop("withdraw_amount", None)
        await message.reply_text("❌ خطا. دوباره تلاش کنید.")
        return

    context.user_data.pop("withdraw_step", None)
    context.user_data.pop("withdraw_amount", None)

    with closing(get_db()) as db:
        pending = db.execute("""
            SELECT id FROM withdrawals
            WHERE user_id = ? AND status = 'pending'
            LIMIT 1
        """, (user.id,)).fetchone()

    if pending:
        await message.reply_text(
            "❌ شما یک درخواست برداشت در انتظار دارید.\n"
            "لطفاً تا بررسی آن صبر کنید.",
            reply_markup=main_keyboard()
        )
        return

    try:
        change_balance(
            user.id, -amount, "withdraw",
            f"Withdraw {wallet[:10]}..."
        )
    except ValueError as e:
        if str(e) == "insufficient_balance":
            await message.reply_text("❌ موجودی کافی نیست.",
                                     reply_markup=main_keyboard())
        else:
            await message.reply_text("❌ برداشت انجام نشد.",
                                     reply_markup=main_keyboard())
        return
    except Exception:
        logger.exception("Withdraw deduction failed")
        await message.reply_text("❌ خطا در برداشت.",
                                 reply_markup=main_keyboard())
        return

    try:
        with closing(get_db()) as db:
            cursor = db.execute("""
                INSERT INTO withdrawals(user_id, amount, wallet)
                VALUES (?, ?, ?)
            """, (user.id, str(amount), wallet))
            wid = cursor.lastrowid
            db.commit()
    except Exception:
        logger.exception("Withdraw insert failed")
        try:
            change_balance(
                user.id, amount, "withdraw_rollback", "Insert failed"
            )
        except Exception:
            logger.exception("Withdraw rollback failed")
        await message.reply_text("❌ برداشت ثبت نشد.",
                                 reply_markup=main_keyboard())
        return

    short_wallet = (
        wallet if len(wallet) <= 40
        else wallet[:20] + "..." + wallet[-15:]
    )

    for owner in OWNER_IDS:
        try:
            await safe_send_message(
                context.bot, owner,
                "🏧 <b>درخواست برداشت جدید</b>\n\n"
                f"🆔 شماره: <code>{wid}</code>\n"
                f"👤 نام: {safe_name(user.first_name or '-')}\n"
                f"🔗 یوزر: @{safe_name(user.username or '-')}\n"
                f"🆔 آیدی: <code>{user.id}</code>\n"
                f"💰 مبلغ: <b>{money(amount)} {CURRENCY}</b>\n"
                f"📮 ولت:\n<code>{safe_name(short_wallet)}</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "✅ تأیید", callback_data=f"wd_ok_{wid}"
                        ),
                        InlineKeyboardButton(
                            "❌ رد", callback_data=f"wd_no_{wid}"
                        ),
                    ]
                ])
            )
        except Exception:
            logger.exception("Notify owner failed")

    await message.reply_text(
        "✅ درخواست برداشت ثبت شد.\n\n"
        f"🆔 شماره: {wid}\n"
        f"💰 مبلغ: {money(amount)} {CURRENCY}\n"
        f"📮 ولت: <code>{safe_name(short_wallet)}</code>\n\n"
        "⏳ در انتظار تأیید مدیریت.",
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )


# ============================================================
# ADMIN PANEL
# ============================================================

async def admin_panel(update, context):
    user = update.effective_user
    if not is_owner(user.id):
        await update.message.reply_text("❌ شما مالک ربات نیستید.")
        return

    status = "🟢 روشن" if bot_enabled() else "🔴 خاموش"
    reward = get_referral_reward()

    await update.message.reply_text(
        f"👑 <b>پنل مدیریت BET_BT</b>\n\n"
        f"وضعیت ربات: {status}\n"
        f"🎁 پاداش زیرمجموعه: {money(reward)} {CURRENCY}\n"
        f"💱 واحد پول: {CURRENCY}\n\n"
        "از دکمه‌های پایین صفحه استفاده کنید 👇",
        parse_mode="HTML",
        reply_markup=admin_reply_keyboard()
    )


async def ask_admin_balance(update, context, operation):
    user = update.effective_user
    context.user_data["admin_operation"] = operation
    context.user_data["admin_waiting"] = True

    title = "➕ شارژ موجودی" if operation == "charge" else "➖ کسر موجودی"

    kwargs = {"parse_mode": "HTML"}
    if update.message.chat.type != ChatType.PRIVATE:
        kwargs["reply_markup"] = ReplyKeyboardRemove()

    await update.message.reply_text(
        f"{title}\n\n"
        "فرمت ارسال:\n"
        "<code>آیدی_عددی مبلغ</code>\n\n"
        "مثال:\n"
        "<code>8935601841 100</code>\n\n"
        "برای لغو: <code>لغو</code>",
        **kwargs
    )


async def admin_balance_by_id(update, context):
    user = update.effective_user
    if not user or not is_owner(user.id):
        return False

    operation = context.user_data.get("admin_operation")
    waiting = context.user_data.get("admin_waiting")
    if not operation or not waiting:
        return False

    text = normalize_digits(update.message.text.strip())

    if text == "لغو":
        context.user_data.pop("admin_operation", None)
        context.user_data.pop("admin_waiting", None)

        kwargs = {"reply_markup": admin_reply_keyboard()}
        if update.message.chat.type != ChatType.PRIVATE:
            kwargs["reply_markup"] = ReplyKeyboardRemove()

        await update.message.reply_text(
            "❌ عملیات لغو شد.",
            **kwargs
        )
        return True

    match = re.match(r"^(\d+)\s+([0-9]+(?:\.[0-9]+)?)$", text)
    if not match:
        await update.message.reply_text(
            "❌ فرمت اشتباه.\n\nمثال:\n<code>8935601841 100</code>\n\n"
            "برای لغو: <code>لغو</code>",
            parse_mode="HTML"
        )
        return True

    target_id = int(match.group(1))
    amount = parse_amount(match.group(2))

    if amount is None:
        await update.message.reply_text("❌ مبلغ صحیح نیست.")
        return True
    if target_id <= 0:
        await update.message.reply_text("❌ آیدی کاربر صحیح نیست.")
        return True

    ensure_user_id(target_id)

    context.user_data.pop("admin_operation", None)
    context.user_data.pop("admin_waiting", None)

    try:
        if operation == "charge":
            new_balance = change_balance(
                target_id, amount, "admin_charge", f"Owner {user.id}"
            )
            title = "شارژ"
        else:
            new_balance = change_balance(
                target_id, -amount, "admin_remove", f"Owner {user.id}"
            )
            title = "کسر"
    except ValueError as e:
        if str(e) == "insufficient_balance":
            kwargs = {"reply_markup": admin_reply_keyboard()}
            if update.message.chat.type != ChatType.PRIVATE:
                kwargs["reply_markup"] = ReplyKeyboardRemove()
            await update.message.reply_text(
                "❌ موجودی کاربر برای کسر کافی نیست.",
                **kwargs
            )
        else:
            await update.message.reply_text("❌ عملیات انجام نشد.")
        return True
    except Exception:
        logger.exception("Admin balance by ID failed")
        await update.message.reply_text("❌ خطا در تغییر موجودی.")
        return True

    kwargs = {"reply_markup": admin_reply_keyboard()}
    if update.message.chat.type != ChatType.PRIVATE:
        kwargs["reply_markup"] = ReplyKeyboardRemove()

    await update.message.reply_text(
        f"✅ {title} انجام شد.\n\n"
        f"🆔 آیدی: {target_id}\n"
        f"💰 مبلغ: {money(amount)} {CURRENCY}\n"
        f"💳 موجودی جدید: {money(new_balance)} {CURRENCY}",
        **kwargs
    )
    return True


async def admin_change_by_reply(update, context, operation, amount):
    user = update.effective_user
    message = update.message

    if not is_owner(user.id):
        return

    if not message.reply_to_message:
        await message.reply_text(
            "❌ باید روی پیام کاربر Reply کنید.",
            reply_markup=admin_reply_keyboard()
        )
        return

    target = message.reply_to_message.from_user
    if not target:
        await message.reply_text("❌ کاربر پیدا نشد.")
        return
    if target.is_bot:
        await message.reply_text("❌ نمی‌توان موجودی ربات را تغییر داد.")
        return

    ensure_user(target)

    try:
        if operation == "charge":
            new_balance = change_balance(
                target.id, amount, "admin_charge", f"Owner {user.id}"
            )
            title = "شارژ"
        else:
            new_balance = change_balance(
                target.id, -amount, "admin_remove", f"Owner {user.id}"
            )
            title = "کسر"
    except ValueError as e:
        if str(e) == "insufficient_balance":
            await message.reply_text(
                "❌ موجودی کاربر برای کسر کافی نیست."
            )
        else:
            await message.reply_text("❌ عملیات انجام نشد.")
        return
    except Exception:
        logger.exception("Admin reply balance failed")
        await message.reply_text("❌ عملیات انجام نشد.")
        return

    kwargs = {}
    if message.chat.type != ChatType.PRIVATE:
        kwargs["reply_markup"] = ReplyKeyboardRemove()

    await message.reply_text(
        f"✅ {title} انجام شد.\n\n"
        f"👤 کاربر: {safe_name(target.first_name or target.id)}\n"
        f"🆔 آیدی: {target.id}\n"
        f"💰 مبلغ: {money(amount)} {CURRENCY}\n"
        f"💳 موجودی جدید: {money(new_balance)} {CURRENCY}",
        **kwargs
    )

    try:
        await safe_send_message(
            context.bot, target.id,
            f"💳 <b>موجودی شما تغییر کرد</b>\n\n"
            f"{'➕' if operation == 'charge' else '➖'} مبلغ: "
            f"<b>{money(amount)} {CURRENCY}</b>\n"
            f"💰 موجودی جدید: <b>{money(new_balance)} {CURRENCY}</b>",
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )
    except Exception:
        pass


async def send_admin_stats(update, context):
    user = update.effective_user
    if not is_owner(user.id):
        return

    with closing(get_db()) as db:
        users = db.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        total_row = db.execute("""
            SELECT COALESCE(SUM(CAST(balance AS REAL)), 0) AS total FROM users
        """).fetchone()
        referrals = db.execute("SELECT COUNT(*) AS c FROM referrals").fetchone()["c"]
        games = db.execute("SELECT COUNT(*) AS c FROM games").fetchone()["c"]
        pending_wd = db.execute("""
            SELECT COUNT(*) AS c FROM withdrawals WHERE status = 'pending'
        """).fetchone()["c"]
        verified = db.execute("""
            SELECT COUNT(*) AS c FROM users WHERE captcha_verified = 1
        """).fetchone()["c"]
        banned_count = db.execute("""
            SELECT COUNT(*) AS c FROM banned_users
        """).fetchone()["c"]

    total = float(total_row["total"] or 0)
    reward = get_referral_reward()

    await update.message.reply_text(
        "📊 <b>آمار BET_BT</b>\n\n"
        f"👤 کاربران: {users}\n"
        f"✅ تأیید‌شده (کپچا): {verified}\n"
        f"🚫 بن‌شده‌ها: {banned_count}\n"
        f"💰 مجموع موجودی: {total:.4f} {CURRENCY}\n"
        f"👥 زیرمجموعه‌ها: {referrals}\n"
        f"🎁 پاداش زیرمجموعه: {money(reward)} {CURRENCY}\n"
        f"🎮 بازی‌ها: {games}\n"
        f"🏧 برداشت در انتظار: {pending_wd}\n\n"
        f"وضعیت: {'🟢 روشن' if bot_enabled() else '🔴 خاموش'}",
        parse_mode="HTML",
        reply_markup=admin_reply_keyboard()
    )


async def send_admin_withdrawals(update, context):
    user = update.effective_user
    if not is_owner(user.id):
        return

    with closing(get_db()) as db:
        rows = db.execute("""
            SELECT * FROM withdrawals
            WHERE status = 'pending'
            ORDER BY id ASC
            LIMIT 10
        """).fetchall()

    if not rows:
        await update.message.reply_text(
            "🏧 برداشت‌های در انتظار\n\n✅ هیچ درخواست در انتظاری نیست.",
            reply_markup=admin_reply_keyboard()
        )
        return

    for wd in rows:
        wd_id = wd["id"]
        wd_uid = wd["user_id"]
        amount = wd["amount"]
        wallet = wd["wallet"]
        await safe_send_message(
            context.bot, update.effective_chat.id,
            f"🏧 درخواست #{wd_id}\n"
            f"👤 کاربر: <code>{wd_uid}</code>\n"
            f"💰 مبلغ: {money(Decimal(amount))} {CURRENCY}\n"
            f"📮 ولت: <code>{safe_name(wallet)}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "✅ تأیید", callback_data=f"wd_ok_{wd_id}"
                    ),
                    InlineKeyboardButton(
                        "❌ رد", callback_data=f"wd_no_{wd_id}"
                    ),
                ]
            ])
        )

    await update.message.reply_text(
        f"🏧 {len(rows)} درخواست در انتظار — به بالا اسکرول کنید.",
        reply_markup=admin_reply_keyboard()
    )


async def send_admin_balances(update, context):
    user = update.effective_user
    if not is_owner(user.id):
        return

    with closing(get_db()) as db:
        rows = db.execute("""
            SELECT user_id, username, first_name, balance
            FROM users
            ORDER BY CAST(balance AS REAL) DESC, user_id ASC
        """).fetchall()

    if not rows:
        await update.message.reply_text(
            "💰 موجودی کاربران\n\n❌ هنوز کاربری ثبت نشده.",
            reply_markup=admin_reply_keyboard()
        )
        return

    lines = [f"💰 <b>موجودی کاربران ({len(rows)})</b>\n"]
    for i, r in enumerate(rows, 1):
        if r["username"]:
            name = f"@{r['username']}"
        elif r["first_name"]:
            name = r["first_name"]
        else:
            name = str(r["user_id"])
        bal = money(Decimal(r["balance"]))
        lines.append(f"{i}_ {safe_name(name)} <b>{bal}</b>")

    text = "\n".join(lines)

    if len(text) <= 3800:
        await update.message.reply_text(
            text, parse_mode="HTML",
            reply_markup=admin_reply_keyboard()
        )
        return

    chunk = ""
    chunks = []
    for line in lines:
        if len(chunk) + len(line) + 1 > 3500:
            chunks.append(chunk)
            chunk = line + "\n"
        else:
            chunk += line + "\n"
    if chunk:
        chunks.append(chunk)

    for i, ch in enumerate(chunks):
        await safe_send_message(
            context.bot, update.effective_chat.id, ch,
            parse_mode="HTML",
            reply_markup=admin_reply_keyboard() if i == len(chunks) - 1 else None
        )


# ============================================================
# BAN COMMANDS
# ============================================================

async def ban_command(update, context):
    user = update.effective_user
    if not is_owner(user.id):
        await update.message.reply_text("❌ دسترسی ندارید.")
        return

    message = update.message
    target_id = None

    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
    elif context.args:
        try:
            target_id = int(normalize_digits(context.args[0]))
        except Exception:
            target_id = None

    if not target_id:
        await message.reply_text(
            "❌ استفاده:\n"
            "<code>/ban 123456789</code>\n"
            "یا روی پیام کاربر ریپلای کنید.",
            parse_mode="HTML"
        )
        return

    if target_id == user.id:
        await message.reply_text("❌ نمی‌توانید خودتان را بن کنید.")
        return

    if is_banned(target_id):
        await message.reply_text("ℹ️ این کاربر قبلاً بن شده است.")
        return

    old_balance = ban_user(target_id, reason="owner_ban", banned_by=user.id)

    if old_balance is None:
        await message.reply_text("❌ خطا در بن.")
        return

    target_name = safe_name(get_user_display_name(target_id))

    # 💬 پیام در چت جاری (گروه یا پیوی)
    await message.reply_text(
        "🚫 <b>کاربر از ربات بن شد!</b>\n\n"
        f"👤 {target_name}\n"
        f"🆔 <code>{target_id}</code>\n"
        f"💰 موجودی صفر شد: <b>{money(old_balance)} {CURRENCY}</b>\n\n"
        "✅ از ربات بن شد و موجودی صفر شد.",
        parse_mode="HTML"
    )

    # 💬 پیام در پیوی کاربر
    try:
        await safe_send_message(
            context.bot, target_id,
            "🚫 <b>شما از ربات بن شدید!</b>\n\n"
            f"💰 موجودی شما ({money(old_balance)} {CURRENCY}) صفر شد.\n"
            "دسترسی شما به ربات مسدود است.",
            parse_mode="HTML"
        )
    except Exception:
        logger.warning("Could not notify banned user %s", target_id)

    # 🔔 پیام به مالک در پیوی
    try:
        await safe_send_message(
            context.bot, OWNER_ID,
            f"🚫 کاربر <code>{target_id}</code> بن شد.\n"
            f"💰 موجودی صفرشده: {money(old_balance)} {CURRENCY}",
            parse_mode="HTML"
        )
    except Exception:
        pass


async def unban_command(update, context):
    user = update.effective_user
    if not is_owner(user.id):
        await update.message.reply_text("❌ دسترسی ندارید.")
        return

    message = update.message
    target_id = None

    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
    elif context.args:
        try:
            target_id = int(normalize_digits(context.args[0]))
        except Exception:
            target_id = None

    if not target_id:
        await message.reply_text(
            "❌ استفاده:\n"
            "<code>/unban 123456789</code>\n"
            "یا روی پیام کاربر ریپلای کنید.",
            parse_mode="HTML"
        )
        return

    if not is_banned(target_id):
        await message.reply_text("ℹ️ این کاربر بن نیست.")
        return

    unban_user(target_id)

    await message.reply_text(
        f"✅ کاربر <code>{target_id}</code> از بن خارج شد.\n\n"
        "⚠️ توجه: اگر هنوز اسم یا پروفایل نداشته باشد،\n"
        "با اولین /start دوباره بن می‌شود.",
        parse_mode="HTML"
    )

    try:
        await safe_send_message(
            context.bot, target_id,
            "✅ <b>شما از بن خارج شدید!</b>\n\n"
            "حالا می‌توانید از ربات استفاده کنید.\n"
            "⚠️ اگر اسم یا پروفایل نداشته باشید، دوباره بن می‌شوید.",
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )
    except Exception:
        logger.warning("Could not notify unbanned user %s", target_id)


async def banned_list_command(update, context):
    user = update.effective_user
    if not is_owner(user.id):
        return

    with closing(get_db()) as db:
        rows = db.execute("""
            SELECT b.user_id, b.reason, b.banned_at,
                   u.first_name, u.username
            FROM banned_users b
            LEFT JOIN users u ON u.user_id = b.user_id
            ORDER BY b.banned_at DESC
            LIMIT 50
        """).fetchall()

    if not rows:
        await update.message.reply_text("🚫 هیچ کاربری بن نیست.")
        return

    lines = [f"🚫 <b>لیست بن‌شده‌ها ({len(rows)})</b>\n"]
    for r in rows:
        name = r["first_name"] or r["username"] or "-"
        lines.append(
            f"• <code>{r['user_id']}</code> — {safe_name(name)} "
            f"({safe_name(r['reason'] or '-')})"
        )
    text = "\n".join(lines)

    if len(text) <= 3800:
        await update.message.reply_text(text, parse_mode="HTML")
        return

    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 3500:
            await safe_send_message(
                context.bot, update.effective_chat.id, chunk,
                parse_mode="HTML"
            )
            chunk = line + "\n"
        else:
            chunk += line + "\n"
    if chunk:
        await safe_send_message(
            context.bot, update.effective_chat.id, chunk,
            parse_mode="HTML"
        )


# ============================================================
# WITHDRAW CALLBACK
# ============================================================

async def withdraw_callback(update, context):
    query = update.callback_query
    user = query.from_user

    if not is_owner(user.id):
        try:
            await query.answer("❌ دسترسی ندارید.", show_alert=True)
        except Exception:
            pass
        return

    try:
        parts = query.data.split("_")
        action = parts[1]
        wd_id = int(parts[2])
    except Exception:
        return

    with closing(get_db()) as db:
        try:
            db.execute("BEGIN IMMEDIATE")
            wd = db.execute(
                "SELECT * FROM withdrawals WHERE id = ?", (wd_id,)
            ).fetchone()

            if not wd or wd["status"] != "pending":
                db.rollback()
                try:
                    await query.answer("❌ این درخواست در دسترس نیست.",
                                       show_alert=True)
                except Exception:
                    pass
                return

            amount = Decimal(wd["amount"])
            wd_user_id = int(wd["user_id"])
            wd_wallet = wd["wallet"]

            if action == "ok":
                row = db.execute(
                    "SELECT balance FROM users WHERE user_id = ?",
                    (wd_user_id,)
                ).fetchone()
                if not row:
                    # کاربر نیست → برگرداندن پول به حساب شبح نمی‌شود، پس رد کن
                    db.rollback()
                    try:
                        await query.answer("❌ کاربر پیدا نشد.", show_alert=True)
                    except Exception:
                        pass
                    return

                db.execute("""
                    UPDATE withdrawals
                    SET status = 'approved',
                        processed_at = CURRENT_TIMESTAMP,
                        processed_by = ?
                    WHERE id = ?
                """, (user.id, wd_id))
                db.commit()

                try:
                    await query.answer("✅ تأیید شد.")
                except Exception:
                    pass

                try:
                    await query.edit_message_reply_markup(
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton(
                                "📤 ارسال مدرک پرداخت",
                                callback_data=f"wd_proof_{wd_id}"
                            )]
                        ])
                    )
                except Exception:
                    pass

                await safe_send_message(
                    context.bot, wd_user_id,
                    "✅ <b>درخواست برداشت شما تأیید شد.</b>\n\n"
                    f"🆔 شماره: {wd_id}\n"
                    f"💰 مبلغ: {money(amount)} {CURRENCY}\n"
                    f"📮 ولت: <code>{safe_name(wd_wallet)}</code>\n\n"
                    "⏳ مبلغ به آدرس شما ارسال خواهد شد.\n"
                    "مدرک پرداخت نیز برایتان ارسال می‌شود.",
                    parse_mode="HTML"
                )
            else:
                db.execute("""
                    UPDATE withdrawals
                    SET status = 'rejected',
                        processed_at = CURRENT_TIMESTAMP,
                        processed_by = ?
                    WHERE id = ?
                """, (user.id, wd_id))

                row = db.execute(
                    "SELECT balance FROM users WHERE user_id = ?",
                    (wd_user_id,)
                ).fetchone()
                if row:
                    current = Decimal(row["balance"])
                    new_balance = current + amount
                    db.execute(
                        "UPDATE users SET balance = ? WHERE user_id = ?",
                        (str(new_balance), wd_user_id)
                    )
                    db.execute("""
                        INSERT INTO transactions(user_id, type, amount, description)
                        VALUES (?, ?, ?, ?)
                    """, (
                        wd_user_id, "withdraw_rejected_refund",
                        str(amount), f"Withdraw rejected {wd_id}"
                    ))
                db.commit()

                try:
                    await query.answer("❌ رد شد.")
                except Exception:
                    pass
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except Exception:
                    pass

                await safe_send_message(
                    context.bot, wd_user_id,
                    f"❌ <b>درخواست برداشت شما رد شد.</b>\n\n"
                    f"🆔 شماره: {wd_id}\n"
                    f"💰 مبلغ: {money(amount)} {CURRENCY}\n"
                    "💳 مبلغ به موجودی شما برگشت داده شد.",
                    parse_mode="HTML"
                )
        except Exception:
            db.rollback()
            logger.exception("Withdraw callback failed")
            try:
                await query.answer("❌ خطا در پردازش.", show_alert=True)
            except Exception:
                pass


async def wd_proof_callback(update, context):
    query = update.callback_query
    user = query.from_user

    if not is_owner(user.id):
        try:
            await query.answer("❌ دسترسی ندارید.", show_alert=True)
        except Exception:
            pass
        return

    try:
        wd_id = int(query.data.replace("wd_proof_", ""))
    except Exception:
        return

    with closing(get_db()) as db:
        wd = db.execute(
            "SELECT * FROM withdrawals WHERE id = ?", (wd_id,)
        ).fetchone()

    if not wd or wd["status"] != "approved":
        try:
            await query.answer("❌ این درخواست تأیید نشده.", show_alert=True)
        except Exception:
            pass
        return

    target_id = int(wd["user_id"])
    PENDING_PROOF[user.id] = target_id

    try:
        await query.answer("✅ در انتظار مدرک از شما")
    except Exception:
        pass

    await safe_send_message(
        context.bot, query.message.chat_id,
        "📤 <b>ارسال مدرک پرداخت</b>\n\n"
        f"👤 گیرنده: <code>{target_id}</code>\n"
        f"💰 مبلغ: {money(Decimal(wd['amount']))} {CURRENCY}\n\n"
        "حالا هر پیام (متن، عکس، ویس، ویدیو، سند) که بفرستی،\n"
        "مستقیم برای کاربر فوروارد می‌شود.\n\n"
        "برای لغو: <code>لغو مدرک</code>",
        parse_mode="HTML"
    )


async def owner_proof_handler(update, context):
    user = update.effective_user
    message = update.message

    if not user or not message:
        return
    if not is_owner(user.id):
        return
    if user.id not in PENDING_PROOF:
        return
    if message.chat.type != ChatType.PRIVATE:
        return

    if message.text and message.text.strip() == "لغو مدرک":
        target = PENDING_PROOF.pop(user.id, None)
        await message.reply_text(f"❌ ارسال مدرک لغو شد.\n(گیرنده: {target})")
        return

    target_id = PENDING_PROOF[user.id]

    try:
        await message.copy(chat_id=target_id)
        await message.reply_text(
            f"✅ مدرک برای کاربر <code>{target_id}</code> ارسال شد.\n\n"
            "برای لغو: <code>لغو مدرک</code>",
            parse_mode="HTML"
        )
    except Exception:
        logger.exception("Proof forward failed")
        await message.reply_text(
            "❌ ارسال ناموفق بود. ممکنه کاربر ربات رو بلاک کرده باشه."
        )
        PENDING_PROOF.pop(user.id, None)


# ============================================================
# GAME CONFIG
# ============================================================

GAME_INFO = {
    "dice": ("🎲", "تاس"),
    "darts": ("🎯", "دارت"),
    "bowling": ("🎳", "بولینگ"),
    "basketball": ("🏀", "بسکتبال"),
}

GAME_NAMES = {
    "تاس": "dice", "دارت": "darts", "دارتس": "darts",
    "بولینگ": "bowling", "بولينگ": "bowling",
    "بسکتبال": "basketball",
    "dice": "dice", "darts": "darts",
    "bowling": "bowling", "basketball": "basketball",
}

GAME_EMOJI = {
    "dice": "🎲", "darts": "🎯",
    "bowling": "🎳", "basketball": "🏀",
}


def parse_game(text):
    text = normalize_digits(text.strip())
    pattern = r"^(\d+)\s+([^\s]+)\s+([0-9]+(?:\.[0-9]+)?)$"
    match = re.match(pattern, text)
    if not match:
        return None

    count = int(match.group(1))
    name = match.group(2).lower()
    bet = parse_amount(match.group(3))

    if count < 1:
        return None
    if MAX_GAME_COUNT is not None and count > MAX_GAME_COUNT:
        return "count_too_high"
    if bet is None:
        return None
    if name not in GAME_NAMES:
        return None

    return (count, GAME_NAMES[name], bet)


def get_active_game_for_user(chat_id, user_id):
    with closing(get_db()) as db:
        row = db.execute("""
            SELECT * FROM games
            WHERE chat_id = ?
              AND status IN (
                  'waiting', 'creator_turn', 'opponent_turn',
                  'bot_turn', 'finishing'
              )
              AND (creator_id = ? OR opponent_id = ?)
            ORDER BY id DESC LIMIT 1
        """, (chat_id, user_id, user_id)).fetchone()
    return row


def get_game(game_id):
    with closing(get_db()) as db:
        return db.execute(
            "SELECT * FROM games WHERE id = ?", (game_id,)
        ).fetchone()


def dice_to_points(game_type, dice_value):
    if game_type == "basketball":
        return 1 if int(dice_value) >= 4 else 0
    return int(dice_value)


def format_score(game_type, score):
    if game_type == "basketball":
        return f"{score} گل"
    return str(score)


# ============================================================
# CREATE GAME
# ============================================================

async def create_game(update, context, count, game_type, bet):
    user = update.effective_user
    chat = update.effective_chat

    ensure_user(user)

    if not await require_captcha_and_membership(update, context):
        return
    if not bot_enabled():
        await update.message.reply_text("🔴 ربات خاموش است.")
        return
    if bet < MIN_GAME_BET:
        await update.message.reply_text(
            f"❌ حداقل شرط {money(MIN_GAME_BET)} {CURRENCY} است."
        )
        return
    if MAX_GAME_BET is not None and bet > MAX_GAME_BET:
        await update.message.reply_text(
            f"❌ حداکثر شرط {money(MAX_GAME_BET)} {CURRENCY} است."
        )
        return

    active = get_active_game_for_user(chat.id, user.id)
    if active:
        await update.message.reply_text(
            "❌ شما در حال حاضر یک بازی فعال دارید.\n"
            "ابتدا آن بازی را تمام یا ریست کنید."
        )
        return

    try:
        change_balance(user.id, -bet, "game_bet", f"Game {game_type}")
    except ValueError as e:
        if str(e) == "insufficient_balance":
            await update.message.reply_text(
                "❌ موجودی شما کافی نیست.\n\n"
                f"💰 موجودی: {money(get_balance(user.id))} {CURRENCY}"
            )
        else:
            await update.message.reply_text("❌ ساخت بازی انجام نشد.")
        return

    with closing(get_db()) as db:
        try:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute("""
                INSERT INTO games(
                    chat_id, creator_id, game_type, count, bet,
                    mode, status,
                    creator_total, opponent_total,
                    creator_rolls, opponent_rolls
                )
                VALUES (?, ?, ?, ?, ?, 'friends', 'waiting', 0, 0, 0, 0)
            """, (chat.id, user.id, game_type, count, str(bet)))
            game_id = cursor.lastrowid
            db.commit()
        except Exception:
            db.rollback()
            try:
                change_balance(
                    user.id, bet, "game_create_rollback",
                    f"Game create rollback {game_type}"
                )
            except Exception:
                logger.exception("Game creation rollback failed")
            await update.message.reply_text("❌ ساخت بازی انجام نشد.")
            return

    emoji, name = GAME_INFO[game_type]

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 بازی با دوستان", callback_data=f"join_{game_id}")],
        [InlineKeyboardButton("🤖 بازی با ربات", callback_data=f"bot_{game_id}")],
        [InlineKeyboardButton("❌ لغو بازی", callback_data=f"cancel_{game_id}")],
    ])

    sent = await update.message.reply_text(
        "🎮 بازی جدید\n\n"
        f"{emoji} {name}\n"
        f"🔢 تعداد: {count}\n"
        f"💰 شرط: {money(bet)} {CURRENCY}\n"
        f"👤 سازنده: {safe_name(user.first_name or user.id)}\n\n"
        "یکی از گزینه‌ها را انتخاب کنید.",
        reply_markup=keyboard
    )

    if sent:
        with closing(get_db()) as db:
            db.execute(
                "UPDATE games SET message_id = ? WHERE id = ?",
                (sent.message_id, game_id)
            )
            db.commit()


# ============================================================
# JOIN / BOT / CANCEL GAME
# ============================================================

async def join_game_callback(update, context):
    query = update.callback_query
    user = query.from_user

    if not await require_captcha_and_membership(update, context):
        try:
            await query.answer(
                "❌ ابتدا تأیید امنیتی و عضویت را کامل کنید.",
                show_alert=True
            )
        except Exception:
            pass
        return

    try:
        game_id = int(query.data.replace("join_", ""))
    except Exception:
        return

    ensure_user(user)

    with closing(get_db()) as db:
        try:
            db.execute("BEGIN IMMEDIATE")
            game = db.execute(
                "SELECT * FROM games WHERE id = ?", (game_id,)
            ).fetchone()

            if not game:
                db.rollback()
                await query.answer("❌ بازی پیدا نشد.", show_alert=True)
                return
            if game["status"] != "waiting":
                db.rollback()
                await query.answer("❌ این بازی دیگر فعال نیست.", show_alert=True)
                return
            if game["creator_id"] == user.id:
                db.rollback()
                await query.answer("❌ نمی‌توانید با خودتان بازی کنید.",
                                   show_alert=True)
                return

            bet = Decimal(game["bet"])
            balance_row = db.execute(
                "SELECT balance FROM users WHERE user_id = ?", (user.id,)
            ).fetchone()
            if not balance_row:
                db.rollback()
                await query.answer("❌ کاربر پیدا نشد.", show_alert=True)
                return

            current = Decimal(balance_row["balance"])
            if current < bet:
                db.rollback()
                await query.answer("❌ موجودی شما کافی نیست.", show_alert=True)
                return

            new_balance = current - bet
            db.execute(
                "UPDATE users SET balance = ? WHERE user_id = ?",
                (str(new_balance), user.id)
            )
            db.execute("""
                INSERT INTO transactions(user_id, type, amount, description)
                VALUES (?, ?, ?, ?)
            """, (user.id, "game_bet", str(-bet), f"Join game {game_id}"))

            db.execute("""
                UPDATE games
                SET opponent_id = ?, mode = 'friends', status = 'creator_turn',
                    creator_rolls = 0, opponent_rolls = 0,
                    creator_total = 0, opponent_total = 0
                WHERE id = ?
            """, (user.id, game_id))
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Join game failed")
            await query.answer("❌ پیوستن انجام نشد.", show_alert=True)
            return

    await safe_delete_message(
        context.bot, query.message.chat_id, query.message.message_id
    )

    emoji, name = GAME_INFO[game["game_type"]]
    creator_tag = user_tag(game["creator_id"])
    opponent_tag = user_tag(user.id)

    sent = await safe_send_message(
        context.bot, query.message.chat_id,
        "🎮 بازی دوستان شروع شد!\n\n"
        f"{emoji} {name}\n"
        f"🔢 تعداد پرتاب: {game['count']}\n"
        f"💰 شرط هر نفر: {money(bet)} {CURRENCY}\n\n"
        f"👤 {creator_tag}\n👤 {opponent_tag}\n\n"
        f"🎯 نوبت {creator_tag} است.\n"
        f"{emoji} را {game['count']} بار بفرستید.",
        parse_mode="HTML"
    )

    if sent:
        with closing(get_db()) as db:
            db.execute(
                "UPDATE games SET message_id = ? WHERE id = ?",
                (sent.message_id, game_id)
            )
            db.commit()


async def bot_game_callback(update, context):
    query = update.callback_query
    user = query.from_user

    if not await require_captcha_and_membership(update, context):
        try:
            await query.answer("❌ ابتدا تأیید امنیتی و عضویت.", show_alert=True)
        except Exception:
            pass
        return

    try:
        game_id = int(query.data.replace("bot_", ""))
    except Exception:
        return

    with closing(get_db()) as db:
        try:
            db.execute("BEGIN IMMEDIATE")
            game = db.execute(
                "SELECT * FROM games WHERE id = ?", (game_id,)
            ).fetchone()

            if not game:
                db.rollback()
                await query.answer("❌ بازی پیدا نشد.", show_alert=True)
                return
            if game["status"] != "waiting":
                db.rollback()
                await query.answer("❌ بازی فعال نیست.", show_alert=True)
                return
            if game["creator_id"] != user.id:
                db.rollback()
                await query.answer("❌ فقط سازنده می‌تواند.", show_alert=True)
                return

            db.execute("""
                UPDATE games
                SET opponent_id = -1, mode = 'bot', status = 'creator_turn',
                    creator_rolls = 0, opponent_rolls = 0,
                    creator_total = 0, opponent_total = 0
                WHERE id = ?
            """, (game_id,))
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Bot game start failed")
            return

    await safe_delete_message(
        context.bot, query.message.chat_id, query.message.message_id
    )

    emoji, name = GAME_INFO[game["game_type"]]
    player_tag = user_tag(user.id)

    sent = await safe_send_message(
        context.bot, query.message.chat_id,
        "🤖 بازی با ربات شروع شد!\n\n"
        f"{emoji} {name}\n"
        f"🔢 تعداد پرتاب: {game['count']}\n"
        f"💰 شرط: {money(Decimal(game['bet']))} {CURRENCY}\n\n"
        f"🎯 نوبت {player_tag} است.\n\n"
        f"{emoji} را {game['count']} بار بفرستید.",
        parse_mode="HTML"
    )

    if sent:
        with closing(get_db()) as db:
            db.execute(
                "UPDATE games SET message_id = ? WHERE id = ?",
                (sent.message_id, game_id)
            )
            db.commit()


async def cancel_game_callback(update, context):
    query = update.callback_query
    user = query.from_user

    try:
        game_id = int(query.data.replace("cancel_", ""))
    except Exception:
        return

    with closing(get_db()) as db:
        try:
            db.execute("BEGIN IMMEDIATE")
            game = db.execute(
                "SELECT * FROM games WHERE id = ?", (game_id,)
            ).fetchone()

            if not game:
                db.rollback()
                await query.answer("❌ بازی پیدا نشد.", show_alert=True)
                return
            if game["status"] != "waiting":
                db.rollback()
                await query.answer("❌ بازی دیگر قابل لغو نیست.", show_alert=True)
                return
            if game["creator_id"] != user.id and not is_owner(user.id):
                db.rollback()
                await query.answer("❌ فقط سازنده یا مالک.", show_alert=True)
                return

            bet = Decimal(game["bet"])
            row = db.execute(
                "SELECT balance FROM users WHERE user_id = ?",
                (game["creator_id"],)
            ).fetchone()
            current = Decimal(row["balance"])
            new_balance = current + bet

            db.execute(
                "UPDATE users SET balance = ? WHERE user_id = ?",
                (str(new_balance), game["creator_id"])
            )
            db.execute("""
                INSERT INTO transactions(user_id, type, amount, description)
                VALUES (?, ?, ?, ?)
            """, (game["creator_id"], "game_refund",
                  str(bet), f"Cancel game {game_id}"))

            db.execute("""
                UPDATE games SET status = 'cancelled',
                    finished_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (game_id,))
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Cancel failed")
            await query.answer("❌ لغو انجام نشد.", show_alert=True)
            return

    try:
        await query.answer("بازی لغو شد.")
    except Exception:
        pass

    await safe_delete_message(
        context.bot, query.message.chat_id, query.message.message_id
    )

    await safe_send_message(
        context.bot, query.message.chat_id,
        "❌ بازی لغو شد.\n\n"
        f"💰 {money(bet)} {CURRENCY} به سازنده برگشت داده شد."
    )


# ============================================================
# USER DICE HANDLER
# ============================================================

def dice_matches_game(dice_emoji, game_type):
    return dice_emoji == GAME_EMOJI[game_type]


async def user_game_dice_handler(update, context):
    message = update.message
    user = update.effective_user
    chat = update.effective_chat

    if not message or not user:
        return
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if not is_game_chat(chat):
        return
    if not message.dice:
        return
    if is_banned(user.id):
        return

    game = get_active_game_for_user(chat.id, user.id)
    if not game:
        return
    if game["status"] not in ("creator_turn", "opponent_turn"):
        return

    game_type = game["game_type"]
    if not dice_matches_game(message.dice.emoji, game_type):
        return

    is_creator = (user.id == game["creator_id"])
    is_opponent = (game["opponent_id"] == user.id)

    if game["status"] == "creator_turn":
        if not is_creator:
            return

        new_rolls = 0
        count = int(game["count"])
        mode = game["mode"]

        with closing(get_db()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                fresh = db.execute(
                    "SELECT * FROM games WHERE id = ?", (game["id"],)
                ).fetchone()
                if not fresh:
                    db.rollback()
                    return

                rolls = int(fresh["creator_rolls"])
                if rolls >= int(fresh["count"]):
                    db.rollback()
                    return

                new_rolls = rolls + 1
                current_total = int(fresh["creator_total"])
                new_total = current_total + dice_to_points(
                    game_type, message.dice.value
                )

                if new_rolls >= int(fresh["count"]):
                    new_status = "bot_turn" if fresh["mode"] == "bot" else "opponent_turn"
                else:
                    new_status = "creator_turn"

                db.execute("""
                    UPDATE games
                    SET creator_rolls = ?, creator_total = ?, status = ?
                    WHERE id = ? AND status = 'creator_turn' AND creator_rolls = ?
                """, (new_rolls, new_total, new_status,
                      fresh["id"], rolls))
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("Creator dice processing failed")
                return

        if mode == "bot" and new_rolls >= count:
            bot_total = await bot_roll_game(context, game["id"])
            if bot_total is not None:
                await finish_game(context, game["id"])
            return

        if mode == "friends" and new_rolls >= count:
            fresh = get_game(game["id"])
            if not fresh:
                return
            opponent_id = fresh["opponent_id"]
            opponent_tag = user_tag(opponent_id)
            await safe_send_message(
                context.bot, chat.id,
                "✅ پرتاب‌های سازنده تمام شد.\n\n"
                f"🎯 حالا نوبت {opponent_tag} است.\n"
                f"{GAME_EMOJI[game_type]} را {game['count']} بار بفرستید.",
                parse_mode="HTML"
            )
        return

    if game["status"] == "opponent_turn":
        if not is_opponent:
            return

        new_rolls = 0
        count = int(game["count"])

        with closing(get_db()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                fresh = db.execute(
                    "SELECT * FROM games WHERE id = ?", (game["id"],)
                ).fetchone()
                if not fresh:
                    db.rollback()
                    return

                rolls = int(fresh["opponent_rolls"])
                if rolls >= int(fresh["count"]):
                    db.rollback()
                    return

                new_rolls = rolls + 1
                current_total = int(fresh["opponent_total"])
                new_total = current_total + dice_to_points(
                    game_type, message.dice.value
                )

                if new_rolls >= int(fresh["count"]):
                    new_status = "finishing"
                else:
                    new_status = "opponent_turn"

                db.execute("""
                    UPDATE games
                    SET opponent_rolls = ?, opponent_total = ?, status = ?
                    WHERE id = ? AND status = 'opponent_turn' AND opponent_rolls = ?
                """, (new_rolls, new_total, new_status,
                      fresh["id"], rolls))
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("Opponent dice processing failed")
                return

        if new_rolls >= count:
            await finish_game(context, game["id"])


# ============================================================
# BOT ROLLS
# ============================================================

async def bot_roll_game(context, game_id):
    game = get_game(game_id)
    if not game:
        return None
    if game["mode"] != "bot":
        return None
    if game["status"] != "bot_turn":
        return None

    total = 0
    for _ in range(int(game["count"])):
        fresh = get_game(game_id)
        if not fresh or fresh["status"] != "bot_turn":
            return None

        msg = await safe_send_dice(
            context.bot, game["chat_id"], GAME_EMOJI[game["game_type"]]
        )
        if not msg:
            await reset_game_internal(context, game_id, reason="bot_roll_failed")
            return None

        try:
            total += dice_to_points(game["game_type"], msg.dice.value)
        except Exception:
            pass

    with closing(get_db()) as db:
        try:
            db.execute("BEGIN IMMEDIATE")
            fresh = db.execute(
                "SELECT status FROM games WHERE id = ?", (game_id,)
            ).fetchone()
            if not fresh or fresh["status"] != "bot_turn":
                db.rollback()
                return None

            db.execute("""
                UPDATE games
                SET opponent_total = ?, opponent_rolls = ?, status = 'finishing'
                WHERE id = ? AND status = 'bot_turn'
            """, (total, int(game["count"]), game_id))
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Bot result save failed")
            return None
    return total


# ============================================================
# FINISH GAME
# ============================================================

async def finish_game(context, game_id):
    chat_id = None
    game_type = None
    creator_id = None
    opponent_id = None
    mode = None
    bet = Decimal("0")
    creator_total = 0
    opponent_total = 0
    result_type = None
    winner_id = None
    loser_id = None
    payout_value = Decimal("0")
    commission_value = Decimal("0")
    refund_value = Decimal("0")

    with closing(get_db()) as db:
        try:
            db.execute("BEGIN IMMEDIATE")
            game = db.execute(
                "SELECT * FROM games WHERE id = ?", (game_id,)
            ).fetchone()
            if not game:
                db.rollback()
                return
            if game["status"] != "finishing":
                db.rollback()
                return

            cursor = db.execute("""
                UPDATE games
                SET status = 'finished', finished_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'finishing'
            """, (game_id,))
            if cursor.rowcount != 1:
                db.rollback()
                return

            chat_id = game["chat_id"]
            game_type = game["game_type"]
            creator_id = int(game["creator_id"])
            opponent_id = game["opponent_id"]
            mode = game["mode"]
            bet = Decimal(game["bet"])
            creator_total = int(game["creator_total"])
            opponent_total = int(game["opponent_total"])

            pool = bet * 2
            payout = (bet * WIN_MULTIPLIER).quantize(Decimal("0.00000001"))
            commission = pool - payout

            def credit(uid, amt, ttype, desc):
                row = db.execute(
                    "SELECT balance FROM users WHERE user_id = ?", (uid,)
                ).fetchone()
                if not row:
                    db.execute("""
                        INSERT OR IGNORE INTO users(user_id, balance)
                        VALUES (?, '0')
                    """, (uid,))
                    row = db.execute(
                        "SELECT balance FROM users WHERE user_id = ?", (uid,)
                    ).fetchone()
                old_balance = Decimal(row["balance"])
                new_balance = old_balance + amt
                db.execute(
                    "UPDATE users SET balance = ? WHERE user_id = ?",
                    (str(new_balance), uid)
                )
                db.execute("""
                    INSERT INTO transactions(user_id, type, amount, description)
                    VALUES (?, ?, ?, ?)
                """, (uid, ttype, str(amt), desc))

            if mode == "bot":
                if creator_total == opponent_total:
                    credit(creator_id, bet, "game_draw_refund",
                           f"Bot draw {game_id}")
                    result_type = "bot_draw"
                    refund_value = bet
                elif creator_total > opponent_total:
                    credit(creator_id, payout, "game_win",
                           f"Bot game win {game_id}")
                    result_type = "bot_user_win"
                    winner_id = creator_id
                    payout_value = payout
                else:
                    result_type = "bot_bot_win"
            elif creator_total == opponent_total:
                credit(creator_id, bet, "game_draw_refund", f"Draw {game_id}")
                if opponent_id and int(opponent_id) > 0:
                    credit(int(opponent_id), bet, "game_draw_refund",
                           f"Draw {game_id}")
                result_type = "two_draw"
                refund_value = bet
            else:
                if creator_total > opponent_total:
                    winner = creator_id
                    loser = (int(opponent_id)
                             if opponent_id and int(opponent_id) > 0 else None)
                else:
                    winner = (int(opponent_id)
                              if opponent_id and int(opponent_id) > 0 else None)
                    loser = creator_id

                if winner is None:
                    db.rollback()
                    await reset_game_internal(
                        context, game_id, reason="winner_missing"
                    )
                    return

                credit(winner, payout, "game_win", f"Game win {game_id}")
                credit(OWNER_ID, commission, "commission",
                       f"Commission game {game_id}")

                result_type = "two_win"
                winner_id = winner
                loser_id = loser
                payout_value = payout
                commission_value = commission

            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Finish game failed")
            return

    emoji, name = GAME_INFO[game_type]
    creator_display = format_score(game_type, creator_total)
    opponent_display = format_score(game_type, opponent_total)
    creator_tag = user_tag(creator_id)

    if mode == "bot":
        opponent_tag = "🤖 ربات"
    else:
        opponent_tag = user_tag(int(opponent_id)) if opponent_id else "?"

    if result_type in ("bot_draw", "two_draw"):
        await safe_send_message(
            context.bot, chat_id,
            "🏁 <b>نتیجه بازی</b>\n\n"
            f"{emoji} {name}\n\n"
            f"👤 {creator_tag}: <b>{creator_display}</b>\n"
            f"👤 {opponent_tag}: <b>{opponent_display}</b>\n\n"
            "🤝 مساوی شد.\n"
            f"💰 {money(refund_value)} {CURRENCY} به هر نفر برگشت.",
            parse_mode="HTML"
        )
        await send_pv_result(
            context, creator_id, name,
            "🤝 مساوی شدی!",
            f"💰 {money(refund_value)} {CURRENCY} برگشت."
        )
        if mode == "friends" and opponent_id and int(opponent_id) > 0:
            await send_pv_result(
                context, int(opponent_id), name,
                "🤝 مساوی شدی!",
                f"💰 {money(refund_value)} {CURRENCY} برگشت."
            )
        return

    if result_type == "bot_bot_win":
        await safe_send_message(
            context.bot, chat_id,
            "🏁 <b>نتیجه بازی</b>\n\n"
            f"{emoji} {name}\n\n"
            f"👤 {creator_tag}: <b>{creator_display}</b>\n"
            f"👤 🤖 ربات: <b>{opponent_display}</b>\n\n"
            f"😔 {creator_tag} باخت.\n"
            f"💰 {money(bet)} {CURRENCY} از دست رفت.",
            parse_mode="HTML"
        )
        await send_pv_result(
            context, creator_id, name,
            "😔 باختی!",
            f"💰 {money(bet)} {CURRENCY} از دست رفت."
        )
        return

    if result_type == "bot_user_win":
        await safe_send_message(
            context.bot, chat_id,
            "🏁 <b>نتیجه بازی</b>\n\n"
            f"{emoji} {name}\n\n"
            f"👤 {creator_tag}: <b>{creator_display}</b>\n"
            f"👤 🤖 ربات: <b>{opponent_display}</b>\n\n"
            f"🏆 {creator_tag} برنده شد!\n"
            f"💰 جایزه: <b>{money(payout_value)} {CURRENCY}</b>",
            parse_mode="HTML"
        )
        await send_pv_result(
            context, creator_id, name,
            "🏆 برنده شدی!",
            f"💰 جایزه: <b>{money(payout_value)} {CURRENCY}</b>"
        )
        return

    winner_tag = user_tag(winner_id)
    loser_tag = user_tag(loser_id) if loser_id else "?"

    await safe_send_message(
        context.bot, chat_id,
        "🏁 <b>نتیجه بازی</b>\n\n"
        f"{emoji} {name}\n\n"
        f"👤 {creator_tag}: <b>{creator_display}</b>\n"
        f"👤 {opponent_tag}: <b>{opponent_display}</b>\n\n"
        f"🏆 {winner_tag} برنده شد!\n"
        f"😔 {loser_tag} باخت.\n\n"
        f"💰 جایزه برنده: <b>{money(payout_value)} {CURRENCY}</b>",
        parse_mode="HTML"
    )

    await send_pv_result(
        context, winner_id, name,
        "🏆 برنده شدی!",
        f"💰 جایزه: <b>{money(payout_value)} {CURRENCY}</b>"
    )
    if loser_id:
        await send_pv_result(
            context, loser_id, name,
            "😔 باختی!",
            f"💰 {money(bet)} {CURRENCY} از دست رفت."
        )


# ============================================================
# PV RESULT
# ============================================================

async def send_pv_result(context, user_id, game_name, header, detail):
    text = (
        f"{header}\n\n"
        f"🎮 بازی: {safe_name(game_name)}\n"
        f"{detail}\n\n"
        f"💰 موجودی: {money(get_balance(user_id))} {CURRENCY}"
    )
    try:
        await safe_send_message(
            context.bot, user_id, text,
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )
    except Exception:
        logger.warning("PV result failed for user %s", user_id)


# ============================================================
# RESET
# ============================================================

async def reset_game_internal(context, game_id, reason="manual"):
    with closing(get_db()) as db:
        try:
            db.execute("BEGIN IMMEDIATE")
            game = db.execute(
                "SELECT * FROM games WHERE id = ?", (game_id,)
            ).fetchone()
            if not game:
                db.rollback()
                return False
            if game["status"] in ("finished", "cancelled", "reset"):
                db.rollback()
                return False

            bet = Decimal(game["bet"])

            row = db.execute(
                "SELECT balance FROM users WHERE user_id = ?",
                (game["creator_id"],)
            ).fetchone()
            if row:
                balance = Decimal(row["balance"])
                db.execute(
                    "UPDATE users SET balance = ? WHERE user_id = ?",
                    (str(balance + bet), game["creator_id"])
                )
                db.execute("""
                    INSERT INTO transactions(user_id, type, amount, description)
                    VALUES (?, ?, ?, ?)
                """, (game["creator_id"], "game_reset_refund",
                      str(bet), f"Reset {game_id}: {reason}"))

            if (game["mode"] == "friends"
                    and game["opponent_id"]
                    and int(game["opponent_id"]) > 0):
                row = db.execute(
                    "SELECT balance FROM users WHERE user_id = ?",
                    (game["opponent_id"],)
                ).fetchone()
                if row:
                    balance = Decimal(row["balance"])
                    db.execute(
                        "UPDATE users SET balance = ? WHERE user_id = ?",
                        (str(balance + bet), game["opponent_id"])
                    )
                    db.execute("""
                        INSERT INTO transactions(user_id, type, amount, description)
                        VALUES (?, ?, ?, ?)
                    """, (game["opponent_id"], "game_reset_refund",
                          str(bet), f"Reset {game_id}: {reason}"))

            db.execute("""
                UPDATE games SET status = 'reset',
                    finished_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (game_id,))
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Reset game failed")
            return False
    return True


async def reset_game_command(update, context):
    user = update.effective_user
    chat = update.effective_chat

    if not is_owner(user.id):
        await update.message.reply_text("❌ فقط مالک می‌تواند ریست کند.")
        return

    args = context.args
    if args:
        try:
            game_id = int(normalize_digits(args[0]))
        except Exception:
            await update.message.reply_text(
                "❌ آیدی بازی صحیح نیست.\nمثال:\n/resetgame 25"
            )
            return
    else:
        with closing(get_db()) as db:
            row = db.execute("""
                SELECT id FROM games
                WHERE chat_id = ?
                  AND status NOT IN ('finished', 'cancelled', 'reset')
                ORDER BY id DESC LIMIT 1
            """, (chat.id,)).fetchone()
        if not row:
            await update.message.reply_text("❌ بازی فعالی در این گپ نیست.")
            return
        game_id = int(row["id"])

    success = await reset_game_internal(
        context, game_id, reason=f"Owner {user.id}"
    )
    if success:
        await update.message.reply_text(
            f"♻️ بازی ریست شد.\n🎮 Game ID: {game_id}"
        )
    else:
        await update.message.reply_text("❌ بازی پیدا نشد یا بسته شده.")


async def reset_all_games_command(update, context):
    user = update.effective_user
    if not is_owner(user.id):
        await update.message.reply_text("❌ فقط مالک.")
        return

    with closing(get_db()) as db:
        rows = db.execute("""
            SELECT id FROM games
            WHERE status NOT IN ('finished', 'cancelled', 'reset')
        """).fetchall()

    count = 0
    for row in rows:
        if await reset_game_internal(
            context, int(row["id"]), reason=f"Owner all {user.id}"
        ):
            count += 1

    await update.message.reply_text(
        f"♻️ ریست انجام شد.\n🎮 تعداد: {count}"
    )


# ============================================================
# CLEANUP
# ============================================================

async def cleanup_old_games(context):
    with closing(get_db()) as db:
        rows = db.execute("""
            SELECT id FROM games
            WHERE status IN (
                'waiting', 'creator_turn', 'opponent_turn',
                'bot_turn', 'finishing'
            )
            AND datetime(created_at) < datetime('now', ?)
        """, (f"-{GAME_TIMEOUT_MINUTES} minutes",)).fetchall()

    for row in rows:
        await reset_game_internal(
            context, int(row["id"]), reason="timeout"
        )


async def cleanup_job(context):
    try:
        await cleanup_old_games(context)
    except Exception:
        logger.exception("Cleanup job failed")


# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(update, context):
    message = update.message
    user = update.effective_user

    if not message or not user:
        return
    if not message.text:
        return

    ensure_user(user)

    if not await require_captcha_and_membership(update, context):
        return

    raw_text = message.text.strip()
    text = normalize_digits(raw_text).strip()

    # WITHDRAW FLOW
    step = context.user_data.get("withdraw_step")

    if step == "amount":
        if text == "لغو" or raw_text == "لغو":
            context.user_data.pop("withdraw_step", None)
            context.user_data.pop("withdraw_amount", None)
            await message.reply_text(
                "❌ برداشت لغو شد.",
                reply_markup=main_keyboard()
            )
            return
        await handle_withdraw_amount(update, context, text)
        return

    if step == "wallet":
        if text == "لغو" or raw_text == "لغو":
            context.user_data.pop("withdraw_step", None)
            context.user_data.pop("withdraw_amount", None)
            await message.reply_text(
                "❌ برداشت لغو شد.",
                reply_markup=main_keyboard()
            )
            return
        await handle_withdraw_wallet(update, context, text)
        return

    # ADMIN INPUT
    if is_owner(user.id):
        handled = await admin_balance_by_id(update, context)
        if handled:
            return

    # ADMIN REPLY-BASED
    if is_owner(user.id):
        m = re.match(r"^شارژ\s+([0-9]+(?:\.[0-9]+)?)$", text)
        if m:
            amount = parse_amount(m.group(1))
            if amount is None:
                await message.reply_text("❌ مبلغ صحیح نیست.")
                return
            await admin_change_by_reply(update, context, "charge", amount)
            return

        m = re.match(r"^کسر\s+([0-9]+(?:\.[0-9]+)?)$", text)
        if m:
            amount = parse_amount(m.group(1))
            if amount is None:
                await message.reply_text("❌ مبلغ صحیح نیست.")
                return
            await admin_change_by_reply(update, context, "remove", amount)
            return

    # ADMIN BUTTONS (PV only)
    if is_owner(user.id) and message.chat.type == ChatType.PRIVATE:
        if raw_text == "📊 آمار ربات" or raw_text == "📊 آمار":
            await send_admin_stats(update, context)
            return
        if raw_text == "🟢 روشن":
            set_bot_enabled(True)
            await message.reply_text(
                "🟢 ربات روشن شد.",
                reply_markup=admin_reply_keyboard()
            )
            return
        if raw_text == "🔴 خاموش":
            set_bot_enabled(False)
            await message.reply_text(
                "🔴 ربات خاموش شد.",
                reply_markup=admin_reply_keyboard()
            )
            return
        if raw_text == "➕ شارژ کاربر" or raw_text == "➕ شارژ":
            await ask_admin_balance(update, context, "charge")
            return
        if raw_text == "➖ کسر کاربر" or raw_text == "➖ کسر":
            await ask_admin_balance(update, context, "remove")
            return
        if raw_text == "🏧 برداشت‌های در انتظار":
            await send_admin_withdrawals(update, context)
            return
        if raw_text == "💰 موجودی کاربران":
            await send_admin_balances(update, context)
            return
        if raw_text == "🚫 بن کاربر":
            await message.reply_text(
                "برای بن کاربر:\n"
                "• روی پیام کاربر ریپلای کنید و بنویسید:\n"
                "<code>/ban</code>\n"
                "• یا بنویسید:\n"
                "<code>/ban 123456789</code>",
                parse_mode="HTML"
            )
            return
        if raw_text == "✅ آنبن کاربر":
            await message.reply_text(
                "برای آنبن:\n"
                "• روی پیام کاربر ریپلای کنید و بنویسید:\n"
                "<code>/unban</code>\n"
                "• یا بنویسید:\n"
                "<code>/unban 123456789</code>",
                parse_mode="HTML"
            )
            return
        if raw_text == "➕ افزایش پاداش":
            new_reward = get_referral_reward() + Decimal("0.01")
            set_referral_reward(new_reward)
            await message.reply_text(
                f"✅ پاداش افزایش یافت: {money(new_reward)} {CURRENCY}",
                reply_markup=admin_reply_keyboard()
            )
            return
        if raw_text.startswith("➖ کاهش پاداش"):
            new_reward = get_referral_reward() - Decimal("0.01")
            if new_reward < Decimal("0"):
                new_reward = Decimal("0")
            set_referral_reward(new_reward)
            await message.reply_text(
                f"✅ پاداش کاهش یافت: {money(new_reward)} {CURRENCY}",
                reply_markup=admin_reply_keyboard()
            )
            return
        if raw_text == "🏠 منوی اصلی" or raw_text == "منوی اصلی":
            await send_start_menu(context, message.chat.id, user.id)
            return

    # Admin buttons in group
    if is_owner(user.id) and message.chat.type != ChatType.PRIVATE:
        admin_buttons = [
            "📊 آمار ربات", "📊 آمار", "🟢 روشن", "🔴 خاموش",
            "➕ شارژ کاربر", "➕ شارژ", "➖ کسر کاربر", "➖ کسر",
            "🏧 برداشت‌های در انتظار", "💰 موجودی کاربران",
            "🚫 بن کاربر", "✅ آنبن کاربر",
            "➕ افزایش پاداش", "🏠 منوی اصلی"
        ]
        if raw_text in admin_buttons or raw_text.startswith("➖ کاهش پاداش"):
            await message.reply_text(
                "❌ پنل مدیریت فقط در پیوی کار می‌کند.",
                reply_markup=ReplyKeyboardRemove()
            )
            return

    # USER BUTTONS
    if raw_text in ("💰 موجودی", "موجودی", "م", "موجودی من", "balance"):
        await show_balance(update, context)
        return
    if raw_text in ("👥 زیرمجموعه", "زیرمجموعه", "زیر مجموعه", "رفرال", "referral"):
        await show_referral(update, context)
        return
    if raw_text in ("🏧 برداشت TRX", "برداشت"):
        await show_withdraw_info(update, context)
        return
    if raw_text in ("🆔 آیدی من", "آیدی", "ایدی", "myid", "id"):
        await show_my_id(update, context)
        return
    if raw_text in ("📚 راهنما", "راهنما", "کمک", "help"):
        await show_help(update, context)
        return

    # TRANSFER
    if text.startswith("انتقال"):
        match_id = re.match(
            r"^انتقال\s+([0-9]+(?:\.[0-9]+)?)\s+(\d{5,15})$", text
        )
        match_reply = re.match(
            r"^انتقال\s+([0-9]+(?:\.[0-9]+)?)$", text
        )

        if match_id:
            amount = parse_amount(match_id.group(1))
            target_id = int(match_id.group(2))
            if amount is None:
                await message.reply_text("❌ مبلغ صحیح نیست.")
                return
            await transfer_by_id(update, context, amount, target_id)
            return

        if match_reply:
            amount = parse_amount(match_reply.group(1))
            if amount is None:
                await message.reply_text("❌ مبلغ صحیح نیست.")
                return
            await transfer(update, context, amount)
            return

        await message.reply_text(
            "❌ فرمت صحیح:\n\n"
            "1️⃣ با ریپلای:\n<code>انتقال 0.5</code>\n\n"
            "2️⃣ با آیدی عددی:\n<code>انتقال 0.5 123456789</code>",
            parse_mode="HTML"
        )
        return

    # GAME
    game = parse_game(text)

    if game == "count_too_high":
        await message.reply_text(
            f"❌ حداکثر تعداد پرتاب {MAX_GAME_COUNT} است.\n"
            f"مثال:\n3 تاس 0.5"
        )
        return

    if game:
        if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
            await message.reply_text("❌ بازی‌ها فقط داخل گپ انجام می‌شوند.")
            return
        if not is_game_chat(message.chat):
            await message.reply_text(
                f"❌ بازی فقط در گپ زیر مجاز است:\n{GROUP_URL}"
            )
            return
        count, game_type, bet = game
        await create_game(update, context, count, game_type, bet)
        return

    return


# ============================================================
# COMMANDS
# ============================================================

async def admin_command(update, context):
    user = update.effective_user
    if not is_owner(user.id):
        await update.message.reply_text("❌ دسترسی ندارید.")
        return
    if update.message.chat.type != ChatType.PRIVATE:
        await update.message.reply_text("❌ پنل مدیریت فقط در پیوی در دسترسه.")
        return
    await admin_panel(update, context)


async def balance_command(update, context):
    await show_balance(update, context)


async def referral_command(update, context):
    await show_referral(update, context)


async def help_command(update, context):
    await show_help(update, context)


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(update, context):
    error = context.error
    if isinstance(error, (TimedOut, NetworkError)):
        logger.warning("Temporary Telegram error: %s", error)
        return
    logger.exception("Unhandled exception", exc_info=error)


# ============================================================
# MAIN
# ============================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN تنظیم نشده است.")

    init_db()
    auto_ban_invalid_users()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )

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

    application.add_handler(CallbackQueryHandler(
        membership_callback, pattern=r"^check_membership$"
    ))
    application.add_handler(CallbackQueryHandler(
        captcha_callback, pattern=r"^cap_\d+_\d+$"
    ))
    application.add_handler(CallbackQueryHandler(
        withdraw_callback, pattern=r"^wd_(ok|no)_\d+$"
    ))
    application.add_handler(CallbackQueryHandler(
        wd_proof_callback, pattern=r"^wd_proof_\d+$"
    ))
    application.add_handler(CallbackQueryHandler(
        join_game_callback, pattern=r"^join_\d+$"
    ))
    application.add_handler(CallbackQueryHandler(
        bot_game_callback, pattern=r"^bot_\d+$"
    ))
    application.add_handler(CallbackQueryHandler(
        cancel_game_callback, pattern=r"^cancel_\d+$"
    ))

    application.add_handler(MessageHandler(
        filters.Dice.ALL,
        user_game_dice_handler
    ))

    application.add_handler(MessageHandler(
        (filters.PHOTO | filters.VOICE | filters.VIDEO
         | filters.Document.ALL | filters.AUDIO)
        & filters.ChatType.PRIVATE,
        owner_proof_handler
    ))

    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        text_handler
    ))

    application.add_error_handler(error_handler)

    if application.job_queue:
        application.job_queue.run_repeating(
            cleanup_job, interval=300, first=60
        )

    logger.info("BET_BT started successfully.")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False
    )


if __name__ == "__main__":
    main()
