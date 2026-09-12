# -*- coding: utf-8 -*-
import os
import asyncio
import logging
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta, timezone
from html import escape

from aiohttp import web
from aiogram import Bot, Dispatcher, Router, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from typing import Any, Awaitable, Callable

from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, ReplyKeyboardMarkup,
    KeyboardButton, BotCommand, BotCommandScopeChat, Update,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN Environment Variable topilmadi.")

SUPERADMIN_USERNAME = "omono_v"
SUPERADMIN_ID_ENV = os.getenv("SUPERADMIN_ID", "").strip()
SUPERADMIN_ID = int(SUPERADMIN_ID_ENV) if SUPERADMIN_ID_ENV.isdigit() else None
DEFAULT_CHANNEL = "@uz_kinocinema"
INSTAGRAM_URL = "https://www.instagram.com/oemovie/"
BOT_USERNAME = "kinocinemauz_bot"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
PORT = int(os.getenv("PORT", "10000"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("KinoCinema")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL Environment Variable topilmadi. Neon/PostgreSQL ulanish URL sini Render Environment Variables ga qo\'ying.")

class Database:
    """Small PostgreSQL wrapper with automatic rollback/reconnect safety."""
    def __init__(self, url):
        self.url = url
        self.conn = None
        self._connect()

    def _connect(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
        self.conn = psycopg2.connect(
            self.url,
            cursor_factory=RealDictCursor,
            sslmode="require",
            connect_timeout=15,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=3,
        )

    def execute(self, sql, params=()):
        sql = sql.replace("?", "%s")
        try:
            cur = self.conn.cursor()
            cur.execute(sql, params)
            return cur
        except (psycopg2.InterfaceError, psycopg2.OperationalError):
            # Neon/Render can drop an idle connection. Reconnect once.
            try:
                self._connect()
                cur = self.conn.cursor()
                cur.execute(sql, params)
                return cur
            except psycopg2.Error:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                raise
        except psycopg2.Error:
            # PostgreSQL marks the transaction aborted after a SQL error.
            # Roll it back immediately so the next Telegram update can run.
            try:
                self.conn.rollback()
            except Exception:
                pass
            raise

    def executescript(self, sql):
        try:
            cur = self.conn.cursor()
            cur.execute(sql)
            return cur
        except psycopg2.Error:
            try:
                self.conn.rollback()
            except Exception:
                pass
            raise

    def commit(self):
        try:
            self.conn.commit()
        except (psycopg2.InterfaceError, psycopg2.OperationalError):
            # Do not silently claim a failed commit succeeded. Re-raise.
            raise

    def rollback(self):
        try:
            self.conn.rollback()
        except Exception:
            pass

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


DB = Database(DATABASE_URL)


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def init_db():
    DB.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        prime_until TEXT,
        referred_by BIGINT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS admins (
        user_id BIGINT PRIMARY KEY,
        username TEXT,
        added_by BIGINT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS channels (
        id SERIAL PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        title TEXT
    );
    CREATE TABLE IF NOT EXISTS cards (
        admin_id BIGINT PRIMARY KEY,
        card_number TEXT NOT NULL,
        card_owner TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS movies (
        id SERIAL PRIMARY KEY,
        code TEXT UNIQUE NOT NULL,
        title TEXT NOT NULL,
        file_id TEXT,
        link_url TEXT,
        prime_only INTEGER NOT NULL DEFAULT 0,
        views INTEGER NOT NULL DEFAULT 0,
        added_by BIGINT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS payments (
        id SERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        admin_id BIGINT NOT NULL,
        plan TEXT NOT NULL,
        days INTEGER NOT NULL,
        price INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        screenshot_file_id TEXT,
        created_at TEXT NOT NULL,
        decided_at TEXT
    );
    CREATE TABLE IF NOT EXISTS referrals (
        user_id BIGINT PRIMARY KEY,
        admin_id BIGINT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS orders (
        id SERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        admin_id BIGINT NOT NULL,
        text TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """)

    # Existing Neon tables may have been created by an earlier version using
    # PostgreSQL INTEGER. Telegram IDs can exceed INTEGER's 32-bit range, so
    # migrate every Telegram-ID column to BIGINT. These ALTER statements are
    # safe to run on every startup and preserve existing rows.
    telegram_id_columns = [
        ("users", "user_id"),
        ("users", "referred_by"),
        ("admins", "user_id"),
        ("admins", "added_by"),
        ("cards", "admin_id"),
        ("movies", "added_by"),
        ("payments", "user_id"),
        ("payments", "admin_id"),
        ("referrals", "user_id"),
        ("referrals", "admin_id"),
        ("orders", "user_id"),
        ("orders", "admin_id"),
    ]
    for table, column in telegram_id_columns:
        DB.execute(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE BIGINT')

    # Movie files are optional now: a movie/serial can be delivered by a
    # Telegram/private-channel link instead of a Telegram video file.
    DB.execute("ALTER TABLE movies ALTER COLUMN file_id DROP NOT NULL")
    DB.execute("ALTER TABLE movies ADD COLUMN IF NOT EXISTS link_url TEXT")

    DB.execute("INSERT INTO channels(username, title) VALUES(?, ?) ON CONFLICT(username) DO NOTHING", (DEFAULT_CHANNEL, "Uz KinoCinema"))
    DB.commit()


def upsert_user(user):
    DB.execute("""
        INSERT INTO users(user_id, username, first_name, created_at)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name
    """, (user.id, user.username, user.first_name, now()))
    DB.commit()


def get_user(user_id):
    return DB.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def get_superadmin_id():
    global SUPERADMIN_ID
    if SUPERADMIN_ID:
        return SUPERADMIN_ID
    row = DB.execute("SELECT user_id FROM users WHERE lower(COALESCE(username,''))=?", (SUPERADMIN_USERNAME,)).fetchone()
    if row:
        SUPERADMIN_ID = row["user_id"]
        return SUPERADMIN_ID
    row = DB.execute("SELECT user_id FROM admins WHERE lower(COALESCE(username,''))=?", (SUPERADMIN_USERNAME,)).fetchone()
    if row:
        SUPERADMIN_ID = row["user_id"]
        return SUPERADMIN_ID
    return None


def is_superadmin_user(user):
    if SUPERADMIN_ID and user.id == SUPERADMIN_ID:
        return True
    return (user.username or "").lstrip("@").lower() == SUPERADMIN_USERNAME


def is_superadmin_id(user_id):
    if SUPERADMIN_ID and user_id == SUPERADMIN_ID:
        return True
    row = DB.execute("SELECT username FROM users WHERE user_id=?", (user_id,)).fetchone()
    return bool(row and (row["username"] or "").lstrip("@").lower() == SUPERADMIN_USERNAME)


def ensure_superadmin(user):
    global SUPERADMIN_ID
    if not is_superadmin_user(user):
        return False
    SUPERADMIN_ID = user.id
    upsert_user(user)
    DB.execute("""
        INSERT INTO admins(user_id, username, added_by, created_at)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET username=excluded.username
    """, (user.id, user.username or SUPERADMIN_USERNAME, user.id, now()))
    DB.commit()
    return True


def is_admin_id(user_id):
    if is_superadmin_id(user_id):
        return True
    return DB.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)).fetchone() is not None


def is_admin_user(user):
    return is_superadmin_user(user) or is_admin_id(user.id)


def get_referral_admin(user_id):
    row = DB.execute("SELECT admin_id FROM referrals WHERE user_id=?", (user_id,)).fetchone()
    return row["admin_id"] if row else None


def set_referral_once(user_id, admin_id):
    if user_id == admin_id or not is_admin_id(admin_id):
        return
    DB.execute("INSERT INTO referrals(user_id, admin_id, created_at) VALUES(?, ?, ?) ON CONFLICT(user_id) DO NOTHING", (user_id, admin_id, now()))
    DB.execute("UPDATE users SET referred_by=? WHERE user_id=? AND referred_by IS NULL", (admin_id, user_id))
    DB.commit()


def get_admin_card(admin_id):
    return DB.execute("SELECT * FROM cards WHERE admin_id=?", (admin_id,)).fetchone()


def active_prime(user_id):
    row = get_user(user_id)
    if not row or not row["prime_until"]:
        return False
    try:
        return datetime.fromisoformat(row["prime_until"]) > datetime.now(timezone.utc).replace(tzinfo=None)
    except ValueError:
        return False


def prime_until_text(value):
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return value


def main_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🔎 Kino qidirish"), KeyboardButton(text="⭐ Prime status")],
        [KeyboardButton(text="📚 Kinolar ro'yxati")],
        [KeyboardButton(text="📸 Instagramga qaytish")],
        [KeyboardButton(text="🎬 Kino buyurtma qilish")],
        [KeyboardButton(text="🤝 Reklama & Bot olish")],
    ], resize_keyboard=True)


def back_cancel():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⬅️ Orqaga"), KeyboardButton(text="❌ Bekor qilish")]], resize_keyboard=True)

def movie_media_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="⏭ O'tkazib yuborish")],
        [KeyboardButton(text="⬅️ Orqaga"), KeyboardButton(text="❌ Bekor qilish")],
    ], resize_keyboard=True)


def movie_management_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="➕ Kino qo'shish"), KeyboardButton(text="✏️ Kino tahrirlash")],
        [KeyboardButton(text="🗑 Kino o'chirish"), KeyboardButton(text="📚 Kinolar")],
        [KeyboardButton(text="⬅️ Admin panel")],
    ], resize_keyboard=True)


def admin_menu(user):
    rows = [
        [KeyboardButton(text="🎬 Kino boshqaruvi")],
        [KeyboardButton(text="📊 Statistika"), KeyboardButton(text="💳 Karta sozlamalari")],
        [KeyboardButton(text="📢 Kanallar"), KeyboardButton(text="🔗 Mening referralim")],
    ]
    if is_superadmin_user(user):
        rows.append([KeyboardButton(text="👥 Adminlar")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def movie_edit_keyboard():
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="✏️ Kod", callback_data="movie_edit:code"),
        InlineKeyboardButton(text="📝 Nomi", callback_data="movie_edit:title"),
    )
    b.row(
        InlineKeyboardButton(text="🎬 Video", callback_data="movie_edit:video"),
        InlineKeyboardButton(text="🔗 Link", callback_data="movie_edit:link"),
    )
    b.row(InlineKeyboardButton(text="⭐ Kino turi", callback_data="movie_edit:type"))
    b.row(InlineKeyboardButton(text="⬅️ Kino boshqaruvi", callback_data="movie_edit:back"))
    return b.as_markup()


def movie_delete_confirm_keyboard(movie_id):
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="✅ Ha, o'chirish", callback_data=f"movie_delete:yes:{movie_id}"),
        InlineKeyboardButton(text="❌ Yo'q", callback_data="movie_delete:no"),
    )
    return b.as_markup()


def card_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="➕ Karta qo'shish"), KeyboardButton(text="🔄 Kartani almashtirish")],
        [KeyboardButton(text="👀 Hozirgi kartani ko'rish"), KeyboardButton(text="🗑 Kartani o'chirish")],
        [KeyboardButton(text="⬅️ Admin panel")],
    ], resize_keyboard=True)


def admin_manage_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="➕ Admin qo'shish"), KeyboardButton(text="🗑 Admin o'chirish")],
        [KeyboardButton(text="📋 Adminlar"), KeyboardButton(text="⬅️ Admin panel")],
    ], resize_keyboard=True)


def channel_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="➕ Kanal qo'shish"), KeyboardButton(text="🗑 Kanal o'chirish")],
        [KeyboardButton(text="📋 Kanallar"), KeyboardButton(text="⬅️ Admin panel")],
    ], resize_keyboard=True)


def subscription_keyboard():
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="📢 Kanalga o'tish", url="https://t.me/uz_kinocinema"))
    b.row(InlineKeyboardButton(text="✅ Obuna bo'ldim", callback_data="subscription_check"))
    return b.as_markup()


def prime_keyboard():
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="7 kun — 7 000 so'm", callback_data="prime:7:7000:7"))
    b.row(InlineKeyboardButton(text="1 oy — 20 000 so'm", callback_data="prime:1oy:20000:30"))
    b.row(InlineKeyboardButton(text="3 oy — 50 000 so'm", callback_data="prime:3oy:50000:90"))
    b.row(InlineKeyboardButton(text="Umrbod — 150 000 so'm", callback_data="prime:lifetime:150000:0"))
    return b.as_markup()


def movie_type_keyboard():
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🆓 Oddiy kino", callback_data="movie_type:0"), InlineKeyboardButton(text="⭐ Faqat Prime", callback_data="movie_type:1"))
    return b.as_markup()


def payment_keyboard(payment_id):
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="✅", callback_data=f"pay:approve:{payment_id}"), InlineKeyboardButton(text="❌", callback_data=f"pay:reject:{payment_id}"))
    return b.as_markup()


class SearchState(StatesGroup):
    code = State()
class CardState(StatesGroup):
    number = State()
    owner = State()
class MovieAddState(StatesGroup):
    code = State()
    title = State()
    video = State()
    link = State()
    type = State()
class MovieDeleteState(StatesGroup):
    code = State()
    confirm = State()


class MovieEditState(StatesGroup):
    code = State()
    menu = State()
    edit_code = State()
    title = State()
    video = State()
    link = State()
    type = State()

class AdminAddState(StatesGroup):
    user = State()
class AdminDeleteState(StatesGroup):
    user = State()
class ChannelAddState(StatesGroup):
    username = State()
class ChannelDeleteState(StatesGroup):
    username = State()
class PaymentState(StatesGroup):
    screenshot = State()
class OrderState(StatesGroup):
    text = State()


async def subscribed(bot, user_id):
    if is_admin_id(user_id):
        return True
    rows = DB.execute("SELECT username FROM channels ORDER BY id").fetchall()
    for row in rows:
        try:
            member = await bot.get_chat_member(row["username"], user_id)
            if member.status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
                return False
            if member.status == ChatMemberStatus.RESTRICTED and not getattr(member, "is_member", False):
                return False
        except Exception:
            logger.exception("Kanal obunasini tekshirish xatosi")
            return False
    return True


async def set_commands_for_user(bot, user_id):
    commands = [BotCommand(command="start", description="User panel")]
    if is_admin_id(user_id):
        commands.insert(0, BotCommand(command="admin", description="Admin panel"))
    try:
        await bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id=user_id))
    except Exception:
        logger.exception("Command menyusini o'rnatishda xato")


async def send_main(message):
    await message.answer("🎬 KinoCinema botiga xush kelibsiz!\n\nKerakli bo'limni tanlang:", reply_markup=main_menu())


async def send_admin(message):
    await message.answer("👨‍💻 Admin panel", reply_markup=admin_menu(message.from_user))


class SubscriptionMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable[[Any, dict[str, Any]], Awaitable[Any]], event: Any, data: dict[str, Any]):
        user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)
        bot = data["bot"]
        ensure_superadmin(user)

        if isinstance(event, Message):
            text = event.text or ""
            if text.startswith("/start") or text.startswith("/admin"):
                return await handler(event, data)
            if text in {"⬅️ Orqaga", "❌ Bekor qilish"}:
                return await handler(event, data)
        if isinstance(event, CallbackQuery) and event.data == "subscription_check":
            return await handler(event, data)
        if is_admin_user(user):
            return await handler(event, data)
        if not await subscribed(bot, user.id):
            if isinstance(event, CallbackQuery):
                await event.answer("🔒 Avval majburiy kanalga obuna bo'ling.", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("🔒 Avval majburiy kanalga obuna bo'ling.", reply_markup=subscription_keyboard())
            return
        return await handler(event, data)


class ActiveStateLockMiddleware(BaseMiddleware):
    """
    Bir FSM jarayoni tugamaguncha foydalanuvchi boshqa menyu/buyruqqa
    sakray olmaydi. Faqat joriy bosqichdagi input va Orqaga/Bekor qilish
    ishlaydi. /start va /admin ham jarayon tugamaguncha bloklanadi.
    """

    MENU_TEXTS = {
        "🔎 Kino qidirish", "⭐ Prime status", "📚 Kinolar ro'yxati",
        "📸 Instagramga qaytish", "🎬 Kino buyurtma qilish",
        "🤝 Reklama & Bot olish", "🎬 Kino boshqaruvi",
        "➕ Kino qo'shish", "✏️ Kino tahrirlash", "🗑 Kino o'chirish",
        "📚 Kinolar", "📊 Statistika", "💳 Karta sozlamalari",
        "📢 Kanallar", "🔗 Mening referralim", "👥 Adminlar",
        "➕ Admin qo'shish", "🗑 Admin o'chirish", "📋 Adminlar",
        "➕ Karta qo'shish", "🔄 Kartani almashtirish",
        "👀 Hozirgi kartani ko'rish", "🗑 Kartani o'chirish",
        "➕ Kanal qo'shish", "🗑 Kanal o'chirish", "📋 Kanallar",
        "⬅️ Admin panel",
    }

    async def __call__(self, handler, event, data):
        state = data.get("state")
        if state is None:
            return await handler(event, data)

        current = await state.get_state()
        if not current:
            return await handler(event, data)

        if isinstance(event, CallbackQuery):
            # FSM davomida boshqa inline tugmalar ham boshqa buyruqqa
            # olib o'tmasin. Joriy kino turi tugmalari bundan mustasno.
            if current == MovieAddState.type.state and (event.data or "").startswith("movie_type:"):
                return await handler(event, data)
            if current in {MovieEditState.menu.state, MovieEditState.link.state, MovieEditState.type.state, MovieDeleteState.confirm.state} and (event.data or "").startswith(("movie_edit:", "movie_delete:")):
                return await handler(event, data)
            await event.answer("⏳ Avval joriy jarayonni tugating yoki ❌ Bekor qilishni bosing.", show_alert=True)
            return

        if isinstance(event, Message):
            text = (event.text or "").strip()
            if text in {"⬅️ Orqaga", "❌ Bekor qilish"}:
                return await handler(event, data)
            if text.startswith("/") or text in self.MENU_TEXTS:
                await event.answer("⏳ Avval joriy jarayonni tugating yoki ❌ Bekor qilishni bosing.")
                return

        return await handler(event, data)


router = Router()
router.message.outer_middleware(SubscriptionMiddleware())
router.callback_query.outer_middleware(SubscriptionMiddleware())
router.message.outer_middleware(ActiveStateLockMiddleware())
router.callback_query.outer_middleware(ActiveStateLockMiddleware())

# =========================
# COMMANDS: faqat komandalar
# =========================
@router.message(CommandStart())
async def start_cmd(message: Message, state: FSMContext, bot: Bot):
    upsert_user(message.from_user)
    ensure_superadmin(message.from_user)
    await set_commands_for_user(bot, message.from_user.id)
    await state.clear()

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].startswith("ref_"):
        try:
            ref_id = int(parts[1][4:])
            set_referral_once(message.from_user.id, ref_id)
        except ValueError:
            pass

    if not is_admin_user(message.from_user) and not await subscribed(bot, message.from_user.id):
        await message.answer("🔒 Avval majburiy kanalga obuna bo'ling.", reply_markup=subscription_keyboard())
        return
    await send_main(message)


@router.message(Command("admin"))
async def admin_cmd(message: Message, state: FSMContext, bot: Bot):
    upsert_user(message.from_user)
    ensure_superadmin(message.from_user)
    await state.clear()
    await set_commands_for_user(bot, message.from_user.id)
    if not is_admin_user(message.from_user):
        await message.answer("❌ Siz admin emassiz.")
        return
    await send_admin(message)


@router.callback_query(F.data == "subscription_check")
async def subscription_check(callback: CallbackQuery, bot: Bot, state: FSMContext):
    if await subscribed(bot, callback.from_user.id):
        await state.clear()
        await callback.answer("✅ Obuna tasdiqlandi!", show_alert=True)
        await callback.message.answer("🎬 KinoCinema botiga xush kelibsiz!\n\nKerakli bo'limni tanlang:", reply_markup=main_menu())
    else:
        await callback.answer("❌ Hali kanalga obuna bo'lmagansiz.", show_alert=True)

async def deliver_movie(message: Message, movie):
    """Deliver a movie either as Telegram video or as a viewing link."""
    title = escape(movie["title"])
    code = escape(movie["code"])
    link = (movie.get("link_url") if hasattr(movie, "get") else None) or ""
    file_id = (movie.get("file_id") if hasattr(movie, "get") else None) or ""

    if link:
        await message.answer(
            f"🎬 {title}\n🔢 Kod: {code}\n\n🔗 To'liq tomosha qilish uchun:\n{escape(link)}\n\n🍿 Yoqimli tomosha!"
        )
        return

    if file_id:
        await message.answer_video(
            file_id,
            caption=f"🎬 {title}\n🔢 Kod: {code}\n\n🍿 Yoqimli tomosha!"
        )
        return

    raise ValueError("Kino uchun video ham, link ham saqlanmagan")


# =========================
# GLOBAL FSM SAFETY
# This is intentionally BEFORE all no-state fallbacks,
# but each FSM handler has an explicit state filter.
# =========================

@router.message(SearchState.code, F.text, ~F.text.in_({"⬅️ Orqaga", "❌ Bekor qilish"}))
async def search_code(message: Message, state: FSMContext):
    query = message.text.strip()
    if not query:
        await message.answer("❌ Kino kodi yoki kino nomini yuboring.")
        return

    movie = None
    # Kino kodi faqat 3 xonali raqam. Qidiruv esa so'z va sonlarni ham qabul qiladi.
    if query.isdigit() and len(query) == 3:
        movie = DB.execute("SELECT * FROM movies WHERE code=?", (query,)).fetchone()
    elif not query.isdigit():
        movie = DB.execute(
            "SELECT * FROM movies WHERE title ILIKE ? ORDER BY id DESC LIMIT 1",
            (f"%{query}%",)
        ).fetchone()
    else:
        # 3 xonali bo'lmagan son kino kodi emas. Uni nom ichidan qidiramiz.
        movie = DB.execute(
            "SELECT * FROM movies WHERE title ILIKE ? ORDER BY id DESC LIMIT 1",
            (f"%{query}%",)
        ).fetchone()

    if not movie:
        await message.answer("❌ Bunday kino topilmadi. Qayta urinib ko'ring.")
        return
    if movie["prime_only"] and not active_prime(message.from_user.id) and not is_admin_id(message.from_user.id):
        await message.answer("⭐ Bu kino faqat Prime uchun.")
        return
    DB.execute("UPDATE movies SET views=views+1 WHERE id=?", (movie["id"],))
    DB.commit()
    try:
        await deliver_movie(message, movie)
    except Exception:
        logger.exception("Kino yuborishda xato")
        await message.answer("❌ Kino/linkni yuborishda xatolik yuz berdi.")
    await state.clear()


@router.message(MovieAddState.code, F.text, ~F.text.in_({"⬅️ Orqaga", "❌ Bekor qilish"}))
async def movie_add_code(message: Message, state: FSMContext):
    code = message.text.strip()
    if not code.isdigit() or len(code) != 3:
        await message.answer("❌ Kino kodi aynan 3 xonali raqam bo'lishi kerak. Masalan: 327")
        return
    if DB.execute("SELECT 1 FROM movies WHERE code=?", (code,)).fetchone():
        await message.answer("❌ Bu koddagi kino allaqachon mavjud.")
        return
    await state.update_data(code=code)
    await state.set_state(MovieAddState.title)
    await message.answer("🎬 Kino qo'shish — 2/5\n\nKino nomini yuboring.", reply_markup=back_cancel())


@router.message(MovieAddState.title, F.text, ~F.text.in_({"⬅️ Orqaga", "❌ Bekor qilish"}))
async def movie_add_title(message: Message, state: FSMContext):
    title = message.text.strip()
    if not title:
        await message.answer("❌ Kino nomi bo'sh bo'lmasin.")
        return
    await state.update_data(title=title)
    await state.set_state(MovieAddState.video)
    await message.answer(
        "🎬 Kino qo'shish — 3/5\n\n"
        "VIDEO yuboring.\n"
        "⚠️ Video majburiy. Link keyingi bosqichda ixtiyoriy bo'ladi.",
        reply_markup=back_cancel()
    )


@router.message(MovieAddState.video, F.video)
async def movie_add_video(message: Message, state: FSMContext):
    await state.update_data(file_id=message.video.file_id)
    await state.set_state(MovieAddState.link)
    await message.answer(
        "🎬 Kino qo'shish — 4/5\n\n"
        "🔗 Agar to'liq film/serial maxfiy kanal yoki xabarda bo'lsa, uning linkini yuboring.\n"
        "Kerak bo'lmasa, ⏭ O'tkazib yuborish tugmasini bosing.",
        reply_markup=movie_media_keyboard()
    )


@router.message(MovieAddState.video, F.text, ~F.text.in_({"⬅️ Orqaga", "❌ Bekor qilish"}))
async def movie_add_video_wrong(message: Message):
    await message.answer("🎬 Iltimos, kino videosini VIDEO ko'rinishida yuboring. Bu bosqichni o'tkazib yuborib bo'lmaydi.")


@router.message(MovieAddState.link, F.text == "⏭ O'tkazib yuborish")
async def movie_add_link_skip(message: Message, state: FSMContext):
    await state.update_data(link_url=None)
    await state.set_state(MovieAddState.type)
    await message.answer("🎬 Kino qo'shish — 5/5\n\nKino turini tanlang:", reply_markup=movie_type_keyboard())


@router.message(MovieAddState.link, F.text, ~F.text.in_({"⬅️ Orqaga", "❌ Bekor qilish", "⏭ O'tkazib yuborish"}))
async def movie_add_link(message: Message, state: FSMContext):
    link = message.text.strip()
    if not (link.startswith("https://t.me/") or link.startswith("http://t.me/") or link.startswith("https://telegram.me/") or link.startswith("http://telegram.me/")):
        await message.answer("❌ Telegram linkini yuboring. Masalan: https://t.me/c/123456789/123 yoki https://t.me/username/123")
        return
    await state.update_data(link_url=link)
    await state.set_state(MovieAddState.type)
    await message.answer("🎬 Kino qo'shish — 5/5\n\nKino turini tanlang:", reply_markup=movie_type_keyboard())


@router.callback_query(MovieAddState.type, F.data.startswith("movie_type:"))
async def movie_add_type(callback: CallbackQuery, state: FSMContext):
    if not is_admin_user(callback.from_user):
        await callback.answer("❌ Admin huquqi kerak.", show_alert=True)
        return
    data = await state.get_data()
    if not data.get("file_id"):
        await callback.answer("❌ Kino videosi majburiy.", show_alert=True)
        return
    prime_only = 1 if callback.data.endswith(":1") else 0
    try:
        DB.execute(
            "INSERT INTO movies(code,title,file_id,link_url,prime_only,views,added_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (data["code"], data["title"], data.get("file_id"), data.get("link_url"), prime_only, 0, callback.from_user.id, now())
        )
        DB.commit()
    except psycopg2.IntegrityError:
        DB.conn.rollback()
        await callback.answer("❌ Bu kino kodi allaqachon mavjud.", show_alert=True)
        return
    await state.clear()
    await callback.answer("✅ Kino qo'shildi!")
    delivery = "🎬 Video + link" if data.get("link_url") else "🎬 Video"
    await callback.message.answer(
        f"✅ Kino qo'shildi!\n\nKod: {escape(data['code'])}\nNomi: {escape(data['title'])}\nTuri: {'Prime' if prime_only else 'Oddiy'}\nUsul: {delivery}",
        reply_markup=movie_management_menu()
    )


@router.message(CardState.number, F.text, ~F.text.in_({"⬅️ Orqaga", "❌ Bekor qilish"}))
async def card_number(message: Message, state: FSMContext):
    number = "".join(message.text.split())
    if not number.isdigit() or len(number) != 16:
        await message.answer("❌ Karta raqami aynan 16 xonali raqam bo'lishi kerak.")
        return
    await state.update_data(card_number=number)
    await state.set_state(CardState.owner)
    await message.answer("2/2\n\n👤 Karta egasining ism-familiyasini yuboring.", reply_markup=back_cancel())


@router.message(CardState.owner, F.text, ~F.text.in_({"⬅️ Orqaga", "❌ Bekor qilish"}))
async def card_owner(message: Message, state: FSMContext):
    owner = message.text.strip()
    if len(owner) < 2:
        await message.answer("❌ Ism-familiya juda qisqa.")
        return
    data = await state.get_data()
    DB.execute("INSERT INTO cards(admin_id,card_number,card_owner,updated_at) VALUES(?,?,?,?) ON CONFLICT(admin_id) DO UPDATE SET card_number=excluded.card_number, card_owner=excluded.card_owner, updated_at=excluded.updated_at", (message.from_user.id, data["card_number"], owner, now()))
    DB.commit()
    await state.clear()
    await message.answer("✅ Karta muvaffaqiyatli saqlandi.", reply_markup=card_menu())


@router.message(AdminAddState.user, F.text, ~F.text.in_({"⬅️ Orqaga", "❌ Bekor qilish"}))
async def admin_add_user(message: Message, state: FSMContext, bot: Bot):
    if not is_superadmin_user(message.from_user):
        await state.clear()
        await message.answer("❌ Faqat @omono_v admin qo'sha oladi.")
        return
    raw = message.text.strip()
    target = None
    if raw.isdigit():
        target = get_user(int(raw))
    else:
        username = raw.lstrip("@").lower()
        target = DB.execute("SELECT * FROM users WHERE lower(COALESCE(username,''))=?", (username,)).fetchone()
    if not target:
        await message.answer("❌ Bu foydalanuvchi botga hali /start bermagan yoki topilmadi.")
        return
    uid = target["user_id"]
    if is_admin_id(uid):
        await message.answer("ℹ️ Bu user allaqachon admin.")
        return
    DB.execute("INSERT INTO admins(user_id,username,added_by,created_at) VALUES(?,?,?,?)", (uid, target["username"], message.from_user.id, now()))
    DB.commit()
    await set_commands_for_user(bot, uid)
    await state.clear()
    await message.answer("✅ Admin qo'shildi.", reply_markup=admin_manage_menu())


@router.message(AdminDeleteState.user, F.text, ~F.text.in_({"⬅️ Orqaga", "❌ Bekor qilish"}))
async def admin_delete_user(message: Message, state: FSMContext):
    if not is_superadmin_user(message.from_user):
        await state.clear()
        await message.answer("❌ Faqat @omono_v admin o'chira oladi.")
        return
    raw = message.text.strip()
    uid = int(raw) if raw.isdigit() else None
    if uid is None:
        row = DB.execute("SELECT user_id FROM users WHERE lower(COALESCE(username,''))=?", (raw.lstrip("@").lower(),)).fetchone()
        uid = row["user_id"] if row else None
    if uid is None:
        await message.answer("❌ Foydalanuvchi topilmadi.")
        return
    if is_superadmin_id(uid):
        await message.answer("❌ Bosh adminni o'chirib bo'lmaydi.")
        return
    if not is_admin_id(uid):
        await message.answer("❌ Bunday admin topilmadi.")
        return
    DB.execute("DELETE FROM admins WHERE user_id=?", (uid,))
    DB.commit()
    await state.clear()
    await message.answer("✅ Admin o'chirildi.", reply_markup=admin_manage_menu())


@router.message(ChannelAddState.username, F.text, ~F.text.in_({"⬅️ Orqaga", "❌ Bekor qilish"}))
async def channel_add(message: Message, state: FSMContext, bot: Bot):
    value = message.text.strip()
    if value.startswith("https://t.me/") or value.startswith("http://t.me/"):
        username = "@" + value.rstrip("/").split("/")[-1]
    else:
        username = value if value.startswith("@") else "@" + value
    try:
        chat = await bot.get_chat(username)
        me = await bot.get_me()
        member = await bot.get_chat_member(chat.id, me.id)
        if member.status != ChatMemberStatus.ADMINISTRATOR:
            await message.answer("❌ Bot bu kanalda administrator emas.")
            return
    except TelegramBadRequest:
        await message.answer("❌ Kanal topilmadi.")
        return
    except TelegramForbiddenError:
        await message.answer("❌ Bot bu kanalga kira olmaydi.")
        return
    except Exception:
        logger.exception("Kanal tekshirish xatosi")
        await message.answer("❌ Kanalni tekshirishda xatolik yuz berdi.")
        return
    try:
        DB.execute("INSERT INTO channels(username,title) VALUES(?,?)", (username, chat.title or username))
        DB.commit()
    except psycopg2.IntegrityError:
        DB.rollback()
        await message.answer("ℹ️ Bu kanal allaqachon mavjud.")
        return
    await state.clear()
    await message.answer("✅ Kanal qo'shildi.", reply_markup=channel_menu())


@router.message(ChannelDeleteState.username, F.text, ~F.text.in_({"⬅️ Orqaga", "❌ Bekor qilish"}))
async def channel_delete(message: Message, state: FSMContext):
    value = message.text.strip()
    username = value if value.startswith("@") else "@" + value
    row = DB.execute("SELECT 1 FROM channels WHERE username=?", (username,)).fetchone()
    if not row:
        await message.answer("❌ Bunday kanal topilmadi.")
        return
    DB.execute("DELETE FROM channels WHERE username=?", (username,))
    DB.commit()
    await state.clear()
    await message.answer("✅ Kanal o'chirildi.", reply_markup=channel_menu())


@router.message(MovieDeleteState.code, F.text, ~F.text.in_({"⬅️ Orqaga", "❌ Bekor qilish"}))
async def movie_delete_code(message: Message, state: FSMContext):
    if not is_admin_user(message.from_user):
        await state.clear()
        await message.answer("❌ Admin huquqi kerak.", reply_markup=main_menu())
        return
    code = message.text.strip()
    if not code.isdigit() or len(code) != 3:
        await message.answer("❌ Kino kodi aynan 3 xonali raqam bo'lishi kerak. Masalan: 327")
        return
    movie = DB.execute("SELECT * FROM movies WHERE code=?", (code,)).fetchone()
    if not movie:
        await message.answer("❌ Bunday kino topilmadi. Qayta urinib ko'ring.")
        return
    await state.update_data(movie_id=movie["id"], code=movie["code"])
    await state.set_state(MovieDeleteState.confirm)
    await message.answer(
        f"⚠️ Kino o'chiriladi!\n\n"
        f"🎬 {escape(movie['title'])}\n"
        f"🔢 Kod: {escape(movie['code'])}\n"
        f"⭐ Turi: {'Prime' if movie['prime_only'] else 'Oddiy'}\n"
        f"👁 Ko'rishlar: {movie['views']}\n\n"
        "Bu amalni qaytarib bo'lmaydi. Davom etamizmi?",
        reply_markup=movie_delete_confirm_keyboard(movie["id"])
    )


@router.callback_query(MovieDeleteState.confirm, F.data.startswith("movie_delete:"))
async def movie_delete_decision(callback: CallbackQuery, state: FSMContext):
    if not is_admin_user(callback.from_user):
        await state.clear()
        await callback.answer("❌ Admin huquqi kerak.", show_alert=True)
        return
    if callback.data == "movie_delete:no":
        await state.clear()
        await callback.answer("Bekor qilindi.")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer("🗑 Kino o'chirish bekor qilindi.", reply_markup=movie_management_menu())
        return
    parts = callback.data.split(":")
    if len(parts) != 3 or parts[1] != "yes":
        await callback.answer("❌ So'rov xato.", show_alert=True)
        return
    try:
        movie_id = int(parts[2])
    except ValueError:
        await callback.answer("❌ Kino ID xato.", show_alert=True)
        return
    data = await state.get_data()
    if data.get("movie_id") != movie_id:
        await callback.answer("❌ So'rov eskirgan.", show_alert=True)
        return
    movie = DB.execute("SELECT * FROM movies WHERE id=?", (movie_id,)).fetchone()
    if not movie:
        await state.clear()
        await callback.answer("ℹ️ Kino allaqachon o'chirilgan.", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer("ℹ️ Kino topilmadi.", reply_markup=movie_management_menu())
        return
    DB.execute("DELETE FROM movies WHERE id=?", (movie_id,))
    DB.commit()
    await state.clear()
    await callback.answer("✅ Kino o'chirildi!")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(
        f"✅ Kino o'chirildi.\n\n🎬 {escape(movie['title'])}\n🔢 Kod: {escape(movie['code'])}",
        reply_markup=movie_management_menu()
    )


# =========================
# MOVIE EDITING
# =========================
@router.message(MovieEditState.code, F.text, ~F.text.in_({"⬅️ Orqaga", "❌ Bekor qilish"}))
async def movie_edit_code_lookup(message: Message, state: FSMContext):
    if not is_admin_user(message.from_user):
        await state.clear()
        await message.answer("❌ Admin huquqi kerak.", reply_markup=main_menu())
        return
    code = message.text.strip()
    if not code.isdigit() or len(code) != 3:
        await message.answer("❌ Kino kodi aynan 3 xonali raqam bo'lishi kerak. Masalan: 327")
        return
    movie = DB.execute("SELECT * FROM movies WHERE code=?", (code,)).fetchone()
    if not movie:
        await message.answer("❌ Bunday kino topilmadi. Qayta urinib ko'ring.")
        return
    await state.update_data(movie_id=movie["id"], original_code=movie["code"])
    await state.set_state(MovieEditState.menu)
    await message.answer(
        f"✏️ Kino tahrirlash\n\n"
        f"🎬 Nomi: {escape(movie['title'])}\n"
        f"🔢 Kod: {escape(movie['code'])}\n"
        f"⭐ Turi: {'Prime' if movie['prime_only'] else 'Oddiy'}\n"
        f"👁 Ko'rishlar: {movie['views']}\n"
        f"🎬 Video: {'Bor' if movie['file_id'] else 'Yo‘q'}\n"
        f"🔗 Link: {'Bor' if movie['link_url'] else 'Yo‘q'}\n\n"
        "Qaysi qismini o'zgartirasiz?",
        reply_markup=movie_edit_keyboard()
    )


@router.callback_query(F.data.startswith("movie_edit:"))
async def movie_edit_callback(callback: CallbackQuery, state: FSMContext):
    if not is_admin_user(callback.from_user):
        await state.clear()
        await callback.answer("❌ Admin huquqi kerak.", show_alert=True)
        return
    current = await state.get_state()
    if not current or not current.startswith("MovieEditState:"):
        await callback.answer("⏳ Avval kino tahrirlashni boshlang.", show_alert=True)
        return
    action = callback.data.split(":", 1)[1]
    data = await state.get_data()
    movie_id = data.get("movie_id")
    movie = DB.execute("SELECT * FROM movies WHERE id=?", (movie_id,)).fetchone()
    if not movie:
        await state.clear()
        await callback.answer("❌ Kino topilmadi.", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer("❌ Kino topilmadi.", reply_markup=movie_management_menu())
        return

    if action == "back":
        if current != MovieEditState.menu.state:
            await callback.answer("⏳ Avval joriy tahrirlash bosqichini tugating yoki Orqaga bosing.", show_alert=True)
            return
        await state.clear()
        await callback.answer()
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer("🎬 Kino boshqaruvi", reply_markup=movie_management_menu())
        return

    if action == "return_menu":
        if current not in {MovieEditState.link.state, MovieEditState.type.state}:
            await callback.answer("⏳ Hozirgi amal uchun Orqaga tugmasidan foydalaning.", show_alert=True)
            return
        await state.set_state(MovieEditState.menu)
        await callback.answer()
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer("✏️ Qaysi qismini o'zgartirasiz?", reply_markup=movie_edit_keyboard())
        return

    if action == "link_remove":
        if current != MovieEditState.link.state:
            await callback.answer("⏳ Hozir link tahrirlash bosqichi emas.", show_alert=True)
            return
        DB.execute("UPDATE movies SET link_url=NULL WHERE id=?", (movie_id,))
        DB.commit()
        await state.set_state(MovieEditState.menu)
        await callback.answer("✅ Link olib tashlandi.")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer("✅ Link olib tashlandi.", reply_markup=movie_edit_keyboard())
        return

    if action in {"type:0", "type:1"}:
        if current != MovieEditState.type.state:
            await callback.answer("⏳ Hozir kino turi tanlash bosqichi emas.", show_alert=True)
            return
        value = int(action.rsplit(":", 1)[1])
        DB.execute("UPDATE movies SET prime_only=? WHERE id=?", (value, movie_id))
        DB.commit()
        await state.set_state(MovieEditState.menu)
        await callback.answer("✅ Kino turi o'zgartirildi.")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer("✅ Kino turi o'zgartirildi.", reply_markup=movie_edit_keyboard())
        return

    mapping = {
        "code": (MovieEditState.edit_code, "✏️ Yangi kino kodini yuboring. Kod aynan 3 xonali bo'lishi kerak."),
        "title": (MovieEditState.title, "📝 Yangi kino nomini yuboring."),
        "video": (MovieEditState.video, "🎬 Yangi VIDEO yuboring."),
        "link": (MovieEditState.link, "🔗 Yangi Telegram linkini yuboring yoki linkni olib tashlash uchun tugmani bosing."),
        "type": (MovieEditState.type, "⭐ Kino turini tanlang."),
    }
    if action not in mapping:
        await callback.answer("❌ Noma'lum amal.", show_alert=True)
        return
    new_state, prompt = mapping[action]
    await state.set_state(new_state)
    await callback.answer()
    if action == "link":
        b = InlineKeyboardBuilder()
        b.row(InlineKeyboardButton(text="🗑 Linkni olib tashlash", callback_data="movie_edit:link_remove"))
        b.row(InlineKeyboardButton(text="⬅️ Orqaga", callback_data="movie_edit:return_menu"))
        await callback.message.answer(prompt, reply_markup=b.as_markup())
    elif action == "type":
        b = InlineKeyboardBuilder()
        b.row(
            InlineKeyboardButton(text="🆓 Oddiy kino", callback_data="movie_edit:type:0"),
            InlineKeyboardButton(text="⭐ Faqat Prime", callback_data="movie_edit:type:1"),
        )
        b.row(InlineKeyboardButton(text="⬅️ Orqaga", callback_data="movie_edit:return_menu"))
        await callback.message.answer(prompt, reply_markup=b.as_markup())
    else:
        await callback.message.answer(prompt, reply_markup=back_cancel())


@router.message(MovieEditState.edit_code, F.text, ~F.text.in_({"⬅️ Orqaga", "❌ Bekor qilish"}))
async def movie_edit_save_code(message: Message, state: FSMContext):
    code = message.text.strip()
    if not code.isdigit() or len(code) != 3:
        await message.answer("❌ Kino kodi aynan 3 xonali raqam bo'lishi kerak. Masalan: 327")
        return
    data = await state.get_data()
    movie_id = data.get("movie_id")
    existing = DB.execute("SELECT id FROM movies WHERE code=? AND id<>?", (code, movie_id)).fetchone()
    if existing:
        await message.answer("❌ Bu kod boshqa kinoga tegishli. Boshqa kod tanlang.")
        return
    try:
        DB.execute("UPDATE movies SET code=? WHERE id=?", (code, movie_id))
        DB.commit()
    except psycopg2.IntegrityError:
        DB.rollback()
        await message.answer("❌ Bu kod boshqa kinoga tegishli. Boshqa kod tanlang.")
        return
    await state.set_state(MovieEditState.menu)
    await message.answer("✅ Kino kodi o'zgartirildi.", reply_markup=movie_edit_keyboard())


@router.message(MovieEditState.title, F.text, ~F.text.in_({"⬅️ Orqaga", "❌ Bekor qilish"}))
async def movie_edit_save_title(message: Message, state: FSMContext):
    title = message.text.strip()
    if not title:
        await message.answer("❌ Kino nomi bo'sh bo'lmasin.")
        return
    data = await state.get_data()
    DB.execute("UPDATE movies SET title=? WHERE id=?", (title, data.get("movie_id")))
    DB.commit()
    await state.set_state(MovieEditState.menu)
    await message.answer("✅ Kino nomi o'zgartirildi.", reply_markup=movie_edit_keyboard())


@router.message(MovieEditState.video, F.video)
async def movie_edit_save_video(message: Message, state: FSMContext):
    data = await state.get_data()
    DB.execute("UPDATE movies SET file_id=? WHERE id=?", (message.video.file_id, data.get("movie_id")))
    DB.commit()
    await state.set_state(MovieEditState.menu)
    await message.answer("✅ Kino videosi almashtirildi.", reply_markup=movie_edit_keyboard())


@router.message(MovieEditState.video, F.text, ~F.text.in_({"⬅️ Orqaga", "❌ Bekor qilish"}))
async def movie_edit_video_wrong(message: Message):
    await message.answer("🎬 Iltimos, yangi videoni VIDEO ko'rinishida yuboring.")


@router.message(MovieEditState.link, F.text, ~F.text.in_({"⬅️ Orqaga", "❌ Bekor qilish"}))
async def movie_edit_save_link(message: Message, state: FSMContext):
    link = message.text.strip()
    if not (link.startswith("https://t.me/") or link.startswith("http://t.me/") or link.startswith("https://telegram.me/") or link.startswith("http://telegram.me/")):
        await message.answer("❌ Telegram linkini yuboring. Masalan: https://t.me/c/123456789/123")
        return
    data = await state.get_data()
    DB.execute("UPDATE movies SET link_url=? WHERE id=?", (link, data.get("movie_id")))
    DB.commit()
    await state.set_state(MovieEditState.menu)
    await message.answer("✅ Kino linki saqlandi.", reply_markup=movie_edit_keyboard())


@router.message(PaymentState.screenshot, F.photo)
async def payment_photo(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    admin_id = data.get("admin_id")
    if not admin_id:
        await state.clear()
        await message.answer("❌ To'lov ma'lumotlari topilmadi.", reply_markup=main_menu())
        return
    photo_id = message.photo[-1].file_id
    # Payment yaratilishidan oldin user yozuvi mavjudligini kafolatlaymiz.
    upsert_user(message.from_user)
    payment_cur = DB.execute("INSERT INTO payments(user_id,admin_id,plan,days,price,status,screenshot_file_id,created_at) VALUES(?,?,?,?,?,?,?,?) RETURNING id", (message.from_user.id, admin_id, data["plan"], data["days"], data["price"], "pending", photo_id, now()))
    payment_id = payment_cur.fetchone()["id"]
    DB.commit()
    await state.clear()
    caption = f"🧾 Yangi PRIME to'lov!\n\n👤 User: {message.from_user.id}\n📦 Plan: {escape(data['plan'])}\n📅 Muddat: {'Umrbod' if data['days']==0 else str(data['days'])+' kun'}\n💰 Narx: {data['price']:,} so'm"
    try:
        await bot.send_photo(admin_id, photo_id, caption=caption, reply_markup=payment_keyboard(payment_id))
        await message.answer("✅ Screenshot qabul qilindi.\n\nAdmin tekshirganidan keyin natija yuboriladi.", reply_markup=main_menu())
    except Exception:
        logger.exception("Payment adminga yuborilmadi")
        await message.answer("⚠️ To'lov saqlandi, ammo mas'ul adminga yuborilmadi. Admin botga /start yuborishi kerak.", reply_markup=main_menu())


@router.message(PaymentState.screenshot, ~F.text.in_({"⬅️ Orqaga", "❌ Bekor qilish"}))
async def payment_wrong(message: Message):
    await message.answer("📸 Iltimos, screenshotni RASM ko'rinishida yuboring.")


@router.message(OrderState.text, F.text, ~F.text.in_({"⬅️ Orqaga", "❌ Bekor qilish"}))
async def order_text(message: Message, state: FSMContext, bot: Bot):
    text = message.text.strip()
    admin_id = get_referral_admin(message.from_user.id) or get_superadmin_id()
    if not admin_id:
        await state.clear()
        await message.answer("❌ Mas'ul admin aniqlanmadi.", reply_markup=main_menu())
        return
    order_cur = DB.execute("INSERT INTO orders(user_id,admin_id,text,created_at) VALUES(?,?,?,?) RETURNING id", (message.from_user.id, admin_id, text, now()))
    order_id = order_cur.fetchone()["id"]
    DB.commit()
    await state.clear()
    await message.answer("✅ Buyurtmangiz qabul qilindi.", reply_markup=main_menu())
    try:
        await bot.send_message(admin_id, f"🎬 Yangi kino buyurtma!\n\n🧾 ID: {order_id}\n👤 User ID: {message.from_user.id}\n📝 {escape(text)}")
    except Exception:
        logger.exception("Buyurtmani adminga yuborishda xato")

# =========================
# Back/cancel only when FSM exists
# =========================
@router.message(StateFilter("*"), F.text == "❌ Bekor qilish")
async def cancel_any(message: Message, state: FSMContext):
    current = await state.get_state()
    await state.clear()
    if current and current.startswith(("CardState",)):
        await message.answer("❌ Bekor qilindi.", reply_markup=card_menu())
    elif current and current.startswith(("SearchState", "PaymentState", "OrderState")):
        await message.answer("❌ Bekor qilindi.", reply_markup=main_menu())
    elif current and current.startswith(("MovieEditState", "MovieDeleteState", "MovieAddState")):
        await message.answer("❌ Bekor qilindi.", reply_markup=movie_management_menu())
    else:
        await message.answer("❌ Bekor qilindi.", reply_markup=admin_menu(message.from_user) if is_admin_user(message.from_user) else main_menu())


@router.message(StateFilter("*"), F.text == "⬅️ Orqaga")
async def back_any(message: Message, state: FSMContext):
    current = await state.get_state()
    if current == CardState.owner.state:
        await state.set_state(CardState.number)
        await message.answer("💳 Karta raqamini yuboring.", reply_markup=back_cancel())
    elif current == CardState.number.state:
        await state.clear(); await message.answer("💳 Karta sozlamalari", reply_markup=card_menu())
    elif current == MovieEditState.menu.state:
        await state.clear(); await message.answer("🎬 Kino boshqaruvi", reply_markup=movie_management_menu())
    elif current in {MovieEditState.edit_code.state, MovieEditState.title.state, MovieEditState.video.state, MovieEditState.link.state, MovieEditState.type.state}:
        await state.set_state(MovieEditState.menu)
        await message.answer("✏️ Kino tahrirlash", reply_markup=movie_edit_keyboard())
    elif current == MovieDeleteState.confirm.state:
        await state.clear(); await message.answer("🗑 Kino o'chirish bekor qilindi.", reply_markup=movie_management_menu())
    elif current == MovieDeleteState.code.state:
        await state.clear(); await message.answer("🎬 Kino boshqaruvi", reply_markup=movie_management_menu())
    elif current == MovieAddState.type.state:
        await state.set_state(MovieAddState.link); await message.answer("🎬 Kino qo'shish — 4/5\n\n🔗 Linkni yuboring yoki ⏭ O'tkazib yuborish tugmasini bosing.", reply_markup=movie_media_keyboard())
    elif current == MovieAddState.link.state:
        await state.set_state(MovieAddState.video); await message.answer("🎬 Kino qo'shish — 3/5\n\nVIDEO yuboring. Bu bosqich majburiy.", reply_markup=back_cancel())
    elif current == MovieAddState.video.state:
        await state.set_state(MovieAddState.title); await message.answer("🎬 Kino qo'shish — 2/5\n\nKino nomini yuboring.", reply_markup=back_cancel())
    elif current == MovieAddState.title.state:
        await state.set_state(MovieAddState.code); await message.answer("🎬 Kino kodini yuboring.", reply_markup=back_cancel())
    elif current == MovieAddState.code.state or current == MovieDeleteState.code.state:
        await state.clear(); await send_admin(message)
    elif current in {AdminAddState.user.state, AdminDeleteState.user.state}:
        await state.clear(); await message.answer("👥 Adminlar", reply_markup=admin_manage_menu())
    elif current in {ChannelAddState.username.state, ChannelDeleteState.username.state}:
        await state.clear(); await message.answer("📢 Kanallar", reply_markup=channel_menu())
    else:
        await state.clear(); await send_main(message)

# =========================
# NO-STATE USER MENU
# Every menu handler explicitly requires no FSM state.
# =========================
@router.message(StateFilter(None), F.text == "🔎 Kino qidirish")
async def search_start(message: Message, state: FSMContext):
    await state.set_state(SearchState.code)
    await message.answer("🔎 Kino qidirish\n\n3 xonali kino kodi yoki kino nomi/so'zini yuboring.\nMasalan: 247 yoki Jasur", reply_markup=back_cancel())


@router.message(StateFilter(None), F.text == "⭐ Prime status")
async def prime_status(message: Message):
    row = get_user(message.from_user.id)
    if active_prime(message.from_user.id):
        await message.answer(f"⭐ Prime status\n\n✅ Faol\n\n📅 Tugash sanasi:\n{prime_until_text(row['prime_until'])}")
    else:
        await message.answer("⭐ Prime status\n\n❌ Prime faol emas.", reply_markup=prime_keyboard())


@router.callback_query(F.data.startswith("prime:"))
async def choose_prime(callback: CallbackQuery, state: FSMContext):
    try:
        _, plan, price, days = callback.data.split(":")
        price, days = int(price), int(days)
    except ValueError:
        await callback.answer("❌ Tarif xato.", show_alert=True); return
    admin_id = get_referral_admin(callback.from_user.id) or get_superadmin_id()
    if not admin_id:
        await callback.answer("❌ Mas'ul admin aniqlanmadi.", show_alert=True); return
    card = get_admin_card(admin_id)
    if not card:
        await callback.answer("❌ Mas'ul adminning kartasi sozlanmagan.", show_alert=True); return
    plan_name = {"7":"7 kun", "1oy":"1 oy", "3oy":"3 oy", "lifetime":"Umrbod"}[plan]
    await state.set_state(PaymentState.screenshot)
    await state.update_data(admin_id=admin_id, plan=plan_name, days=days, price=price)
    await callback.answer()
    await callback.message.answer(f"💳 To'lov ma'lumotlari\n\n💳 Karta: {escape(card['card_number'])}\n👤 Egasi: {escape(card['card_owner'])}\n💰 Narx: {price:,} so'm\n\n📸 To'lov screenshotini RASM ko'rinishida yuboring.", reply_markup=back_cancel())


@router.message(StateFilter(None), F.text == "📚 Kinolar ro'yxati")
async def user_movies(message: Message):
    rows = DB.execute("SELECT * FROM movies ORDER BY id DESC").fetchall()
    if not rows:
        await message.answer("📚 Hozircha kinolar mavjud emas.")
        return
    blocks = [
        f"{'⭐' if r['prime_only'] else '🎬'} {escape(r['title'])}\n🔢 Kod: {escape(r['code'])} | 👁 {r['views']}"
        for r in rows
    ]
    chunks, current = [], ""
    for block in blocks:
        candidate = block if not current else current + "\n\n" + block
        if len(candidate) > 3500:
            if current:
                chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    for i, chunk in enumerate(chunks, 1):
        prefix = f"📚 Kinolar ro'yxati — {i}/{len(chunks)}\n\n" if len(chunks) > 1 else "📚 Kinolar ro'yxati\n\n"
        await message.answer(prefix + chunk)


@router.message(StateFilter(None), F.text == "📸 Instagramga qaytish")
async def instagram(message: Message):
    b = InlineKeyboardBuilder(); b.row(InlineKeyboardButton(text="📸 Instagramga o'tish", url=INSTAGRAM_URL))
    await message.answer("📸 Instagram sahifamiz:", reply_markup=b.as_markup())


@router.message(StateFilter(None), F.text == "🎬 Kino buyurtma qilish")
async def order_start(message: Message, state: FSMContext):
    await state.set_state(OrderState.text)
    await message.answer("🎬 Qanday kino kerakligini yozib yuboring.", reply_markup=back_cancel())


@router.message(StateFilter(None), F.text == "🤝 Reklama & Bot olish")
async def ads(message: Message):
    await message.answer("🤝 Reklama & Bot olish\n\nAdmin: @omono_v")

# =========================
# NO-STATE ADMIN MENU
# =========================
@router.message(StateFilter(None), F.text == "🎬 Kino boshqaruvi")
async def movie_management_start(message: Message, state: FSMContext):
    if not is_admin_user(message.from_user): return
    await state.clear()
    await message.answer("🎬 Kino boshqaruvi", reply_markup=movie_management_menu())


@router.message(StateFilter(None), F.text == "➕ Kino qo'shish")
async def movie_add_start(message: Message, state: FSMContext):
    if not is_admin_user(message.from_user): return
    await state.set_state(MovieAddState.code)
    await message.answer("🎬 Kino qo'shish — 1/5\n\nKino kodini yuboring.", reply_markup=back_cancel())


@router.message(StateFilter(None), F.text == "✏️ Kino tahrirlash")
async def movie_edit_start(message: Message, state: FSMContext):
    if not is_admin_user(message.from_user): return
    await state.set_state(MovieEditState.code)
    await message.answer("✏️ Kino tahrirlash\n\nTahrirlamoqchi bo'lgan kino kodini yuboring.\nMasalan: 327", reply_markup=back_cancel())


@router.message(StateFilter(None), F.text == "🗑 Kino o'chirish")
async def movie_delete_start(message: Message, state: FSMContext):
    if not is_admin_user(message.from_user): return
    await state.set_state(MovieDeleteState.code)
    await message.answer("🗑 Kino o'chirish\n\nKino kodini yuboring. Masalan: 327", reply_markup=back_cancel())


@router.message(StateFilter(None), F.text == "📚 Kinolar")
async def admin_movies(message: Message):
    if not is_admin_user(message.from_user): return
    rows = DB.execute("SELECT * FROM movies ORDER BY id DESC").fetchall()
    if not rows:
        await message.answer("📚 Hozircha kinolar mavjud emas.", reply_markup=movie_management_menu())
        return
    blocks = [
        f"{'⭐' if r['prime_only'] else '🎬'} {escape(r['title'])}\n🔢 {escape(r['code'])} | 👁 {r['views']} | 🔗 {'Bor' if r['link_url'] else 'Yo‘q'}"
        for r in rows
    ]
    chunks, current = [], ""
    for block in blocks:
        candidate = block if not current else current + "\n\n" + block
        if len(candidate) > 3500:
            if current:
                chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    for i, chunk in enumerate(chunks, 1):
        prefix = f"📚 Kinolar — {i}/{len(chunks)}\n\n" if len(chunks) > 1 else "📚 Kinolar\n\n"
        await message.answer(prefix + chunk)
    await message.answer("🎬 Kino boshqaruvi", reply_markup=movie_management_menu())


@router.message(StateFilter(None), F.text == "💳 Karta sozlamalari")
async def card_settings(message: Message, state: FSMContext):
    if not is_admin_user(message.from_user): return
    await state.clear(); await message.answer("💳 Karta sozlamalari", reply_markup=card_menu())


@router.message(StateFilter(None), F.text.in_({"➕ Karta qo'shish", "🔄 Kartani almashtirish"}))
async def card_start(message: Message, state: FSMContext):
    if not is_admin_user(message.from_user): return
    await state.set_state(CardState.number)
    await message.answer("💳 Karta raqamini yuboring.\n\nAynan 16 xonali raqam.\nMasalan:\n9860600435412504", reply_markup=back_cancel())


@router.message(StateFilter(None), F.text == "👀 Hozirgi kartani ko'rish")
async def card_view(message: Message):
    if not is_admin_user(message.from_user): return
    row = get_admin_card(message.from_user.id)
    if not row: await message.answer("❌ Sizda hali karta saqlanmagan."); return
    await message.answer(f"💳 Hozirgi karta\n\n💳 Raqam: {escape(row['card_number'])}\n👤 Egasi: {escape(row['card_owner'])}\n🕒 Yangilangan: {row['updated_at']}")


@router.message(StateFilter(None), F.text == "🗑 Kartani o'chirish")
async def card_delete(message: Message):
    if not is_admin_user(message.from_user): return
    DB.execute("DELETE FROM cards WHERE admin_id=?", (message.from_user.id,)); DB.commit()
    await message.answer("✅ Karta o'chirildi.", reply_markup=card_menu())


@router.message(StateFilter(None), F.text == "📢 Kanallar")
async def channels_start(message: Message, state: FSMContext):
    if not is_admin_user(message.from_user): return
    await state.clear(); await message.answer("📢 Kanallar", reply_markup=channel_menu())


@router.message(StateFilter(None), F.text == "➕ Kanal qo'shish")
async def channel_add_start(message: Message, state: FSMContext):
    if not is_admin_user(message.from_user): return
    await state.set_state(ChannelAddState.username); await message.answer("➕ Kanal qo'shish\n\n@username yoki https://t.me/username yuboring.", reply_markup=back_cancel())


@router.message(StateFilter(None), F.text == "🗑 Kanal o'chirish")
async def channel_delete_start(message: Message, state: FSMContext):
    if not is_admin_user(message.from_user): return
    await state.set_state(ChannelDeleteState.username); await message.answer("🗑 Kanal username'sini yuboring.", reply_markup=back_cancel())


@router.message(StateFilter(None), F.text == "📋 Kanallar")
async def channel_list(message: Message):
    if not is_admin_user(message.from_user): return
    rows = DB.execute("SELECT * FROM channels ORDER BY id").fetchall()
    await message.answer("📢 Kanallar\n\n" + "\n\n".join(f"📢 {escape(r['username'])}\n📝 {escape(r['title'] or '')}" for r in rows) if rows else "📢 Kanallar ro'yxati bo'sh.")


@router.message(StateFilter(None), F.text == "👥 Adminlar")
async def admin_manage(message: Message, state: FSMContext):
    if not is_superadmin_user(message.from_user):
        await message.answer("❌ Bu bo'lim faqat @omono_v uchun."); return
    await state.clear(); await message.answer("👥 Adminlar", reply_markup=admin_manage_menu())


@router.message(StateFilter(None), F.text == "➕ Admin qo'shish")
async def admin_add_start(message: Message, state: FSMContext):
    if not is_superadmin_user(message.from_user):
        await message.answer("❌ Faqat @omono_v admin qo'sha oladi."); return
    await state.set_state(AdminAddState.user)
    await message.answer("➕ Admin qo'shish\n\nTelegram USER ID yoki @username yuboring.\nFoydalanuvchi avval botga /start bergan bo'lishi kerak.", reply_markup=back_cancel())


@router.message(StateFilter(None), F.text == "🗑 Admin o'chirish")
async def admin_delete_start(message: Message, state: FSMContext):
    if not is_superadmin_user(message.from_user):
        await message.answer("❌ Faqat @omono_v admin o'chira oladi."); return
    await state.set_state(AdminDeleteState.user)
    await message.answer("🗑 Admin USER ID yoki @username sini yuboring.", reply_markup=back_cancel())


@router.message(StateFilter(None), F.text == "📋 Adminlar")
async def admin_list(message: Message):
    if not is_superadmin_user(message.from_user): return
    rows = DB.execute("SELECT * FROM admins ORDER BY created_at").fetchall()
    if not rows: await message.answer("👥 Adminlar ro'yxati bo'sh."); return
    await message.answer("👥 Adminlar\n\n" + "\n\n".join(f"👤 @{escape((r['username'] or '').lstrip('@'))}\n🆔 {r['user_id']}" for r in rows))


@router.message(StateFilter(None), F.text == "📊 Statistika")
async def stats(message: Message):
    if not is_admin_user(message.from_user): return
    aid = message.from_user.id
    refs = DB.execute("SELECT COUNT(*) c FROM referrals WHERE admin_id=?", (aid,)).fetchone()["c"]
    revenue = DB.execute("SELECT COALESCE(SUM(price),0) s FROM payments WHERE admin_id=? AND status='approved'", (aid,)).fetchone()["s"]
    approved = DB.execute("SELECT COUNT(*) c FROM payments WHERE admin_id=? AND status='approved'", (aid,)).fetchone()["c"]
    pending = DB.execute("SELECT COUNT(*) c FROM payments WHERE admin_id=? AND status='pending'", (aid,)).fetchone()["c"]
    text = f"📊 Statistika\n\n👥 Sizning referral orqali kirganlar: {refs}\n💰 Tasdiqlangan Prime tushumi: {revenue:,} so'm\n📦 Tasdiqlangan to'lovlar: {approved}\n📦 Kutilayotgan to'lovlar: {pending}"
    if is_superadmin_user(message.from_user):
        users = DB.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        movies = DB.execute("SELECT COUNT(*) c FROM movies").fetchone()["c"]
        primes = DB.execute("SELECT COUNT(*) c FROM users WHERE prime_until IS NOT NULL AND prime_until > ?", (now(),)).fetchone()["c"]
        total = DB.execute("SELECT COALESCE(SUM(price),0) s FROM payments WHERE status='approved'").fetchone()["s"]
        admins = DB.execute("SELECT COUNT(*) c FROM admins").fetchone()["c"]
        text += f"\n\n🌐 Umumiy statistika\n\n👥 Umumiy foydalanuvchilar: {users}\n🎬 Kinolar: {movies}\n⭐ Prime userlar: {primes}\n💰 Umumiy tushum: {total:,} so'm\n👥 Adminlar: {admins}"
    await message.answer(text)


@router.message(StateFilter(None), F.text == "🔗 Mening referralim")
async def referral(message: Message, bot: Bot):
    if not is_admin_user(message.from_user): return
    me = await bot.get_me(); username = me.username or BOT_USERNAME
    link = f"https://t.me/{username}?start=ref_{message.from_user.id}"
    count = DB.execute("SELECT COUNT(*) c FROM referrals WHERE admin_id=?", (message.from_user.id,)).fetchone()["c"]
    revenue = DB.execute("SELECT COALESCE(SUM(price),0) s FROM payments WHERE admin_id=? AND status='approved'", (message.from_user.id,)).fetchone()["s"]
    await message.answer(f"🔗 Sizning referral linkingiz:\n\n{link}\n\n👥 Silka orqali kirganlar: {count}\n💰 Tasdiqlangan tushum: {revenue:,} so'm")


@router.message(StateFilter(None), F.text == "⬅️ Admin panel")
async def admin_back(message: Message, state: FSMContext):
    await state.clear()
    if is_admin_user(message.from_user): await send_admin(message)
    else: await send_main(message)

# =========================
# Numeric fallback ONLY when NO FSM state
# =========================
@router.message(StateFilter(None), F.text.regexp(r"^\d{3}$"))
async def numeric_movie_fallback(message: Message):
    movie = DB.execute("SELECT * FROM movies WHERE code=?", (message.text.strip(),)).fetchone()
    if not movie:
        await message.answer("❌ Bunday kino topilmadi."); return
    if movie["prime_only"] and not active_prime(message.from_user.id) and not is_admin_id(message.from_user.id):
        await message.answer("⭐ Bu kino faqat Prime uchun."); return
    DB.execute("UPDATE movies SET views=views+1 WHERE id=?", (movie["id"],)); DB.commit()
    try:
        await deliver_movie(message, movie)
    except Exception:
        logger.exception("Fallback kino yuborishda xato")
        await message.answer("❌ Kino/linkni yuborishda xatolik yuz berdi.")

# Last fallback is explicitly NO STATE.
@router.message(StateFilter(None))
async def final_fallback(message: Message):
    await message.answer("❌ Buyruq yoki kino topilmadi.\n\nKerakli bo'limni menyudan tanlang.", reply_markup=main_menu())


@router.callback_query(F.data.startswith("pay:"))
async def payment_decision(callback: CallbackQuery, bot: Bot):
    parts = callback.data.split(":")
    if len(parts) != 3: await callback.answer("❌ Callback xato.", show_alert=True); return
    action = parts[1]
    try: payment_id = int(parts[2])
    except ValueError: await callback.answer("❌ Payment ID xato.", show_alert=True); return
    payment = DB.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
    if not payment: await callback.answer("❌ To'lov topilmadi.", show_alert=True); return
    if payment["admin_id"] != callback.from_user.id and not is_superadmin_id(callback.from_user.id):
        await callback.answer("❌ Bu to'lov sizga biriktirilmagan.", show_alert=True); return
    if payment["status"] != "pending": await callback.answer("ℹ️ Bu to'lov allaqachon ko'rib chiqilgan.", show_alert=True); return
    if action == "approve":
        # Payment jadvalidagi user_id asosiy manba. Ba'zan users yozuvi
        # eski DB/import/restart sabab yo'qolgan bo'lishi mumkin. Bunday
        # holatda to'lovni bekor qilmaymiz: userni minimal ma'lumot bilan
        # qayta yaratib, Prime'ni faollashtiramiz.
        user = get_user(payment["user_id"])
        if not user:
            DB.execute(
                "INSERT INTO users(user_id, username, first_name, created_at) VALUES(?, ?, ?, ?) ON CONFLICT(user_id) DO NOTHING",
                (payment["user_id"], "", "", now())
            )
            DB.commit()
            user = get_user(payment["user_id"])
        if not user:
            await callback.answer("❌ User ma'lumotini tiklab bo'lmadi.", show_alert=True)
            return
        if payment["days"] == 0:
            until = "9999-12-31 23:59:59"
        else:
            base = datetime.now(timezone.utc).replace(tzinfo=None)
            if user["prime_until"]:
                try:
                    old = datetime.fromisoformat(user["prime_until"])
                    if old > base: base = old
                except ValueError: pass
            until = (base + timedelta(days=payment["days"])).strftime("%Y-%m-%d %H:%M:%S")
        if is_superadmin_id(callback.from_user.id) and payment["admin_id"] != callback.from_user.id:
            cur = DB.execute("UPDATE payments SET status='approved',decided_at=? WHERE id=? AND status='pending'", (now(), payment_id))
        else:
            cur = DB.execute("UPDATE payments SET status='approved',decided_at=? WHERE id=? AND status='pending' AND admin_id=?", (now(), payment_id, callback.from_user.id))
        if cur.rowcount != 1: await callback.answer("ℹ️ To'lov allaqachon ko'rib chiqilgan.", show_alert=True); return
        DB.execute("UPDATE users SET prime_until=? WHERE user_id=?", (until, payment["user_id"])); DB.commit()
        await callback.answer("✅ To'lov tasdiqlandi.")
        try: await callback.message.edit_reply_markup(reply_markup=None)
        except Exception: pass
        try: await bot.send_message(payment["user_id"], f"✅ To'lov tasdiqlandi!\n\n⭐ Prime: {payment['plan']}\n📅 Tugash sanasi: {prime_until_text(until)}")
        except Exception: logger.exception("Prime xabari yuborilmadi")
    elif action == "reject":
        if is_superadmin_id(callback.from_user.id) and payment["admin_id"] != callback.from_user.id:
            cur = DB.execute("UPDATE payments SET status='rejected',decided_at=? WHERE id=? AND status='pending'", (now(), payment_id))
        else:
            cur = DB.execute("UPDATE payments SET status='rejected',decided_at=? WHERE id=? AND status='pending' AND admin_id=?", (now(), payment_id, callback.from_user.id))
        if cur.rowcount != 1: await callback.answer("ℹ️ To'lov allaqachon ko'rib chiqilgan.", show_alert=True); return
        DB.commit(); await callback.answer("❌ To'lov bekor qilindi.")
        try: await callback.message.edit_reply_markup(reply_markup=None)
        except Exception: pass
        try: await bot.send_message(payment["user_id"], "❌ To'lov bekor qilindi.\n\nPrime ochilmadi.")
        except Exception: logger.exception("Rejected payment xabari yuborilmadi")


# =========================================================
# RENDER FREE WEBHOOK SERVER
# Polling ishlatilmaydi: Telegram update'larni HTTPS webhook orqali yuboradi.
# =========================================================
WEBHOOK_PATH = "/telegram/webhook"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()


def get_webhook_url():
    # Render automatically provides RENDER_EXTERNAL_URL for web services.
    # WEBHOOK_URL can be set manually if needed.
    base = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
    if not base:
        render_url = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
        if render_url:
            base = render_url
    if not base:
        hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
        if hostname:
            base = f"https://{hostname}"
    if not base:
        raise RuntimeError(
            "WEBHOOK_URL yoki RENDER_EXTERNAL_URL/RENDER_EXTERNAL_HOSTNAME topilmadi."
        )
    return f"{base}{WEBHOOK_PATH}"


async def health(request):
    return web.Response(text="KinoCinema OK", status=200)


async def telegram_webhook(request):
    # Telegram secret-token verification.
    if WEBHOOK_SECRET:
        received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if received != WEBHOOK_SECRET:
            return web.Response(text="Forbidden", status=403)

    try:
        payload = await request.json()
        update = Update.model_validate(payload)
        await request.app["dispatcher"].feed_update(request.app["bot"], update)
        return web.Response(text="OK", status=200)
    except Exception:
        logger.exception("Telegram webhook update qayta ishlashda xato")
        # Telegram 5xx olsa update'ni qayta yuborishi mumkin.
        return web.Response(text="Internal Server Error", status=500)


async def main():
    global BOT_USERNAME
    init_db()

    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    BOT_USERNAME = me.username or BOT_USERNAME

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    # Default menu: ordinary users only see /start.
    try:
        await bot.set_my_commands([BotCommand(command="start", description="User panel")])
    except Exception:
        logger.exception("Default command menyusini o'rnatishda xato")

    # Existing admins get /admin + /start.
    for row in DB.execute("SELECT user_id FROM admins").fetchall():
        try:
            await set_commands_for_user(bot, row["user_id"])
        except Exception:
            logger.exception("Admin command menyusini o'rnatishda xato: %s", row["user_id"])

    webhook_url = get_webhook_url()
    if not WEBHOOK_SECRET:
        logger.warning("WEBHOOK_SECRET o'rnatilmagan. Render Environment Variables orqali o'rnatish tavsiya etiladi.")

    app = web.Application()
    app["bot"] = bot
    app["dispatcher"] = dp
    app.router.add_get("/", health)
    app.router.add_post(WEBHOOK_PATH, telegram_webhook)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    await bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET or None,
        allowed_updates=dp.resolve_used_update_types(),
        drop_pending_updates=False,
    )

    logger.info("KinoCinema webhook ishga tushdi: %s", webhook_url)
    logger.info("Render port: %s", PORT)

    try:
        # Web serverni doimiy ushlab turamiz. Polling YO'Q.
        await asyncio.Event().wait()
    finally:
        try:
            await bot.delete_webhook(drop_pending_updates=False)
        except Exception:
            logger.exception("Webhook o'chirishda xato")
        await runner.cleanup()
        await bot.session.close()
        DB.close()


if __name__ == "__main__":
    asyncio.run(main())
