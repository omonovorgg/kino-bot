import os
import re
import logging
import asyncio
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, Union, Dict, Any

from aiohttp import web
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command, StateFilter, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, BotCommand, BotCommandScopeChat, BotCommandScopeDefault
)
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

# ==============================================================================
# LOGGING & ENVIRONMENT
# ==============================================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
logger = logging.getLogger("KinoCinemaBot")

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    logger.error("BOT_TOKEN environment variable not set!")

SUPERADMIN_USERNAME = "omono_v"
DEFAULT_CHANNEL = "@uz_kinocinema"
DB_FILE = "kino_bot.db"

# ==============================================================================
# DATABASE MANAGEMENT
# ==============================================================================
class Database:
    def __init__(self, db_file: str):
        self.db_file = db_file
        self.init_db()

    def get_connection(self):
        conn = sqlite3.connect(self.db_file)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Users table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                ref_admin_id INTEGER,
                prime_until DATETIME,
                joined_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """)

            # Admins table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                is_superadmin INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """)

            # Channels table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                channel_id TEXT PRIMARY KEY,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """)

            # Cards table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS cards (
                admin_id INTEGER PRIMARY KEY,
                card_number TEXT NOT NULL,
                card_owner TEXT NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """)

            # Movies table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS movies (
                code TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                file_id TEXT NOT NULL,
                prime INTEGER DEFAULT 0,
                views INTEGER DEFAULT 0,
                added_by INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """)

            # Payments table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                admin_id INTEGER NOT NULL,
                plan TEXT NOT NULL,
                days INTEGER NOT NULL,
                price INTEGER NOT NULL,
                screenshot_file_id TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                decided_at DATETIME
            )
            """)

            # Orders table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                admin_id INTEGER NOT NULL,
                movie_title TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """)

            # Set default channel
            cursor.execute("INSERT OR IGNORE INTO channels (channel_id) VALUES (?)", (DEFAULT_CHANNEL,))
            conn.commit()

    # User operations
    def add_user(self, user_id: int, username: Optional[str], full_name: str, ref_admin_id: Optional[int] = None):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id, ref_admin_id FROM users WHERE user_id = ?", (user_id,))
            user = cursor.fetchone()
            if not user:
                cursor.execute(
                    "INSERT INTO users (user_id, username, full_name, ref_admin_id) VALUES (?, ?, ?, ?)",
                    (user_id, username, full_name, ref_admin_id)
                )
            else:
                cursor.execute(
                    "UPDATE users SET username = ?, full_name = ? WHERE user_id = ?",
                    (username, full_name, user_id)
                )
            conn.commit()

    def get_user(self, user_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            return cursor.fetchone()

    def get_user_by_username(self, username: str):
        clean_un = username.lstrip('@').lower()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE LOWER(username) = ?", (clean_un,))
            return cursor.fetchone()

    def set_prime(self, user_id: int, days: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            user = self.get_user(user_id)
            now = datetime.now()
            
            current_until = None
            if user and user['prime_until']:
                try:
                    current_until = datetime.strptime(user['prime_until'], "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    pass

            if current_until and current_until > now:
                new_until = current_until + timedelta(days=days)
            else:
                new_until = now + timedelta(days=days)

            new_until_str = new_until.strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute("UPDATE users SET prime_until = ? WHERE user_id = ?", (new_until_str, user_id))
            conn.commit()
            return new_until_str

    def is_prime(self, user_id: int) -> bool:
        user = self.get_user(user_id)
        if not user or not user['prime_until']:
            return False
        try:
            until = datetime.strptime(user['prime_until'], "%Y-%m-%d %H:%M:%S")
            return until > datetime.now()
        except ValueError:
            return False

    # Admin operations
    def is_admin(self, user_id: int) -> bool:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
            return cursor.fetchone() is not None

    def is_superadmin(self, user_id: int, username: Optional[str] = None) -> bool:
        if username and username.lstrip('@').lower() == SUPERADMIN_USERNAME.lower():
            return True
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT is_superadmin FROM admins WHERE user_id = ?", (user_id,))
            res = cursor.fetchone()
            return bool(res and res['is_superadmin'])

    def add_admin(self, user_id: int, username: Optional[str], is_superadmin: int = 0):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO admins (user_id, username, is_superadmin) VALUES (?, ?, ?)",
                (user_id, username, is_superadmin)
            )
            conn.commit()

    def remove_admin(self, user_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM admins WHERE user_id = ? AND is_superadmin = 0", (user_id,))
            conn.commit()

    def get_all_admins(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM admins")
            return cursor.fetchall()

    # Channels
    def get_channels(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT channel_id FROM channels")
            return [row['channel_id'] for row in cursor.fetchall()]

    def add_channel(self, channel_id: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO channels (channel_id) VALUES (?)", (channel_id,))
            conn.commit()

    def remove_channel(self, channel_id: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM channels WHERE channel_id = ?", (channel_id,))
            conn.commit()

    # Cards
    def set_card(self, admin_id: int, card_number: str, card_owner: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO cards (admin_id, card_number, card_owner, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                (admin_id, card_number, card_owner)
            )
            conn.commit()

    def get_card(self, admin_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM cards WHERE admin_id = ?", (admin_id,))
            res = cursor.fetchone()
            if res:
                return res
            # Fallback to superadmin card
            cursor.execute("SELECT user_id FROM admins WHERE is_superadmin = 1 LIMIT 1")
            sa = cursor.fetchone()
            if sa and sa['user_id'] != admin_id:
                cursor.execute("SELECT * FROM cards WHERE admin_id = ?", (sa['user_id'],))
                return cursor.fetchone()
            return None

    def delete_card(self, admin_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM cards WHERE admin_id = ?", (admin_id,))
            conn.commit()

    # Movies
    def add_movie(self, code: str, title: str, file_id: str, prime: int, added_by: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO movies (code, title, file_id, prime, added_by) VALUES (?, ?, ?, ?, ?)",
                (code, title, file_id, prime, added_by)
            )
            conn.commit()

    def get_movie(self, code: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM movies WHERE code = ?", (code,))
            return cursor.fetchone()

    def search_movies_by_title(self, title: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM movies WHERE LOWER(title) LIKE ?", (f"%{title.lower()}%",))
            return cursor.fetchall()

    def delete_movie(self, code: str) -> bool:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM movies WHERE code = ?", (code,))
            conn.commit()
            return cursor.rowcount > 0

    def increment_views(self, code: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE movies SET views = views + 1 WHERE code = ?", (code,))
            conn.commit()

    def get_all_movies(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM movies ORDER BY created_at DESC")
            return cursor.fetchall()

    # Payments
    def create_payment(self, user_id: int, admin_id: int, plan: str, days: int, price: int, screenshot_file_id: str) -> int:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO payments (user_id, admin_id, plan, days, price, screenshot_file_id) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, admin_id, plan, days, price, screenshot_file_id)
            )
            conn.commit()
            return cursor.lastrowid

    def get_payment(self, payment_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM payments WHERE id = ?", (payment_id,))
            return cursor.fetchone()

    def update_payment_status(self, payment_id: int, status: str) -> bool:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE payments SET status = ?, decided_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'pending'",
                (status, payment_id)
            )
            conn.commit()
            return cursor.rowcount > 0

    # Orders
    def add_order(self, user_id: int, admin_id: int, movie_title: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO orders (user_id, admin_id, movie_title) VALUES (?, ?, ?)",
                (user_id, admin_id, movie_title)
            )
            conn.commit()

    # Statistics
    def get_admin_stats(self, admin_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) as total FROM users WHERE ref_admin_id = ?", (admin_id,))
            refs = cursor.fetchone()['total']

            cursor.execute("SELECT COUNT(DISTINCT user_id) as total FROM payments WHERE admin_id = ? AND status = 'approved'", (admin_id,))
            prime_users = cursor.fetchone()['total']

            cursor.execute("SELECT COUNT(*) as total, COALESCE(SUM(price), 0) as total_amount FROM payments WHERE admin_id = ? AND status = 'approved'", (admin_id,))
            approved_p = cursor.fetchone()

            cursor.execute("SELECT COUNT(*) as total FROM payments WHERE admin_id = ? AND status = 'pending'", (admin_id,))
            pending_p = cursor.fetchone()['total']

            cursor.execute("SELECT COUNT(*) as total, COALESCE(SUM(views), 0) as total_views FROM movies WHERE added_by = ?", (admin_id,))
            movies_info = cursor.fetchone()

            return {
                "refs": refs,
                "prime_users": prime_users,
                "approved_payments": approved_p['total'],
                "total_earned": approved_p['total_amount'],
                "pending_payments": pending_p,
                "added_movies": movies_info['total'],
                "movie_views": movies_info['total_views']
            }

    def get_global_stats(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) as total FROM users")
            total_users = cursor.fetchone()['total']

            cursor.execute("SELECT COUNT(*) as total FROM users WHERE prime_until > CURRENT_TIMESTAMP")
            active_primes = cursor.fetchone()['total']

            cursor.execute("SELECT COUNT(*) as total, COALESCE(SUM(price), 0) as total_amount FROM payments WHERE status = 'approved'")
            approved_p = cursor.fetchone()

            cursor.execute("SELECT COUNT(*) as total FROM payments WHERE status = 'pending'")
            pending_p = cursor.fetchone()['total']

            cursor.execute("SELECT COUNT(*) as total, COALESCE(SUM(views), 0) as total_views FROM movies")
            movies_info = cursor.fetchone()

            return {
                "total_users": total_users,
                "active_primes": active_primes,
                "approved_payments": approved_p['total'],
                "total_earned": approved_p['total_amount'],
                "pending_payments": pending_p,
                "total_movies": movies_info['total'],
                "total_views": movies_info['total_views']
            }

db = Database(DB_FILE)

# ==============================================================================
# FSM STATES
# ==============================================================================
class SearchState(StatesGroup):
    query = State()

class OrderState(StatesGroup):
    movie_title = State()

class PaymentState(StatesGroup):
    select_plan = State()
    upload_screenshot = State()

class MovieAddState(StatesGroup):
    code = State()
    title = State()
    video = State()
    type = State()

class MovieDeleteState(StatesGroup):
    code = State()

class CardSetState(StatesGroup):
    number = State()
    owner = State()

class AdminAddState(StatesGroup):
    user_identifier = State()

class AdminDeleteState(StatesGroup):
    user_id = State()

class ChannelAddState(StatesGroup):
    channel_id = State()

class ChannelDeleteState(StatesGroup):
    channel_id = State()

# ==============================================================================
# KEYBOARDS
# ==============================================================================
def get_user_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔎 Kino qidirish"), KeyboardButton(text="⭐ Prime status")],
            [KeyboardButton(text="📚 Kinolar ro'yxati"), KeyboardButton(text="📸 Instagramga qaytish")],
            [KeyboardButton(text="🎬 Kino buyurtma qilish"), KeyboardButton(text="🤝 Reklama & Bot olish")]
        ],
        resize_keyboard=True
    )

def get_admin_keyboard(is_super: bool = False):
    kb = [
        [KeyboardButton(text="🎬 Kino qo‘shish"), KeyboardButton(text="🗑 Kino o‘chirish")],
        [KeyboardButton(text="📚 Kinolar"), KeyboardButton(text="📊 Statistika")],
        [KeyboardButton(text="💳 Karta sozlamalari"), KeyboardButton(text="🔗 Mening referralim")]
    ]
    if is_super:
        kb.append([KeyboardButton(text="📢 Kanallar"), KeyboardButton(text="👥 Adminlar")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def get_cancel_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Bekor qilish")]],
        resize_keyboard=True
    )

def get_cancel_back_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⬅️ Orqaga"), KeyboardButton(text="❌ Bekor qilish")]
        ],
        resize_keyboard=True
    )

def get_card_settings_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Karta qo‘shish"), KeyboardButton(text="🔄 Kartani almashtirish")],
            [KeyboardButton(text="👁 Hozirgi kartani ko‘rish"), KeyboardButton(text="🗑 Kartani o‘chirish")],
            [KeyboardButton(text="❌ Bekor qilish")]
        ],
        resize_keyboard=True
    )

def get_channels_settings_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Kanal qo‘shish"), KeyboardButton(text="🗑 Kanal o‘chirish")],
            [KeyboardButton(text="📋 Kanallar ro‘yxati"), KeyboardButton(text="❌ Bekor qilish")]
        ],
        resize_keyboard=True
    )

def get_admins_settings_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Admin qo‘shish"), KeyboardButton(text="🗑 Admin o‘chirish")],
            [KeyboardButton(text="❌ Bekor qilish")]
        ],
        resize_keyboard=True
    )

def get_prime_plans_inline():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="7 kun — 7 000 so‘m", callback_data="buy_prime:7:7000")],
            [InlineKeyboardButton(text="1 oy — 20 000 so‘m", callback_data="buy_prime:30:20000")],
            [InlineKeyboardButton(text="3 oy — 50 000 so‘m", callback_data="buy_prime:90:50000")],
            [InlineKeyboardButton(text="Umrbod — 150 000 so‘m", callback_data="buy_prime:36500:150000")]
        ]
    )

def get_movie_type_inline():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🆓 Oddiy", callback_data="movie_type:0"),
                InlineKeyboardButton(text="⭐ Prime", callback_data="movie_type:1")
            ]
        ]
    )

# ==============================================================================
# FILTERS & MIDDLEWARES
# ==============================================================================
class SuperAdminFilter(Filter):
    async def __call__(self, event: Union[Message, CallbackQuery]) -> bool:
        user = event.from_user
        return db.is_superadmin(user.id, user.username)

class AdminFilter(Filter):
    async def __call__(self, event: Union[Message, CallbackQuery]) -> bool:
        user = event.from_user
        return db.is_admin(user.id) or db.is_superadmin(user.id, user.username)

async def check_channel_subscription(bot: Bot, user_id: int) -> bool:
    if db.is_admin(user_id) or db.is_superadmin(user_id):
        return True
    
    channels = db.get_channels()
    for ch in channels:
        try:
            member = await bot.get_chat_member(chat_id=ch, user_id=user_id)
            if member.status in ['left', 'kicked']:
                return False
        except Exception as e:
            logger.warning(f"Error checking channel {ch} for user {user_id}: {e}")
            # If bot can't check, assume bypass or fail gracefully
            pass
    return True

async def send_subscription_request(event: Union[Message, CallbackQuery], bot: Bot):
    channels = db.get_channels()
    inline_keyboard = []
    for ch in channels:
        url = ch if ch.startswith("https://") else f"https://t.me/{ch.lstrip('@')}"
        inline_keyboard.append([InlineKeyboardButton(text=f"📢 Kanal ({ch})", url=url)])
    inline_keyboard.append([InlineKeyboardButton(text="✅ Obuna bo‘ldim", callback_data="check_subscription")])

    kb = InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
    text = "🔒 Botdan foydalanish uchun kanalga obuna bo‘ling."

    if isinstance(event, Message):
        await event.answer(text, reply_markup=kb)
    else:
        await event.message.answer(text, reply_markup=kb)

# ==============================================================================
# COMMAND & NAVIGATION HANDLERS (HIGH PRIORITY)
# ==============================================================================
router = Router()

async def setup_bot_commands(bot: Bot, user_id: int, is_admin: bool):
    try:
        if is_admin:
            cmds = [
                BotCommand(command="start", description="User rejimiga o'tish"),
                BotCommand(command="admin", description="Admin paneliga o'tish")
            ]
        else:
            cmds = [
                BotCommand(command="start", description="Botni ishga tushirish")
            ]
        await bot.set_my_commands(cmds, scope=BotCommandScopeChat(chat_id=user_id))
    except Exception as e:
        logger.error(f"Error setting commands for {user_id}: {e}")

@router.message(F.text.in_(["❌ Bekor qilish", "⬅️ Orqaga"]))
async def global_cancel_handler(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    is_admin = db.is_admin(message.from_user.id) or db.is_superadmin(message.from_user.id, message.from_user.username)
    await setup_bot_commands(bot, message.from_user.id, is_admin)
    await message.answer("Amal bekor qilindi.", reply_markup=get_user_keyboard())

@router.message(CommandStart())
async def command_start_handler(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    user = message.from_user
    
    # Process referral
    ref_admin_id = None
    args = message.text.split(maxsplit=1)
    if len(args) > 1 and args[1].startswith("admin_"):
        try:
            possible_id = int(args[1].replace("admin_", ""))
            if db.is_admin(possible_id):
                ref_admin_id = possible_id
        except ValueError:
            pass

    # Ensure Superadmin in DB
    if user.username and user.username.lstrip('@').lower() == SUPERADMIN_USERNAME.lower():
        db.add_admin(user.id, user.username, is_superadmin=1)

    db.add_user(user.id, user.username, user.full_name, ref_admin_id)
    is_admin = db.is_admin(user.id) or db.is_superadmin(user.id, user.username)
    await setup_bot_commands(bot, user.id, is_admin)

    if not await check_channel_subscription(bot, user.id):
        await send_subscription_request(message, bot)
        return

    await message.answer(
        f"Xush kelibsiz, {user.full_name}!\nKinoCinema botiga xush kelibsiz. Kerakli bo‘limni tanlang:",
        reply_markup=get_user_keyboard()
    )

@router.message(Command("admin"))
async def command_admin_handler(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    user = message.from_user
    
    # Check superadmin setup dynamically
    if user.username and user.username.lstrip('@').lower() == SUPERADMIN_USERNAME.lower():
        db.add_admin(user.id, user.username, is_superadmin=1)

    is_super = db.is_superadmin(user.id, user.username)
    is_adm = db.is_admin(user.id) or is_super

    if not is_adm:
        await message.answer("❌ Siz admin emassiz.")
        return

    await setup_bot_commands(bot, user.id, True)
    await message.answer("🔑 Admin paneliga xush kelibsiz!", reply_markup=get_admin_keyboard(is_super))

@router.callback_query(F.data == "check_subscription")
async def check_subscription_callback(callback: CallbackQuery, bot: Bot):
    if await check_channel_subscription(bot, callback.from_user.id):
        await callback.message.delete()
        await callback.message.answer("✅ Obuna tasdiqlandi! Botdan foydalanishingiz mumkin.", reply_markup=get_user_keyboard())
    else:
        await callback.answer("❌ Siz hali barcha kanallarga obuna bo‘lmadingiz!", show_alert=True)

# ==============================================================================
# USER FUNCTIONALITIES & FSM
# ==============================================================================

# Search Movie
@router.message(StateFilter(None), F.text == "🔎 Kino qidirish")
async def user_search_start(message: Message, state: FSMContext, bot: Bot):
    if not await check_channel_subscription(bot, message.from_user.id):
        await send_subscription_request(message, bot)
        return
    await state.set_state(SearchState.query)
    await message.answer("🎬 Kino kodi (3 xonali raqam) yoki kino nomini kiriting:", reply_markup=get_cancel_keyboard())

@router.message(SearchState.query)
async def user_search_process(message: Message, state: FSMContext, bot: Bot):
    if not await check_channel_subscription(bot, message.from_user.id):
        await state.clear()
        await send_subscription_request(message, bot)
        return

    query = message.text.strip()
    await state.clear()

    # Code search check
    if re.match(r"^\d{3}$", query):
        movie = db.get_movie(query)
        if not movie:
            await message.answer("❌ Bunday kodli kino topilmadi.", reply_markup=get_user_keyboard())
            return
        await send_movie_to_user(message, movie)
    else:
        movies = db.search_movies_by_title(query)
        if not movies:
            await message.answer("❌ Bunday nomli kino topilmadi.", reply_markup=get_user_keyboard())
            return
        
        if len(movies) == 1:
            await send_movie_to_user(message, movies[0])
        else:
            text = "🔎 **Topilgan kinolar:**\n\n"
            for m in movies[:10]:
                p_tag = "⭐ " if m['prime'] else ""
                text += f"{p_tag}**{m['title']}** — Kod: `{m['code']}`\n"
            text += "\nKinoni ko‘rish uchun 3 xonali kodini yuboring."
            await message.answer(text, parse_mode="Markdown", reply_markup=get_user_keyboard())

async def send_movie_to_user(message: Message, movie):
    user_id = message.from_user.id
    if movie['prime'] and not db.is_prime(user_id) and not db.is_admin(user_id):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⭐ Prime olish", callback_data="open_prime_info")]
        ])
        await message.answer("🔒 Bu kino faqat Prime/VIP foydalanuvchilar uchun.", reply_markup=kb)
        return

    db.increment_views(movie['code'])
    caption = f"🎬 **{movie['title']}**\n🔢 Kod: `{movie['code']}`\n\n🍿 Yoqimli tomosha!"
    try:
        await message.answer_video(video=movie['file_id'], caption=caption, parse_mode="Markdown", reply_markup=get_user_keyboard())
    except Exception as e:
        logger.error(f"Error sending video: {e}")
        await message.answer("❌ Videoni yuklashda xatolik yuz berdi.", reply_markup=get_user_keyboard())

# Prime System
@router.message(StateFilter(None), F.text == "⭐ Prime status")
async def user_prime_status(message: Message, state: FSMContext, bot: Bot):
    if not await check_channel_subscription(bot, message.from_user.id):
        await send_subscription_request(message, bot)
        return

    user = db.get_user(message.from_user.id)
    is_p = db.is_prime(message.from_user.id)

    if is_p:
        until = user['prime_until']
        text = f"⭐ **Sizning Prime statustingiz faol!**\n\n📅 Amal qilish muddati: `{until}` gacha."
        await message.answer(text, parse_mode="Markdown", reply_markup=get_user_keyboard())
    else:
        text = "⭐ **Prime Status**\n\nPrime obunasi orqali eksklyuziv kinolarni cheklovsiz ko'rishingiz mumkin!\n\nTariflar bilan tanishing va sotib oling:"
        await message.answer(text, reply_markup=get_prime_plans_inline())

@router.callback_query(F.data == "open_prime_info")
async def cb_open_prime_info(callback: CallbackQuery):
    text = "⭐ **Prime Status**\n\nTariflar bilan tanishing va sotib oling:"
    await callback.message.answer(text, reply_markup=get_prime_plans_inline())
    await callback.answer()

@router.callback_query(F.data.startswith("buy_prime:"))
async def cb_buy_prime(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    days = int(parts[1])
    price = int(parts[2])
    plan_name = "7 kun" if days == 7 else "1 oy" if days == 30 else "3 oy" if days == 90 else "Umrbod"

    user = db.get_user(callback.from_user.id)
    ref_admin_id = user['ref_admin_id'] if user and user['ref_admin_id'] else None

    # Get responsible admin card
    card_info = None
    target_admin_id = None
    if ref_admin_id:
        card_info = db.get_card(ref_admin_id)
        target_admin_id = ref_admin_id

    if not card_info:
        # Fallback to superadmin
        sa = None
        for adm in db.get_all_admins():
            if adm['is_superadmin']:
                sa = adm['user_id']
                break
        if sa:
            card_info = db.get_card(sa)
            target_admin_id = sa

    if not card_info:
        await callback.answer("❌ To'lov kartasi hali sozlanmagan. Iltimos keyinroq urinib ko'ring.", show_alert=True)
        return

    await state.set_state(PaymentState.upload_screenshot)
    await state.update_data(days=days, price=price, plan=plan_name, target_admin_id=target_admin_id)

    text = (
        f"💳 **To'lov ma'lumotlari:**\n\n"
        f"Tarif: **{plan_name}**\n"
        f"Summa: **{price:,} so'm**\n\n"
        f"💳 Karta: `{card_info['card_number']}`\n"
        f"👤 Egasining ismi: **{card_info['card_owner']}**\n\n"
        f"To'lovni amalga oshirgach, to'lov chekini (screenshot/rasm) shu yerga yuboring."
    )
    await callback.message.answer(text, parse_mode="Markdown", reply_markup=get_cancel_keyboard())
    await callback.answer()

@router.message(PaymentState.upload_screenshot, F.photo)
async def process_payment_screenshot(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()

    photo_id = message.photo[-1].file_id
    user = message.from_user
    target_admin_id = data['target_admin_id']

    payment_id = db.create_payment(
        user_id=user.id,
        admin_id=target_admin_id,
        plan=data['plan'],
        days=data['days'],
        price=data['price'],
        screenshot_file_id=photo_id
    )

    # Send screenshot to responsible admin
    admin_text = (
        f"📥 **Yangi to'lov cheki!** (ID: {payment_id})\n\n"
        f"👤 Foydalanuvchi: {user.full_name} (@{user.username or 'yoq'})\n"
        f"🆔 ID: `{user.id}`\n"
        f"📦 Tarif: **{data['plan']}**\n"
        f"💰 Summa: **{data['price']:,} so'm**\n"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"pay_app:{payment_id}"),
                InlineKeyboardButton(text="❌ Bekor qilish", callback_data=f"pay_rej:{payment_id}")
            ]
        ]
    )

    try:
        await bot.send_photo(chat_id=target_admin_id, photo=photo_id, caption=admin_text, parse_mode="Markdown", reply_markup=kb)
        await message.answer("✅ To'lov cheki adminga yuborildi. Tekshirilgach, Prime statusingiz faollashtiriladi!", reply_markup=get_user_keyboard())
    except Exception as e:
        logger.error(f"Failed to send payment to admin {target_admin_id}: {e}")
        await message.answer("❌ Adminga xabar yuborishda xatolik. Keyinroq qayta urinib ko'ring.", reply_markup=get_user_keyboard())

@router.message(PaymentState.upload_screenshot)
async def process_payment_screenshot_invalid(message: Message):
    await message.answer("❌ Iltimos, to'lov chekini rasm (screenshot) ko'rinishida yuboring.", reply_markup=get_cancel_keyboard())

# Payment Verification Callbacks
@router.callback_query(F.data.startswith("pay_app:"))
async def cb_approve_payment(callback: CallbackQuery, bot: Bot):
    payment_id = int(callback.data.split(":")[1])
    payment = db.get_payment(payment_id)

    if not payment:
        await callback.answer("❌ To'lov topilmadi.", show_alert=True)
        return

    admin_id = callback.from_user.id
    is_super = db.is_superadmin(admin_id, callback.from_user.username)

    if payment['admin_id'] != admin_id and not is_super:
        await callback.answer("❌ Siz bu to'lovni tasdiqlash huquqiga ega emassiz!", show_alert=True)
        return

    if payment['status'] != 'pending':
        await callback.answer("❌ Bu to'lov allaqachon ko'rib chiqilgan!", show_alert=True)
        return

    # Direct activation without asking extra confirmation
    success = db.update_payment_status(payment_id, 'approved')
    if success:
        new_until = db.set_prime(payment['user_id'], payment['days'])
        
        # Remove buttons & edit caption
        new_caption = callback.message.caption + f"\n\n✅ **TASDIQLANDI** (Admin: {callback.from_user.full_name})"
        await callback.message.edit_caption(caption=new_caption, parse_mode="Markdown", reply_markup=None)

        # Notify user
        try:
            await bot.send_message(
                chat_id=payment['user_id'],
                text=f"🎉 **To'lovingiz tasdiqlandi!**\n\n⭐ Prime status {new_until} gacha faollashtirildi. Yoqimli tomosha!"
            )
        except Exception as e:
            logger.error(f"Failed to notify user {payment['user_id']}: {e}")

        await callback.answer("✅ To'lov tasdiqlandi va Prime ochildi!", show_alert=True)

@router.callback_query(F.data.startswith("pay_rej:"))
async def cb_reject_payment(callback: CallbackQuery, bot: Bot):
    payment_id = int(callback.data.split(":")[1])
    payment = db.get_payment(payment_id)

    if not payment:
        await callback.answer("❌ To'lov topilmadi.", show_alert=True)
        return

    admin_id = callback.from_user.id
    is_super = db.is_superadmin(admin_id, callback.from_user.username)

    if payment['admin_id'] != admin_id and not is_super:
        await callback.answer("❌ Siz bu to'lovni rad etish huquqiga ega emassiz!", show_alert=True)
        return

    if payment['status'] != 'pending':
        await callback.answer("❌ Bu to'lov allaqachon ko'rib chiqilgan!", show_alert=True)
        return

    success = db.update_payment_status(payment_id, 'rejected')
    if success:
        new_caption = callback.message.caption + f"\n\n❌ **RAD ETILDI** (Admin: {callback.from_user.full_name})"
        await callback.message.edit_caption(caption=new_caption, parse_mode="Markdown", reply_markup=None)

        try:
            await bot.send_message(
                chat_id=payment['user_id'],
                text="❌ **To'lovingiz rad etildi.**\nIltimos to'lov chekini to'g'ri yuborganingizni tekshiring yoki adminga murojaat qiling."
            )
        except Exception as e:
            logger.error(f"Failed to notify user {payment['user_id']}: {e}")

        await callback.answer("❌ To'lov rad etildi.", show_alert=True)

# Movie List for User
@router.message(StateFilter(None), F.text == "📚 Kinolar ro'yxati")
async def user_movie_list(message: Message, state: FSMContext, bot: Bot):
    if not await check_channel_subscription(bot, message.from_user.id):
        await send_subscription_request(message, bot)
        return

    movies = db.get_all_movies()
    if not movies:
        await message.answer("📚 Baza hozircha bo'sh.", reply_markup=get_user_keyboard())
        return

    text = "📚 **Mavjud kinolar ro'yxati:**\n\n"
    for m in movies[:30]:  # Limit for message length
        p_tag = "⭐ " if m['prime'] else ""
        text += f"{p_tag}**{m['title']}** — Kod: `{m['code']}`\n"
    text += "\nKinoni ko'rish uchun 3 xonali kodini kiriting."
    await message.answer(text, parse_mode="Markdown", reply_markup=get_user_keyboard())

# Other User Buttons
@router.message(StateFilter(None), F.text == "📸 Instagramga qaytish")
async def user_instagram(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸 Instagram sahifamiz", url="https://www.instagram.com/oemovie/")]
    ])
    await message.answer("Bizning Instagram sahifamizga o'ting:", reply_markup=kb)

@router.message(StateFilter(None), F.text == "🎬 Kino buyurtma qilish")
async def user_order_start(message: Message, state: FSMContext, bot: Bot):
    if not await check_channel_subscription(bot, message.from_user.id):
        await send_subscription_request(message, bot)
        return

    await state.set_state(OrderState.movie_title)
    await message.answer("Qaysi kinoni izlayotganingizni yozing:", reply_markup=get_cancel_keyboard())

@router.message(OrderState.movie_title)
async def user_order_process(message: Message, state: FSMContext, bot: Bot):
    movie_title = message.text.strip()
    await state.clear()

    user = db.get_user(message.from_user.id)
    ref_admin = user['ref_admin_id'] if user and user['ref_admin_id'] else None

    target_admin = ref_admin if ref_admin else None
    if not target_admin:
        for adm in db.get_all_admins():
            if adm['is_superadmin']:
                target_admin = adm['user_id']
                break

    db.add_order(message.from_user.id, target_admin or 0, movie_title)

    if target_admin:
        try:
            order_text = (
                f"🎬 **Yangi kino buyurtmasi!**\n\n"
                f"👤 Foydalanuvchi: {message.from_user.full_name} (@{message.from_user.username or 'yoq'})\n"
                f"🆔 ID: `{message.from_user.id}`\n"
                f"🎥 Kino nomi: **{movie_title}**"
            )
            await bot.send_message(chat_id=target_admin, text=order_text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Error sending order to admin: {e}")

    await message.answer("✅ Buyurtmangiz adminga yuborildi.", reply_markup=get_user_keyboard())

@router.message(StateFilter(None), F.text == "🤝 Reklama & Bot olish")
async def user_reklama(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 @omono_v bilan bog'lanish", url="https://t.me/omono_v")]
    ])
    await message.answer("Reklama va bot xizmati bo'yicha bog'lanish uchun:", reply_markup=kb)

# Default fallback for quick code input when not in state
@router.message(StateFilter(None), F.text.regexp(r"^\d{3}$"))
async def direct_code_input_handler(message: Message, bot: Bot):
    if not await check_channel_subscription(bot, message.from_user.id):
        await send_subscription_request(message, bot)
        return

    code = message.text.strip()
    movie = db.get_movie(code)
    if not movie:
        await message.answer("❌ Bunday kodli kino topilmadi.", reply_markup=get_user_keyboard())
        return
    await send_movie_to_user(message, movie)

# ==============================================================================
# ADMIN FUNCTIONALITIES & FSM
# ==============================================================================

# Add Movie
@router.message(StateFilter(None), F.text == "🎬 Kino qo‘shish", AdminFilter())
async def admin_add_movie_start(message: Message, state: FSMContext):
    await state.set_state(MovieAddState.code)
    await message.answer("1/4 Kino kodini yuboring (FAQAT 3 xonali raqam, masalan: 327):", reply_markup=get_cancel_keyboard())

@router.message(MovieAddState.code)
async def admin_add_movie_code(message: Message, state: FSMContext):
    code = message.text.strip()
    if not re.match(r"^\d{3}$", code):
        await message.answer("❌ Kino kodi FAQAT 3 xonali raqam bo'lishi kerak (masalan: 327). Qayta kiriting:", reply_markup=get_cancel_keyboard())
        return

    if db.get_movie(code):
        await message.answer("❌ Bu kod allaqachon mavjud. Boshqa kod kiriting:", reply_markup=get_cancel_keyboard())
        return

    await state.update_data(code=code)
    await state.set_state(MovieAddState.title)
    await message.answer("2/4 Kino nomini yuboring:", reply_markup=get_cancel_keyboard())

@router.message(MovieAddState.title)
async def admin_add_movie_title(message: Message, state: FSMContext):
    title = message.text.strip()
    if not title:
        await message.answer("❌ Kino nomi bo'sh bo'lishi mumkin emas. Qayta kiriting:", reply_markup=get_cancel_keyboard())
        return

    await state.update_data(title=title)
    await state.set_state(MovieAddState.video)
    await message.answer("3/4 Telegram videoni yuboring:", reply_markup=get_cancel_keyboard())

@router.message(MovieAddState.video, F.video)
async def admin_add_movie_video(message: Message, state: FSMContext):
    file_id = message.video.file_id
    await state.update_data(file_id=file_id)
    await state.set_state(MovieAddState.type)
    await message.answer("4/4 Kino turini tanlang:", reply_markup=get_movie_type_inline())

@router.message(MovieAddState.video)
async def admin_add_movie_video_invalid(message: Message):
    await message.answer("❌ Iltimos, video yuboring.", reply_markup=get_cancel_keyboard())

@router.callback_query(MovieAddState.type, F.data.startswith("movie_type:"))
async def admin_add_movie_type_cb(callback: CallbackQuery, state: FSMContext):
    prime = int(callback.data.split(":")[1])
    data = await state.get_data()
    await state.clear()

    db.add_movie(
        code=data['code'],
        title=data['title'],
        file_id=data['file_id'],
        prime=prime,
        added_by=callback.from_user.id
    )

    is_super = db.is_superadmin(callback.from_user.id, callback.from_user.username)
    await callback.message.answer(f"✅ **{data['title']}** (Kod: `{data['code']}`) muvaffaqiyatli saqlandi!", parse_mode="Markdown", reply_markup=get_admin_keyboard(is_super))
    await callback.answer()

# Delete Movie
@router.message(StateFilter(None), F.text == "🗑 Kino o‘chirish", AdminFilter())
async def admin_delete_movie_start(message: Message, state: FSMContext):
    await state.set_state(MovieDeleteState.code)
    await message.answer("O'chirmoqchi bo'lgan kinoning 3 xonali kodini kiriting:", reply_markup=get_cancel_keyboard())

@router.message(MovieDeleteState.code)
async def admin_delete_movie_process(message: Message, state: FSMContext):
    code = message.text.strip()
    await state.clear()

    is_super = db.is_superadmin(message.from_user.id, message.from_user.username)
    if not re.match(r"^\d{3}$", code):
        await message.answer("❌ Noto'g'ri kod formati.", reply_markup=get_admin_keyboard(is_super))
        return

    if db.delete_movie(code):
        await message.answer(f"✅ Kod `{code}` bo'lgan kino o'chirildi.", parse_mode="Markdown", reply_markup=get_admin_keyboard(is_super))
    else:
        await message.answer("❌ Kino topilmadi.", reply_markup=get_admin_keyboard(is_super))

# Admin Movies List
@router.message(StateFilter(None), F.text == "📚 Kinolar", AdminFilter())
async def admin_movies_list(message: Message):
    movies = db.get_all_movies()
    if not movies:
        await message.answer("📚 Baza hozircha bo'sh.")
        return

    text = "📚 **Barcha kinolar ro'yxati:**\n\n"
    for m in movies[:30]:
        p_tag = "⭐ " if m['prime'] else ""
        text += f"{p_tag}**{m['title']}** — Kod: `{m['code']}` (Ko'rishlar: {m['views']})\n"
    await message.answer(text, parse_mode="Markdown")

# Admin Card Settings
@router.message(StateFilter(None), F.text == "💳 Karta sozlamalari", AdminFilter())
async def admin_card_menu(message: Message):
    await message.answer("💳 **Karta sozlamalari bo'limi:**", reply_markup=get_card_settings_keyboard())

@router.message(StateFilter(None), F.text == "👁 Hozirgi kartani ko‘rish", AdminFilter())
async def admin_card_view(message: Message):
    card = db.get_card(message.from_user.id)
    if card and card['admin_id'] == message.from_user.id:
        text = f"💳 **Sizning kartangiz:\n\nNomeri: `{card['card_number']}`\nEga: **"
    else:
        text = "❌ Sizda hali karta sozlanmagan."
    await message.answer(text, parse_mode="Markdown", reply_markup=get_card_settings_keyboard())

@router.message(StateFilter(None), F.text.in_(["➕ Karta qo‘shish", "🔄 Kartani almashtirish"]), AdminFilter())
async def admin_card_set_start(message: Message, state: FSMContext):
    await state.set_state(CardSetState.number)
    await message.answer("16 xonali karta raqamini kiriting (masalan: 9860600435412504):", reply_markup=get_cancel_keyboard())

@router.message(CardSetState.number)
async def admin_card_set_number(message: Message, state: FSMContext):
    raw_num = message.text.replace(" ", "").strip()
    if not re.match(r"^\d{16}$", raw_num):
        await message.answer("❌ Karta raqami FAQAT 16 ta raqamdan iborat bo'lishi kerak! Qayta kiriting:", reply_markup=get_cancel_keyboard())
        return

    await state.update_data(card_number=raw_num)
    await state.set_state(CardSetState.owner)
    await message.answer("Karta egasining ism-familiyasini kiriting:", reply_markup=get_cancel_keyboard())

@router.message(CardSetState.owner)
async def admin_card_set_owner(message: Message, state: FSMContext):
    owner = message.text.strip()
    data = await state.get_data()
    await state.clear()

    db.set_card(message.from_user.id, data['card_number'], owner)
    await message.answer("✅ Karta ma'lumotlari muvaffaqiyatli saqlandi!", reply_markup=get_card_settings_keyboard())

@router.message(StateFilter(None), F.text == "🗑 Kartani o‘chirish", AdminFilter())
async def admin_card_delete(message: Message):
    db.delete_card(message.from_user.id)
    await message.answer("✅ Kartangiz o'chirildi.", reply_markup=get_card_settings_keyboard())

# Referral Link
@router.message(StateFilter(None), F.text == "🔗 Mening referralim", AdminFilter())
async def admin_referral_link(message: Message, bot: Bot):
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=admin_{message.from_user.id}"
    text = f"🔗 **Sizning shaxsiy referral havolangiz:**\n\n`{ref_link}`\n\nUshbu havola orqali kirgan foydalanuvchilar to'lov qilganda to'lov cheki directly sizga keladi!"
    await message.answer(text, parse_mode="Markdown")

# Admin Statistics
@router.message(StateFilter(None), F.text == "📊 Statistika", AdminFilter())
async def admin_statistics(message: Message):
    user_id = message.from_user.id
    is_super = db.is_superadmin(user_id, message.from_user.username)

    if is_super:
        stats = db.get_global_stats()
        text = (
            f"📊 **UMUMIY GLOBAL STATISTIKA**\n\n"
            f"👥 Jami foydalanuvchilar: **{stats['total_users']}**\n"
            f"⭐ Faol Prime foydalanuvchilar: **{stats['active_primes']}**\n"
            f"✅ Tasdiqlangan to'lovlar soni: **{stats['approved_payments']}**\n"
            f"💰 Jami tushum: **{stats['total_earned']:,} so'm**\n"
            f"⏳ Kutilayotgan to'lovlar: **{stats['pending_payments']}**\n"
            f"🎬 Jami kinolar: **{stats['total_movies']}**\n"
            f"👁 Jami ko'rishlar: **{stats['total_views']}**"
        )
    else:
        stats = db.get_admin_stats(user_id)
        text = (
            f"📊 **SHAXSIY STATISTIKA**\n\n"
            f"👥 Referralingizdan kirganlar: **{stats['refs']}**\n"
            f"⭐ Prime sotib olganlar: **{stats['prime_users']}**\n"
            f"✅ Tasdiqlangan to'lovlaringiz: **{stats['approved_payments']}**\n"
            f"💰 Jami tushumingiz: **{stats['total_earned']:,} so'm**\n"
            f"⏳ Kutilayotgan to'lovlaringiz: **{stats['pending_payments']}**\n"
            f"🎬 Qo'shgan kinolaringiz: **{stats['added_movies']}**\n"
            f"👁 Kinolaringiz ko'rilishi: **{stats['movie_views']}**"
        )
    await message.answer(text, parse_mode="Markdown")

# Superadmin: Manage Channels
@router.message(StateFilter(None), F.text == "📢 Kanallar", SuperAdminFilter())
async def superadmin_channels_menu(message: Message):
    await message.answer("📢 **Kanallarni boshqarish bo'limi:**", reply_markup=get_channels_settings_keyboard())

@router.message(StateFilter(None), F.text == "📋 Kanallar ro‘yxati", SuperAdminFilter())
async def superadmin_channels_list(message: Message):
    channels = db.get_channels()
    if not channels:
        await message.answer("📢 Majburiy kanallar yo'q.")
        return
    text = "📋 **Majburiy kanallar:**\n\n" + "\n".join([f"• `{ch}`" for ch in channels])
    await message.answer(text, parse_mode="Markdown")

@router.message(StateFilter(None), F.text == "➕ Kanal qo‘shish", SuperAdminFilter())
async def superadmin_channel_add_start(message: Message, state: FSMContext):
    await state.set_state(ChannelAddState.channel_id)
    await message.answer("Kanal username'ini yuboring (masalan: @uz_kinocinema):", reply_markup=get_cancel_keyboard())

@router.message(ChannelAddState.channel_id, SuperAdminFilter())
async def superadmin_channel_add_process(message: Message, state: FSMContext, bot: Bot):
    ch_id = message.text.strip()
    if not ch_id.startswith("@"):
        ch_id = f"@{ch_id}"

    # Verify bot is admin in channel
    try:
        member = await bot.get_chat_member(chat_id=ch_id, user_id=bot.id)
        if member.status not in ['administrator', 'creator']:
            await message.answer("❌ Bot bu kanalda admin emas! Avval botni kanalda admin qiling.", reply_markup=get_channels_settings_keyboard())
            await state.clear()
            return
    except Exception as e:
        await message.answer(f"❌ Kanalni tekshirishda xatolik yuz berdi: {e}\nBot kanalda admin ekanligini va username to'g'riligini tekshiring.", reply_markup=get_channels_settings_keyboard())
        await state.clear()
        return

    db.add_channel(ch_id)
    await state.clear()
    await message.answer(f"✅ Kanal `{ch_id}` muvaffaqiyatli qo'shildi!", parse_mode="Markdown", reply_markup=get_channels_settings_keyboard())

@router.message(StateFilter(None), F.text == "🗑 Kanal o‘chirish", SuperAdminFilter())
async def superadmin_channel_del_start(message: Message, state: FSMContext):
    await state.set_state(ChannelDeleteState.channel_id)
    await message.answer("O'chirmoqchi bo'lgan kanal username'ini yuboring (masalan: @uz_kinocinema):", reply_markup=get_cancel_keyboard())

@router.message(ChannelDeleteState.channel_id, SuperAdminFilter())
async def superadmin_channel_del_process(message: Message, state: FSMContext):
    ch_id = message.text.strip()
    if not ch_id.startswith("@"):
        ch_id = f"@{ch_id}"

    db.remove_channel(ch_id)
    await state.clear()
    await message.answer(f"✅ Kanal `{ch_id}` o'chirildi.", parse_mode="Markdown", reply_markup=get_channels_settings_keyboard())

# Superadmin: Manage Admins
@router.message(StateFilter(None), F.text == "👥 Adminlar", SuperAdminFilter())
async def superadmin_admins_menu(message: Message):
    admins = db.get_all_admins()
    text = "👥 **Mavjud adminlar ro'yxati:**\n\n"
    for a in admins:
        role = "Superadmin" if a['is_superadmin'] else "Admin"
        text += f"• ID: `{a['user_id']}` | Username: @{a['username'] or 'yoq'} ({role})\n"
    await message.answer(text, parse_mode="Markdown", reply_markup=get_admins_settings_keyboard())

@router.message(StateFilter(None), F.text == "➕ Admin qo‘shish", SuperAdminFilter())
async def superadmin_add_admin_start(message: Message, state: FSMContext):
    await state.set_state(AdminAddState.user_identifier)
    await message.answer("Yangi adminning Telegram ID si yoki @username'ini yuboring:", reply_markup=get_cancel_keyboard())

@router.message(AdminAddState.user_identifier, SuperAdminFilter())
async def superadmin_add_admin_process(message: Message, state: FSMContext, bot: Bot):
    identifier = message.text.strip()
    await state.clear()

    target_user = None
    if identifier.isdigit():
        target_user = db.get_user(int(identifier))
    elif identifier.startswith("@"):
        target_user = db.get_user_by_username(identifier)

    if not target_user:
        await message.answer("❌ Foydalanuvchi topilmadi. U botga avval /start yuborgan bo'lishi kerak.", reply_markup=get_admins_settings_keyboard())
        return

    db.add_admin(target_user['user_id'], target_user['username'])
    await setup_bot_commands(bot, target_user['user_id'], True)

    await message.answer(f"✅ Foydalanuvchi `{target_user['user_id']}` admin qilib tayinlandi!", parse_mode="Markdown", reply_markup=get_admins_settings_keyboard())

@router.message(StateFilter(None), F.text == "🗑 Admin o‘chirish", SuperAdminFilter())
async def superadmin_del_admin_start(message: Message, state: FSMContext):
    await state.set_state(AdminDeleteState.user_id)
    await message.answer("O'chirmoqchi bo'lgan adminning Telegram ID sini kiriting:", reply_markup=get_cancel_keyboard())

@router.message(AdminDeleteState.user_id, SuperAdminFilter())
async def superadmin_del_admin_process(message: Message, state: FSMContext, bot: Bot):
    val = message.text.strip()
    await state.clear()

    if not val.isdigit():
        await message.answer("❌ Telegram ID raqam bo'lishi kerak.", reply_markup=get_admins_settings_keyboard())
        return

    target_id = int(val)
    if db.is_superadmin(target_id):
        await message.answer("❌ Superadminni o'chirib bo'lmaydi!", reply_markup=get_admins_settings_keyboard())
        return

    db.remove_admin(target_id)
    await setup_bot_commands(bot, target_id, False)

    await message.answer(f"✅ Admin `{target_id}` muvaffaqiyatli o'chirildi.", parse_mode="Markdown", reply_markup=get_admins_settings_keyboard())

# ==============================================================================
# FALLBACK HANDLERS
# ==============================================================================
@router.message(StateFilter(None))
async def global_fallback_no_state(message: Message, bot: Bot):
    if not await check_channel_subscription(bot, message.from_user.id):
        await send_subscription_request(message, bot)
        return
    await message.answer("❌ Noto'g'ri buyruq kiritildi. Iltimos, menyudan foydalaning.", reply_markup=get_user_keyboard())

# ==============================================================================
# RENDER HEALTH CHECK SERVER & BOT LAUNCH
# ==============================================================================
async def handle_health_check(request):
    return web.Response(text="KinoCinema OK", status=200)

async def start_health_server():
    app = web.Application()
    app.router.add_get("/", handle_health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Health server listening on http://0.0.0.0:{port}")

async def main():
    if not TOKEN:
        logger.error("Token is missing! Stopping...")
        return

    bot = Bot(token=TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    # Start dummy web server for Render health checks
    await start_health_server()

    # Default Bot Commands setup
    await bot.set_my_commands([BotCommand(command="start", description="Botni ishga tushirish")], scope=BotCommandScopeDefault())

    logger.info("Bot starting in long polling mode...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
