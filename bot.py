import os
import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from html import escape

from aiohttp import web
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
)
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder


TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN Environment Variable topilmadi.")

SUPERADMIN_USERNAME = "omono_v"
SUPERADMIN_ID = None

BOT_USERNAME = "kinocinemauz_bot"

INSTAGRAM_URL = "https://www.instagram.com/oemovie/"
DEFAULT_CHANNEL = "@uz_kinocinema"

DB_PATH = os.getenv("DB_PATH", "kinocinema.db")
PORT = int(os.getenv("PORT", "10000"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("KinoCinema")


# ============================================================
# DATABASE
# ============================================================

db = sqlite3.connect(
    DB_PATH,
    check_same_thread=False,
)

db.row_factory = sqlite3.Row

db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA foreign_keys=ON")


def now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def init_db():
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            prime_until TEXT,
            referred_by INTEGER,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            added_by INTEGER,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            title TEXT
        );

        CREATE TABLE IF NOT EXISTS cards (
            admin_id INTEGER PRIMARY KEY,
            card_number TEXT NOT NULL,
            card_owner TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS movies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            file_id TEXT NOT NULL,
            prime_only INTEGER NOT NULL DEFAULT 0,
            views INTEGER NOT NULL DEFAULT 0,
            added_by INTEGER,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            admin_id INTEGER NOT NULL,
            plan TEXT NOT NULL,
            days INTEGER,
            price INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            screenshot_file_id TEXT,
            created_at TEXT NOT NULL,
            decided_at TEXT
        );

        CREATE TABLE IF NOT EXISTS referrals (
            user_id INTEGER PRIMARY KEY,
            admin_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            admin_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )

    db.execute(
        """
        INSERT OR IGNORE INTO channels(username, title)
        VALUES(?, ?)
        """,
        (
            DEFAULT_CHANNEL,
            "Uz KinoCinema",
        ),
    )

    db.commit()


def get_user(user_id):
    return db.execute(
        "SELECT * FROM users WHERE user_id=?",
        (user_id,),
    ).fetchone()


def upsert_user(tg_user):
    existing = get_user(tg_user.id)

    if existing:
        db.execute(
            """
            UPDATE users
            SET username=?,
                first_name=?
            WHERE user_id=?
            """,
            (
                tg_user.username,
                tg_user.first_name,
                tg_user.id,
            ),
        )
    else:
        db.execute(
            """
            INSERT INTO users(
                user_id,
                username,
                first_name,
                created_at
            )
            VALUES(?,?,?,?)
            """,
            (
                tg_user.id,
                tg_user.username,
                tg_user.first_name,
                now(),
            ),
        )

    db.commit()


def is_admin(user_id):
    return (
        db.execute(
            "SELECT 1 FROM admins WHERE user_id=?",
            (user_id,),
        ).fetchone()
        is not None
    )


def is_superadmin(user_id):
    if SUPERADMIN_ID is not None and user_id == SUPERADMIN_ID:
        return True

    row = db.execute(
        """
        SELECT 1
        FROM admins
        WHERE user_id=?
          AND lower(username)=?
        """,
        (
            user_id,
            SUPERADMIN_USERNAME.lower(),
        ),
    ).fetchone()

    return row is not None


def add_admin(user_id, username, added_by):
    db.execute(
        """
        INSERT OR REPLACE INTO admins(
            user_id,
            username,
            added_by,
            created_at
        )
        VALUES(?,?,?,?)
        """,
        (
            user_id,
            username,
            added_by,
            now(),
        ),
    )

    db.commit()


def remove_admin(user_id):
    db.execute(
        "DELETE FROM admins WHERE user_id=?",
        (user_id,),
    )

    db.commit()


# ============================================================
# REFERRALS
# ============================================================

def get_referral_admin(user_id):
    row = db.execute(
        """
        SELECT admin_id
        FROM referrals
        WHERE user_id=?
        """,
        (user_id,),
    ).fetchone()

    if row:
        return row["admin_id"]

    return None


def set_referral_once(user_id, admin_id):
    existing = get_referral_admin(user_id)

    if existing is not None:
        return

    if admin_id == user_id:
        return

    db.execute(
        """
        INSERT OR IGNORE INTO referrals(
            user_id,
            admin_id,
            created_at
        )
        VALUES(?,?,?)
        """,
        (
            user_id,
            admin_id,
            now(),
        ),
    )

    db.execute(
        """
        UPDATE users
        SET referred_by=?
        WHERE user_id=?
          AND referred_by IS NULL
        """,
        (
            admin_id,
            user_id,
        ),
    )

    db.commit()


# ============================================================
# CARDS
# ============================================================

def get_admin_card(admin_id):
    return db.execute(
        """
        SELECT *
        FROM cards
        WHERE admin_id=?
        """,
        (admin_id,),
    ).fetchone()


def save_card(admin_id, number, owner):
    db.execute(
        """
        INSERT INTO cards(
            admin_id,
            card_number,
            card_owner,
            updated_at
        )
        VALUES(?,?,?,?)
        ON CONFLICT(admin_id)
        DO UPDATE SET
            card_number=excluded.card_number,
            card_owner=excluded.card_owner,
            updated_at=excluded.updated_at
        """,
        (
            admin_id,
            number,
            owner,
            now(),
        ),
    )

    db.commit()


def delete_card(admin_id):
    db.execute(
        "DELETE FROM cards WHERE admin_id=?",
        (admin_id,),
    )

    db.commit()


# ============================================================
# PRIME
# ============================================================

def active_prime(user_id):
    row = get_user(user_id)

    if not row:
        return False

    if not row["prime_until"]:
        return False

    try:
        return datetime.fromisoformat(
            row["prime_until"]
        ) > datetime.utcnow()
    except ValueError:
        return False


def format_prime_date(value):
    if not value:
        return "—"

    try:
        dt = datetime.fromisoformat(value)

        return dt.strftime(
            "%d.%m.%Y %H:%M"
        )
    except ValueError:
        return value


# ============================================================
# ADMIN STATISTICS
# ============================================================

def admin_stats(admin_id):
    referrals = db.execute(
        """
        SELECT COUNT(*) AS c
        FROM referrals
        WHERE admin_id=?
        """,
        (admin_id,),
    ).fetchone()["c"]

    revenue = db.execute(
        """
        SELECT COALESCE(SUM(price),0) AS s
        FROM payments
        WHERE admin_id=?
          AND status='approved'
        """,
        (admin_id,),
    ).fetchone()["s"]

    approved = db.execute(
        """
        SELECT COUNT(*) AS c
        FROM payments
        WHERE admin_id=?
          AND status='approved'
        """,
        (admin_id,),
    ).fetchone()["c"]

    pending = db.execute(
        """
        SELECT COUNT(*) AS c
        FROM payments
        WHERE admin_id=?
          AND status='pending'
        """,
        (admin_id,),
    ).fetchone()["c"]

    return (
        referrals,
        revenue,
        approved,
        pending,
    )


# ============================================================
# KEYBOARDS
# ============================================================

def main_menu(user_id):
    rows = [
        [
            KeyboardButton(text="🔎 Kino qidirish"),
            KeyboardButton(text="⭐ Prime status"),
        ],
        [
            KeyboardButton(text="📚 Kinolar ro'yxati"),
        ],
        [
            KeyboardButton(text="📸 Instagramga qaytish"),
        ],
        [
            KeyboardButton(text="🎬 Kino buyurtma qilish"),
        ],
        [
            KeyboardButton(text="🤝 Reklama & Bot olish"),
        ],
    ]

    if is_admin(user_id):
        rows.append(
            [
                KeyboardButton(text="👨‍💻 Admin panel"),
            ]
        )

    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
    )


def back_cancel_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="⬅️ Orqaga"),
                KeyboardButton(text="❌ Bekor qilish"),
            ]
        ],
        resize_keyboard=True,
    )


def admin_menu(user_id):
    rows = [
        [
            KeyboardButton(text="🎬 Kino qo'shish"),
            KeyboardButton(text="🗑 Kino o'chirish"),
        ],
        [
            KeyboardButton(text="📚 Kinolar"),
            KeyboardButton(text="📊 Statistika"),
        ],
        [
            KeyboardButton(text="💳 Karta sozlamalari"),
            KeyboardButton(text="📢 Kanallar"),
        ],
        [
            KeyboardButton(text="🔗 Mening referralim"),
        ],
        [
            KeyboardButton(text="🏠 Asosiy menyu"),
        ],
    ]

    if is_superadmin(user_id):
        rows.insert(
            3,
            [
                KeyboardButton(text="👥 Adminlar"),
            ],
        )

    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
    )


def card_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="➕ Karta qo'shish"),
                KeyboardButton(text="🔄 Kartani almashtirish"),
            ],
            [
                KeyboardButton(text="👀 Hozirgi kartani ko'rish"),
                KeyboardButton(text="🗑 Kartani o'chirish"),
            ],
            [
                KeyboardButton(text="⬅️ Admin panel"),
            ],
        ],
        resize_keyboard=True,
    )


def admin_manage_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="➕ Admin qo'shish"),
                KeyboardButton(text="🗑 Admin o'chirish"),
            ],
            [
                KeyboardButton(text="📋 Adminlar"),
                KeyboardButton(text="⬅️ Admin panel"),
            ],
        ],
        resize_keyboard=True,
    )


def channel_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="➕ Kanal qo'shish"),
                KeyboardButton(text="🗑 Kanal o'chirish"),
            ],
            [
                KeyboardButton(text="📋 Kanallar"),
                KeyboardButton(text="⬅️ Admin panel"),
            ],
        ],
        resize_keyboard=True,
    )


def inline_subscription_keyboard():
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text="📢 Kanalga o'tish",
            url="https://t.me/uz_kinocinema",
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="✅ Obuna bo'ldim",
            callback_data="subscription_check",
        )
    )

    return builder.as_markup()


def prime_plans_keyboard():
    builder = InlineKeyboardBuilder()

    plans = [
        (
            "7 kun — 7 000 so'm",
            "prime:7:7000:7",
        ),
        (
            "1 oy — 20 000 so'm",
            "prime:1oy:20000:30",
        ),
        (
            "3 oy — 50 000 so'm",
            "prime:3oy:50000:90",
        ),
        (
            "Umrbod — 150 000 so'm",
            "prime:lifetime:150000:0",
        ),
    ]

    for text, data in plans:
        builder.row(
            InlineKeyboardButton(
                text=text,
                callback_data=data,
            )
        )

    builder.row(
        InlineKeyboardButton(
            text="❌ Yopish",
            callback_data="prime_close",
        )
    )

    return builder.as_markup()


def payment_decision_keyboard(payment_id):
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text="✅ Tasdiqlash",
            callback_data=f"pay:approve:{payment_id}",
        ),
        InlineKeyboardButton(
            text="❌ Bekor qilish",
            callback_data=f"pay:reject:{payment_id}",
        ),
    )

    return builder.as_markup()


# ============================================================
# FSM STATES
# ============================================================

class SearchState(StatesGroup):
    code = State()


class CardState(StatesGroup):
    number = State()
    owner = State()


class MovieAddState(StatesGroup):
    code = State()
    title = State()
    video = State()
    type = State()


class MovieDeleteState(StatesGroup):
    code = State()


class AdminAddState(StatesGroup):
    user_id = State()


class AdminDeleteState(StatesGroup):
    user_id = State()


class ChannelAddState(StatesGroup):
    username = State()


class ChannelDeleteState(StatesGroup):
    username = State()


class PaymentState(StatesGroup):
    screenshot = State()


class OrderState(StatesGroup):
    text = State()


# ============================================================
# ROUTER
# ============================================================

router = Router()


# ============================================================
# SUBSCRIPTION
# ============================================================

async def subscription_required(
    bot: Bot,
    user_id: int,
) -> bool:

    if is_admin(user_id):
        return True

    channels = db.execute(
        """
        SELECT username
        FROM channels
        ORDER BY id
        """
    ).fetchall()

    for row in channels:
        username = row["username"]

        try:
            member = await bot.get_chat_member(
                username,
                user_id,
            )

            if member.status in {
                ChatMemberStatus.LEFT,
                ChatMemberStatus.KICKED,
            }:
                return False

            if (
                member.status == ChatMemberStatus.RESTRICTED
                and not getattr(
                    member,
                    "is_member",
                    False,
                )
            ):
                return False

        except (
            TelegramBadRequest,
            TelegramForbiddenError,
            TelegramNetworkError,
        ):
            logger.exception(
                "Kanal obunasi tekshiruvida xato: %s",
                username,
            )

            return False

        except Exception:
            logger.exception(
                "Noma'lum subscription xatosi: %s",
                username,
            )

            return False

    return True


async def show_subscription_block(
    message: Message,
):
    await message.answer(
        "🔒 Avval majburiy kanalga obuna bo'ling.",
        reply_markup=inline_subscription_keyboard(),
    )


class SubscriptionMiddleware:

    async def __call__(
        self,
        handler,
        event,
        data,
    ):
        bot: Bot = data["bot"]

        user = getattr(
            event,
            "from_user",
            None,
        )

        if not user:
            return await handler(
                event,
                data,
            )

        # /start har doim ishlashi kerak.
        if isinstance(event, Message):

            if (
                event.text
                and event.text.startswith("/start")
            ):
                return await handler(
                    event,
                    data,
                )

            # FSM cancel/back ishlashi kerak.
            if event.text in {
                "❌ Bekor qilish",
                "⬅️ Orqaga",
            }:
                return await handler(
                    event,
                    data,
                )

        # Obuna tekshirish callback har doim ishlashi kerak.
        if isinstance(event, CallbackQuery):

            if event.data == "subscription_check":
                return await handler(
                    event,
                    data,
                )

        # Adminlar majburiy obunadan ozod.
        if is_admin(user.id):
            return await handler(
                event,
                data,
            )

        subscribed = await subscription_required(
            bot,
            user.id,
        )

        if not subscribed:

            if isinstance(
                event,
                CallbackQuery,
            ):
                await event.answer(
                    "🔒 Avval kanalga obuna bo'ling.",
                    show_alert=True,
                )

                try:
                    await event.message.answer(
                        "🔒 Avval majburiy kanalga obuna bo'ling.",
                        reply_markup=inline_subscription_keyboard(),
                    )
                except Exception:
                    pass

                return

            if isinstance(
                event,
                Message,
            ):
                await show_subscription_block(
                    event
                )

                return

        return await handler(
            event,
            data,
        )


router.message.outer_middleware(
    SubscriptionMiddleware()
)

router.callback_query.outer_middleware(
    SubscriptionMiddleware()
)


# ============================================================
# START
# ============================================================

@router.message(CommandStart())
async def start_handler(
    message: Message,
    state: FSMContext,
    bot: Bot,
):
    global SUPERADMIN_ID

    await state.clear()

    upsert_user(
        message.from_user
    )

    # @omono_v yagona superadmin.
    if (
        message.from_user.username
        and message.from_user.username.lower()
        == SUPERADMIN_USERNAME.lower()
    ):
        SUPERADMIN_ID = message.from_user.id

        add_admin(
            message.from_user.id,
            SUPERADMIN_USERNAME,
            message.from_user.id,
        )

    # Referral parametrini olish.
    args = message.text.split(
        maxsplit=1
    )

    if (
        len(args) > 1
        and args[1].startswith("ref_")
    ):
        try:
            ref_admin = int(
                args[1][4:]
            )

            if is_admin(ref_admin):
                set_referral_once(
                    message.from_user.id,
                    ref_admin,
                )

        except ValueError:
            pass

    # Adminlar obunadan ozod.
    if is_admin(message.from_user.id):
        await message.answer(
            "🎬 KinoCinema botiga xush kelibsiz!\n\n"
            "Kerakli bo'limni tanlang:",
            reply_markup=main_menu(
                message.from_user.id
            ),
        )

        return

    # Oddiy user subscription tekshiruvi.
    if not await subscription_required(
        bot,
        message.from_user.id,
    ):
        await message.answer(
            "🔒 Avval majburiy kanalga obuna bo'ling.",
            reply_markup=inline_subscription_keyboard(),
        )

        return

    await message.answer(
        "🎬 KinoCinema botiga xush kelibsiz!\n\n"
        "Kerakli bo'limni tanlang:",
        reply_markup=main_menu(
            message.from_user.id
        ),
    )


# ============================================================
# SUBSCRIPTION CALLBACK
# ============================================================

@router.callback_query(
    F.data == "subscription_check"
)
async def subscription_check_callback(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
):
    if await subscription_required(
        bot,
        callback.from_user.id,
    ):
        await callback.answer(
            "✅ Obuna tasdiqlandi!",
            show_alert=True,
        )

        await state.clear()

        try:
            await callback.message.edit_text(
                "✅ Obuna tasdiqlandi!"
            )
        except Exception:
            pass

        await callback.message.answer(
            "🎬 KinoCinema botiga xush kelibsiz!\n\n"
            "Kerakli bo'limni tanlang:",
            reply_markup=main_menu(
                callback.from_user.id
            ),
        )

    else:
        await callback.answer(
            "❌ Hali kanalga obuna bo'lmagansiz.",
            show_alert=True,
        )


# ============================================================
# CANCEL
# ============================================================

@router.message(
    F.text == "❌ Bekor qilish"
)
async def cancel_handler(
    message: Message,
    state: FSMContext,
):
    current = await state.get_state()

    await state.clear()

    if current and current.startswith(
        "CardState"
    ):
        await message.answer(
            "❌ Bekor qilindi.",
            reply_markup=card_menu(),
        )

    elif current and current.startswith(
        (
            "MovieAddState",
            "MovieDeleteState",
            "AdminAddState",
            "AdminDeleteState",
            "ChannelAddState",
            "ChannelDeleteState",
        )
    ):
        await message.answer(
            "❌ Bekor qilindi.",
            reply_markup=admin_menu(
                message.from_user.id
            ),
        )

    elif current and current.startswith(
        "PaymentState"
    ):
        await message.answer(
            "❌ To'lov bekor qilindi.",
            reply_markup=main_menu(
                message.from_user.id
            ),
        )

    elif current and current.startswith(
        "OrderState"
    ):
        await message.answer(
            "❌ Buyurtma bekor qilindi.",
            reply_markup=main_menu(
                message.from_user.id
            ),
        )

    elif current and current.startswith(
        "SearchState"
    ):
        await message.answer(
            "❌ Qidiruv bekor qilindi.",
            reply_markup=main_menu(
                message.from_user.id
            ),
        )

    else:
        await message.answer(
            "🏠 Asosiy menyu",
            reply_markup=main_menu(
                message.from_user.id
            ),
        )


# ============================================================
# BACK
# ============================================================

@router.message(
    F.text == "⬅️ Orqaga"
)
async def back_handler(
    message: Message,
    state: FSMContext,
):
    current = await state.get_state()

    if current is None:
        await message.answer(
            "🏠 Asosiy menyu",
            reply_markup=main_menu(
                message.from_user.id
            ),
        )

        return

    if current == SearchState.code.state:

        await state.clear()

        await message.answer(
            "🏠 Asosiy menyu",
            reply_markup=main_menu(
                message.from_user.id
            ),
        )

    elif current == CardState.owner.state:

        await state.set_state(
            CardState.number
        )

        await message.answer(
            "💳 Karta raqamini yuboring.\n\n"
            "Masalan:\n"
            "9860600435412504",
            reply_markup=back_cancel_keyboard(),
        )

    elif current == CardState.number.state:

        await state.clear()

        await message.answer(
            "💳 Karta sozlamalari",
            reply_markup=card_menu(),
        )

    elif current == MovieAddState.title.state:

        await state.set_state(
            MovieAddState.code
        )

        await message.answer(
            "🎬 Kino qo'shish — 1/4\n\n"
            "Kino kodini yuboring.",
            reply_markup=back_cancel_keyboard(),
        )

    elif current == MovieAddState.video.state:

        await state.set_state(
            MovieAddState.title
        )

        await message.answer(
            "🎬 Kino qo'shish — 2/4\n\n"
            "Kino nomini yuboring.",
            reply_markup=back_cancel_keyboard(),
        )

    elif current == MovieAddState.type.state:

        await state.set_state(
            MovieAddState.video
        )

        await message.answer(
            "🎬 Kino qo'shish — 3/4\n\n"
            "Kino videosini yuboring.",
            reply_markup=back_cancel_keyboard(),
        )

    elif current == MovieAddState.code.state:

        await state.clear()

        await message.answer(
            "👨‍💻 Admin panel",
            reply_markup=admin_menu(
                message.from_user.id
            ),
        )

    elif current == MovieDeleteState.code.state:

        await state.clear()

        await message.answer(
            "👨‍💻 Admin panel",
            reply_markup=admin_menu(
                message.from_user.id
            ),
        )

    elif current in {
        AdminAddState.user_id.state,
        AdminDeleteState.user_id.state,
    }:

        await state.clear()

        await message.answer(
            "👥 Adminlar",
            reply_markup=admin_manage_menu(),
        )

    elif current in {
        ChannelAddState.username.state,
        ChannelDeleteState.username.state,
    }:

        await state.clear()

        await message.answer(
            "📢 Kanallar",
            reply_markup=channel_menu(),
        )

    elif current == OrderState.text.state:

        await state.clear()

        await message.answer(
            "🏠 Asosiy menyu",
            reply_markup=main_menu(
                message.from_user.id
            ),
        )

    elif current == PaymentState.screenshot.state:

        await state.clear()

        await message.answer(
            "🏠 Asosiy menyu",
            reply_markup=main_menu(
                message.from_user.id
            ),
        )

    else:

        await state.clear()

        await message.answer(
            "🏠 Asosiy menyu",
            reply_markup=main_menu(
                message.from_user.id
            ),
        )


# ============================================================
# KINO QIDIRISH
# ============================================================

@router.message(
    F.text == "🔎 Kino qidirish"
)
async def search_start(
    message: Message,
    state: FSMContext,
):
    await state.set_state(
        SearchState.code
    )

    await message.answer(
        "🔎 Kino kodini yuboring.\n\n"
        "Masalan:\n"
        "247",
        reply_markup=back_cancel_keyboard(),
    )


async def send_movie_by_code(
    message: Message,
    code: str,
    bot: Bot,
):
    code = code.strip()

    movie = db.execute(
        """
        SELECT *
        FROM movies
        WHERE code=?
        """,
        (code,),
    ).fetchone()

    if not movie:
        return False

    if (
        movie["prime_only"]
        and not active_prime(
            message.from_user.id
        )
        and not is_admin(
            message.from_user.id
        )
    ):
        await message.answer(
            "⭐ Bu kino faqat Prime uchun."
        )

        return True

    db.execute(
        """
        UPDATE movies
        SET views=views+1
        WHERE id=?
        """,
        (movie["id"],),
    )

    db.commit()

    caption = (
        f"🎬 {escape(movie['title'])}\n"
        f"🔢 Kod: {escape(movie['code'])}\n\n"
        "🍿 Yoqimli tomosha!"
    )

    try:
        await message.answer_video(
            video=movie["file_id"],
            caption=caption,
        )

    except Exception:
        logger.exception(
            "Video yuborishda xato"
        )

        await message.answer(
            "❌ Videoni yuborishda xatolik yuz berdi. "
            "Keyinroq urinib ko'ring."
        )

    return True


@router.message(
    SearchState.code,
    F.text,
)
async def search_code_handler(
    message: Message,
    state: FSMContext,
    bot: Bot,
):
    code = message.text.strip()

    if not code.isdigit():
        await message.answer(
            "❌ Kino kodi faqat raqamlardan iborat "
            "bo'lishi kerak."
        )

        return

    found = await send_movie_by_code(
        message,
        code,
        bot,
    )

    if not found:
        await message.answer(
            "❌ Bunday kino topilmadi."
        )


# ============================================================
# PRIME STATUS
# ============================================================

@router.message(
    F.text == "⭐ Prime status"
)
async def prime_status(
    message: Message,
):
    row = get_user(
        message.from_user.id
    )

    if active_prime(
        message.from_user.id
    ):
        await message.answer(
            "⭐ Prime status\n\n"
            "✅ Faol\n\n"
            f"📅 Tugash sanasi:\n"
            f"{format_prime_date(row['prime_until'])}"
        )

    else:
        await message.answer(
            "⭐ Prime status\n\n"
            "❌ Prime faol emas.",
            reply_markup=prime_plans_keyboard(),
        )


# ============================================================
# PRIME PLAN
# ============================================================

@router.callback_query(
    F.data.startswith("prime:")
)
async def prime_plan_callback(
    callback: CallbackQuery,
    state: FSMContext,
):
    try:
        (
            _,
            plan,
            price_text,
            days_text,
        ) = callback.data.split(":")

        price = int(price_text)
        days = int(days_text)

    except (
        ValueError,
        AttributeError,
    ):
        await callback.answer(
            "❌ Tarif ma'lumotida xato.",
            show_alert=True,
        )

        return

    admin_id = (
        get_referral_admin(
            callback.from_user.id
        )
        or SUPERADMIN_ID
    )

    if not admin_id:
        await callback.answer(
            "❌ Mas'ul admin aniqlanmadi.",
            show_alert=True,
        )

        return

    card = get_admin_card(
        admin_id
    )

    if not card:
        await callback.answer(
            "❌ Bu admin uchun karta hali "
            "sozlanmagan.",
            show_alert=True,
        )

        return

    plan_names = {
        "7": "7 kun",
        "1oy": "1 oy",
        "3oy": "3 oy",
        "lifetime": "Umrbod",
    }

    plan_name = plan_names.get(
        plan,
        plan,
    )

    await state.set_state(
        PaymentState.screenshot
    )

    await state.update_data(
        admin_id=admin_id,
        plan=plan_name,
        days=days,
        price=price,
    )

    await callback.answer()

    await callback.message.answer(
        "💳 To'lov ma'lumotlari\n\n"
        f"💳 Karta: {escape(card['card_number'])}\n"
        f"👤 Egasi: {escape(card['card_owner'])}\n"
        f"💰 Narx: {price:,} so'm\n\n"
        "📸 To'lov screenshotini RASM "
        "ko'rinishida yuboring.",
        reply_markup=back_cancel_keyboard(),
    )


@router.callback_query(
    F.data == "prime_close"
)
async def prime_close_callback(
    callback: CallbackQuery,
):
    await callback.answer()

    try:
        await callback.message.delete()
    except Exception:
        pass


# ============================================================
# PAYMENT SCREENSHOT
# ============================================================

@router.message(
    PaymentState.screenshot,
    F.photo,
)
async def payment_screenshot_handler(
    message: Message,
    state: FSMContext,
    bot: Bot,
):
    data = await state.get_data()

    admin_id = data.get(
        "admin_id"
    )

    if not admin_id:
        await state.clear()

        await message.answer(
            "❌ To'lov ma'lumotlari topilmadi.",
            reply_markup=main_menu(
                message.from_user.id
            ),
        )

        return

    payment_id = db.execute(
        """
        INSERT INTO payments(
            user_id,
            admin_id,
            plan,
            days,
            price,
            status,
            screenshot_file_id,
            created_at
        )
        VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            message.from_user.id,
            admin_id,
            data["plan"],
            data["days"],
            data["price"],
            "pending",
            message.photo[-1].file_id,
            now(),
        ),
    ).lastrowid

    db.commit()

    await state.clear()

    caption = (
        "🧾 Yangi PRIME to'lov!\n\n"
        f"👤 User ID: {message.from_user.id}\n"
        f"📦 Plan: {data['plan']}\n"
        f"📅 Muddat: "
        f"{'Umrbod' if data['days'] == 0 else str(data['days']) + ' kun'}\n"
        f"💰 Narx: {data['price']:,} so'm"
    )

    try:
        await bot.send_photo(
            chat_id=admin_id,
            photo=message.photo[-1].file_id,
            caption=caption,
            reply_markup=payment_decision_keyboard(
                payment_id
            ),
        )

        await message.answer(
            "✅ Screenshot qabul qilindi.\n\n"
            "Admin tekshirganidan keyin "
            "natija yuboriladi.",
            reply_markup=main_menu(
                message.from_user.id
            ),
        )

    except (
        TelegramForbiddenError,
        TelegramBadRequest,
    ):
        logger.exception(
            "Payment adminiga yuborilmadi: %s",
            admin_id,
        )

        await message.answer(
            "⚠️ To'lov qabul qilindi, ammo "
            "mas'ul adminga xabar yuborilmadi.\n\n"
            "Admin botga /start yuborishi kerak.",
            reply_markup=main_menu(
                message.from_user.id
            ),
        )

    except Exception:
        logger.exception(
            "Payment yuborishda kutilmagan xato"
        )

        await message.answer(
            "⚠️ To'lov saqlandi, ammo adminga "
            "yuborishda xato yuz berdi.",
            reply_markup=main_menu(
                message.from_user.id
            ),
        )


@router.message(
    PaymentState.screenshot
)
async def payment_wrong_type(
    message: Message,
):
    await message.answer(
        "📸 Iltimos, to'lov screenshotini "
        "RASM ko'rinishida yuboring."
    )


# ============================================================
# PAYMENT APPROVE / REJECT
# ============================================================

@router.callback_query(
    F.data.startswith("pay:")
)
async def payment_decision_callback(
    callback: CallbackQuery,
    bot: Bot,
):
    parts = callback.data.split(":")

    if len(parts) != 3:
        await callback.answer(
            "❌ Noto'g'ri callback.",
            show_alert=True,
        )

        return

    action = parts[1]

    try:
        payment_id = int(parts[2])
    except ValueError:
        await callback.answer(
            "❌ Payment ID xato.",
            show_alert=True,
        )

        return

    payment = db.execute(
        """
        SELECT *
        FROM payments
        WHERE id=?
        """,
        (payment_id,),
    ).fetchone()

    if not payment:
        await callback.answer(
            "❌ To'lov topilmadi.",
            show_alert=True,
        )

        return

    # Payment faqat o'z adminiga tegishli.
    if payment["admin_id"] != callback.from_user.id:
        await callback.answer(
            "❌ Bu to'lov sizga biriktirilmagan.",
            show_alert=True,
        )

        return

    # Ikkinchi marta approve/reject bo'lmaydi.
    if payment["status"] != "pending":
        await callback.answer(
            "ℹ️ Bu to'lov allaqachon "
            "ko'rib chiqilgan.",
            show_alert=True,
        )

        return

    # --------------------------------------------------------
    # APPROVE
    # --------------------------------------------------------

    if action == "approve":

        user = get_user(
            payment["user_id"]
        )

        if not user:
            await callback.answer(
                "❌ User topilmadi.",
                show_alert=True,
            )

            return

        # Umrbod.
        if payment["days"] == 0:

            prime_until = (
                "9999-12-31 23:59:59"
            )

        else:

            base = datetime.utcnow()

            if user["prime_until"]:

                try:
                    existing = datetime.fromisoformat(
                        user["prime_until"]
                    )

                    if existing > base:
                        base = existing

                except ValueError:
                    pass

            prime_until = (
                base
                + timedelta(
                    days=payment["days"]
                )
            ).strftime(
                "%Y-%m-%d %H:%M:%S"
            )

        # Atomik holatda pending bo'lsa approve.
        cur = db.execute(
            """
            UPDATE payments
            SET status='approved',
                decided_at=?
            WHERE id=?
              AND status='pending'
              AND admin_id=?
            """,
            (
                now(),
                payment_id,
                callback.from_user.id,
            ),
        )

        if cur.rowcount != 1:
            await callback.answer(
                "ℹ️ Bu to'lov allaqachon "
                "ko'rib chiqilgan.",
                show_alert=True,
            )

            return

        db.execute(
            """
            UPDATE users
            SET prime_until=?
            WHERE user_id=?
            """,
            (
                prime_until,
                payment["user_id"],
            ),
        )

        db.commit()

        await callback.answer(
            "✅ To'lov tasdiqlandi."
        )

        try:
            await callback.message.edit_reply_markup(
                reply_markup=None
            )
        except Exception:
            pass

        try:
            await bot.send_message(
                payment["user_id"],
                "✅ To'lov tasdiqlandi!\n\n"
                f"⭐ Prime: {payment['plan']}\n"
                f"📅 Tugash sanasi: "
                f"{format_prime_date(prime_until)}",
            )

        except Exception:
            logger.exception(
                "Prime userga yuborilmadi"
            )

        return

    # --------------------------------------------------------
    # REJECT
    # --------------------------------------------------------

    if action == "reject":

        cur = db.execute(
            """
            UPDATE payments
            SET status='rejected',
                decided_at=?
            WHERE id=?
              AND status='pending'
              AND admin_id=?
            """,
            (
                now(),
                payment_id,
                callback.from_user.id,
            ),
        )

        if cur.rowcount != 1:
            await callback.answer(
                "ℹ️ Bu to'lov allaqachon "
                "ko'rib chiqilgan.",
                show_alert=True,
            )

            return

        db.commit()

        await callback.answer(
            "❌ To'lov bekor qilindi."
        )

        try:
            await callback.message.edit_reply_markup(
                reply_markup=None
            )
        except Exception:
            pass

        try:
            await bot.send_message(
                payment["user_id"],
                "❌ To'lov bekor qilindi.\n\n"
                "Prime ochilmadi.",
            )

        except Exception:
            logger.exception(
                "Rejected payment userga yuborilmadi"
            )


# ============================================================
# USER MOVIE LIST
# ============================================================

@router.message(
    F.text == "📚 Kinolar ro'yxati"
)
async def movie_list_user(
    message: Message,
):
    rows = db.execute(
        """
        SELECT *
        FROM movies
        ORDER BY id DESC
        """
    ).fetchall()

    if not rows:
        await message.answer(
            "📚 Hozircha kinolar mavjud emas."
        )

        return

    chunks = []

    for movie in rows:

        mark = (
            "⭐ "
            if movie["prime_only"]
            else "🎬 "
        )

        chunks.append(
            f"{mark}"
            f"{escape(movie['title'])}\n"
            f"🔢 Kod: {escape(movie['code'])} | "
            f"👁 {movie['views']}"
        )

    await message.answer(
        "📚 Kinolar ro'yxati\n\n"
        + "\n\n".join(chunks)
    )


# ============================================================
# INSTAGRAM
# ============================================================

@router.message(
    F.text == "📸 Instagramga qaytish"
)
async def instagram_handler(
    message: Message,
):
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text="📸 Instagramga o'tish",
            url=INSTAGRAM_URL,
        )
    )

    await message.answer(
        "📸 Instagram sahifamiz:",
        reply_markup=builder.as_markup(),
    )


# ============================================================
# ORDER
# ============================================================

@router.message(
    F.text == "🎬 Kino buyurtma qilish"
)
async def order_start(
    message: Message,
    state: FSMContext,
):
    await state.set_state(
        OrderState.text
    )

    await message.answer(
        "🎬 Qanday kino kerakligini "
        "yozib yuboring.",
        reply_markup=back_cancel_keyboard(),
    )


@router.message(
    OrderState.text,
    F.text,
)
async def order_handler(
    message: Message,
    state: FSMContext,
    bot: Bot,
):
    text = message.text.strip()

    if not text:
        await message.answer(
            "❌ Buyurtma matni bo'sh bo'lmasin."
        )

        return

    admin_id = (
        get_referral_admin(
            message.from_user.id
        )
        or SUPERADMIN_ID
    )

    if not admin_id:
        await message.answer(
            "❌ Mas'ul admin aniqlanmadi."
        )

        await state.clear()

        return

    order_id = db.execute(
        """
        INSERT INTO orders(
            user_id,
            admin_id,
            text,
            created_at
        )
        VALUES(?,?,?,?)
        """,
        (
            message.from_user.id,
            admin_id,
            text,
            now(),
        ),
    ).lastrowid

    db.commit()

    await state.clear()

    await message.answer(
        "✅ Buyurtmangiz qabul qilindi.",
        reply_markup=main_menu(
            message.from_user.id
        ),
    )

    try:
        await bot.send_message(
            admin_id,
            "🎬 Yangi kino buyurtma!\n\n"
            f"🧾 Buyurtma ID: {order_id}\n"
            f"👤 User ID: {message.from_user.id}\n"
            f"📝 {escape(text)}",
        )

    except Exception:
        logger.exception(
            "Order adminiga yuborilmadi"
        )


# ============================================================
# ADS
# ============================================================

@router.message(
    F.text == "🤝 Reklama & Bot olish"
)
async def ads_handler(
    message: Message,
):
    await message.answer(
        "🤝 Reklama & Bot olish\n\n"
        "Admin: @omono_v"
    )


# ============================================================
# ADMIN PANEL
# ============================================================

@router.message(
    F.text == "👨‍💻 Admin panel"
)
async def admin_panel_handler(
    message: Message,
    state: FSMContext,
):
    await state.clear()

    if not is_admin(
        message.from_user.id
    ):
        await message.answer(
            "❌ Sizda admin huquqi yo'q."
        )

        return

    await message.answer(
        "👨‍💻 Admin panel",
        reply_markup=admin_menu(
            message.from_user.id
        ),
    )


# ============================================================
# HOME
# ============================================================

@router.message(
    F.text == "🏠 Asosiy menyu"
)
async def home_handler(
    message: Message,
    state: FSMContext,
):
    await state.clear()

    await message.answer(
        "🏠 Asosiy menyu",
        reply_markup=main_menu(
            message.from_user.id
        ),
    )


# ============================================================
# ADD MOVIE
# ============================================================

@router.message(
    F.text == "🎬 Kino qo'shish"
)
async def movie_add_start(
    message: Message,
    state: FSMContext,
):
    if not is_admin(
        message.from_user.id
    ):
        return

    await state.set_state(
        MovieAddState.code
    )

    await message.answer(
        "🎬 Kino qo'shish — 1/4\n\n"
        "Kino kodini yuboring.",
        reply_markup=back_cancel_keyboard(),
    )


@router.message(
    MovieAddState.code,
    F.text,
)
async def movie_add_code(
    message: Message,
    state: FSMContext,
):
    code = message.text.strip()

    if not code.isdigit():
        await message.answer(
            "❌ Kino kodi faqat raqamlardan "
            "iborat bo'lishi kerak."
        )

        return

    if db.execute(
        "SELECT 1 FROM movies WHERE code=?",
        (code,),
    ).fetchone():

        await message.answer(
            "❌ Bu koddagi kino allaqachon mavjud."
        )

        return

    await state.update_data(
        code=code
    )

    await state.set_state(
        MovieAddState.title
    )

    await message.answer(
        "🎬 Kino qo'shish — 2/4\n\n"
        "Kino nomini yuboring.",
        reply_markup=back_cancel_keyboard(),
    )


@router.message(
    MovieAddState.title,
    F.text,
)
async def movie_add_title(
    message: Message,
    state: FSMContext,
):
    title = message.text.strip()

    if len(title) < 1:
        await message.answer(
            "❌ Kino nomi bo'sh bo'lmasin."
        )

        return

    await state.update_data(
        title=title
    )

    await state.set_state(
        MovieAddState.video
    )

    await message.answer(
        "🎬 Kino qo'shish — 3/4\n\n"
        "Kino videosini yuboring.",
        reply_markup=back_cancel_keyboard(),
    )


@router.message(
    MovieAddState.video,
    F.video,
)
async def movie_add_video(
    message: Message,
    state: FSMContext,
):
    await state.update_data(
        file_id=message.video.file_id
    )

    await state.set_state(
        MovieAddState.type
    )

    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text="🆓 Oddiy kino",
            callback_data="movie_type:0",
        ),
        InlineKeyboardButton(
            text="⭐ Faqat Prime",
            callback_data="movie_type:1",
        ),
    )

    await message.answer(
        "🎬 Kino qo'shish — 4/4\n\n"
        "Kino turini tanlang:",
        reply_markup=builder.as_markup(),
    )


@router.message(
    MovieAddState.video
)
async def movie_add_video_wrong(
    message: Message,
):
    await message.answer(
        "🎬 Iltimos, kino videosini VIDEO "
        "ko'rinishida yuboring."
    )


@router.callback_query(
    MovieAddState.type,
    F.data.startswith("movie_type:")
)
async def movie_add_type(
    callback: CallbackQuery,
    state: FSMContext,
):
    if not is_admin(
        callback.from_user.id
    ):
        await callback.answer(
            "❌ Admin huquqi kerak.",
            show_alert=True,
        )

        return

    prime_only = (
        1
        if callback.data.endswith(":1")
        else 0
    )

    data = await state.get_data()

    try:
        db.execute(
            """
            INSERT INTO movies(
                code,
                title,
                file_id,
                prime_only,
                views,
                added_by,
                created_at
            )
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                data["code"],
                data["title"],
                data["file_id"],
                prime_only,
                0,
                callback.from_user.id,
                now(),
            ),
        )

        db.commit()

    except sqlite3.IntegrityError:

        await callback.answer(
            "❌ Bu kino kodi allaqachon mavjud.",
            show_alert=True,
        )

        return

    await state.clear()

    await callback.answer(
        "✅ Kino qo'shildi!"
    )

    await callback.message.answer(
        "✅ Kino qo'shildi!\n\n"
        f"Kod: {escape(data['code'])}\n"
        f"Nomi: {escape(data['title'])}\n"
        f"Turi: "
        f"{'Prime' if prime_only else 'Oddiy'}",
        reply_markup=admin_menu(
            callback.from_user.id
        ),
    )


# ============================================================
# DELETE MOVIE
# ============================================================

@router.message(
    F.text == "🗑 Kino o'chirish"
)
async def movie_delete_start(
    message: Message,
    state: FSMContext,
):
    if not is_admin(
        message.from_user.id
    ):
        return

    await state.set_state(
        MovieDeleteState.code
    )

    await message.answer(
        "🗑 Kino kodini yuboring.",
        reply_markup=back_cancel_keyboard(),
    )


@router.message(
    MovieDeleteState.code,
    F.text,
)
async def movie_delete_handler(
    message: Message,
    state: FSMContext,
):
    code = message.text.strip()

    movie = db.execute(
        """
        SELECT *
        FROM movies
        WHERE code=?
        """,
        (code,),
    ).fetchone()

    if not movie:
        await message.answer(
            "❌ Bunday kino topilmadi."
        )

        return

    db.execute(
        "DELETE FROM movies WHERE code=?",
        (code,),
    )

    db.commit()

    await state.clear()

    await message.answer(
        "✅ Kino o'chirildi!\n\n"
        f"🎬 {escape(movie['title'])}\n"
        f"🔢 Kod: {escape(code)}",
        reply_markup=admin_menu(
            message.from_user.id
        ),
    )


# ============================================================
# ADMIN MOVIE LIST
# ============================================================

@router.message(
    F.text == "📚 Kinolar"
)
async def admin_movie_list(
    message: Message,
):
    if not is_admin(
        message.from_user.id
    ):
        return

    rows = db.execute(
        """
        SELECT *
        FROM movies
        ORDER BY id DESC
        """
    ).fetchall()

    if not rows:
        await message.answer(
            "📚 Hozircha kinolar mavjud emas."
        )

        return

    text = []

    for movie in rows:

        text.append(
            f"{'⭐' if movie['prime_only'] else '🎬'} "
            f"{escape(movie['title'])}\n"
            f"🔢 {escape(movie['code'])} | "
            f"👁 {movie['views']}"
        )

    await message.answer(
        "📚 Kinolar\n\n"
        + "\n\n".join(text)
    )


# ============================================================
# CARD SETTINGS
# ============================================================

@router.message(
    F.text == "💳 Karta sozlamalari"
)
async def card_settings(
    message: Message,
    state: FSMContext,
):
    if not is_admin(
        message.from_user.id
    ):
        return

    await state.clear()

    await message.answer(
        "💳 Karta sozlamalari",
        reply_markup=card_menu(),
    )


@router.message(
    F.text.in_(
        {
            "➕ Karta qo'shish",
            "🔄 Kartani almashtirish",
        }
    )
)
async def card_add_start(
    message: Message,
    state: FSMContext,
):
    if not is_admin(
        message.from_user.id
    ):
        return

    await state.set_state(
        CardState.number
    )

    await message.answer(
        "💳 Karta raqamini yuboring.\n\n"
        "Masalan:\n"
        "9860600435412504",
        reply_markup=back_cancel_keyboard(),
    )


@router.message(
    CardState.number,
    F.text,
)
async def card_number_handler(
    message: Message,
    state: FSMContext,
):
    number = "".join(
        message.text.split()
    )

    if (
        not number.isdigit()
        or not 12 <= len(number) <= 19
    ):
        await message.answer(
            "❌ Karta raqami 12–19 ta "
            "raqamdan iborat bo'lishi kerak."
        )

        return

    await state.update_data(
        card_number=number
    )

    await state.set_state(
        CardState.owner
    )

    await message.answer(
        "2/2:\n\n"
        "👤 Karta egasining ism-familiyasini "
        "yuboring.\n\n"
        "Masalan:\n"
        "Muhammad Ali.O.",
        reply_markup=back_cancel_keyboard(),
    )


@router.message(
    CardState.owner,
    F.text,
)
async def card_owner_handler(
    message: Message,
    state: FSMContext,
):
    owner = message.text.strip()

    if len(owner) < 2:
        await message.answer(
            "❌ Ism-familiya juda qisqa."
        )

        return

    data = await state.get_data()

    save_card(
        message.from_user.id,
        data["card_number"],
        owner,
    )

    await state.clear()

    await message.answer(
        "✅ Karta muvaffaqiyatli saqlandi.",
        reply_markup=card_menu(),
    )


# ============================================================
# VIEW CARD
# ============================================================

@router.message(
    F.text == "👀 Hozirgi kartani ko'rish"
)
async def card_view(
    message: Message,
):
    if not is_admin(
        message.from_user.id
    ):
        return

    card = get_admin_card(
        message.from_user.id
    )

    if not card:
        await message.answer(
            "❌ Sizda hali karta saqlanmagan."
        )

        return

    await message.answer(
        "💳 Hozirgi karta\n\n"
        f"💳 Raqam: {escape(card['card_number'])}\n"
        f"👤 Egasi: {escape(card['card_owner'])}\n"
        f"🕒 Yangilangan: {card['updated_at']}"
    )


# ============================================================
# DELETE CARD
# ============================================================

@router.message(
    F.text == "🗑 Kartani o'chirish"
)
async def card_delete(
    message: Message,
):
    if not is_admin(
        message.from_user.id
    ):
        return

    if not get_admin_card(
        message.from_user.id
    ):
        await message.answer(
            "❌ O'chirish uchun karta topilmadi.",
            reply_markup=card_menu(),
        )

        return

    delete_card(
        message.from_user.id
    )

    await message.answer(
        "✅ Karta o'chirildi.",
        reply_markup=card_menu(),
    )


# ============================================================
# BACK TO ADMIN PANEL
# ============================================================

@router.message(
    F.text == "⬅️ Admin panel"
)
async def back_admin_panel(
    message: Message,
    state: FSMContext,
):
    await state.clear()

    if is_admin(
        message.from_user.id
    ):
        await message.answer(
            "👨‍💻 Admin panel",
            reply_markup=admin_menu(
                message.from_user.id
            ),
        )

    else:
        await message.answer(
            "🏠 Asosiy menyu",
            reply_markup=main_menu(
                message.from_user.id
            ),
        )


# ============================================================
# STATISTICS
# ============================================================

@router.message(
    F.text == "📊 Statistika"
)
async def stats_handler(
    message: Message,
):
    if not is_admin(
        message.from_user.id
    ):
        return

    (
        referrals,
        revenue,
        approved,
        pending,
    ) = admin_stats(
        message.from_user.id
    )

    text = (
        "📊 Statistika\n\n"
        f"👥 Sizning referral orqali kirganlar: "
        f"{referrals}\n"
        f"💰 Tasdiqlangan Prime tushumi: "
        f"{revenue:,} so'm\n"
        f"📦 Tasdiqlangan to'lovlar: "
        f"{approved}\n"
        f"📦 Kutilayotgan to'lovlar: "
        f"{pending}"
    )

    # Bosh admin uchun umumiy statistika.
    if is_superadmin(
        message.from_user.id
    ):
        users = db.execute(
            """
            SELECT COUNT(*) AS c
            FROM users
            """
        ).fetchone()["c"]

        movies = db.execute(
            """
            SELECT COUNT(*) AS c
            FROM movies
            """
        ).fetchone()["c"]

        primes = db.execute(
            """
            SELECT COUNT(*) AS c
            FROM users
            WHERE prime_until IS NOT NULL
              AND prime_until > ?
            """,
            (now(),),
        ).fetchone()["c"]

        total = db.execute(
            """
            SELECT COALESCE(SUM(price),0) AS s
            FROM payments
            WHERE status='approved'
            """
        ).fetchone()["s"]

        admins = db.execute(
            """
            SELECT COUNT(*) AS c
            FROM admins
            """
        ).fetchone()["c"]

        text += (
            "\n\n"
            "🌐 Umumiy statistika\n\n"
            f"👥 Umumiy foydalanuvchilar: "
            f"{users}\n"
            f"🎬 Kinolar: {movies}\n"
            f"⭐ Prime userlar: {primes}\n"
            f"💰 Umumiy tushum: "
            f"{total:,} so'm\n"
            f"👥 Adminlar: {admins}"
        )

    await message.answer(
        text
    )


# ============================================================
# REFERRAL
# ============================================================

@router.message(
    F.text == "🔗 Mening referralim"
)
async def referral_handler(
    message: Message,
    bot: Bot,
):
    if not is_admin(
        message.from_user.id
    ):
        return

    me = await bot.get_me()

    username = (
        me.username
        or BOT_USERNAME
    )

    link = (
        f"https://t.me/{username}"
        f"?start=ref_{message.from_user.id}"
    )

    count = db.execute(
        """
        SELECT COUNT(*) AS c
        FROM referrals
        WHERE admin_id=?
        """,
        (message.from_user.id,),
    ).fetchone()["c"]

    revenue = db.execute(
        """
        SELECT COALESCE(SUM(price),0) AS s
        FROM payments
        WHERE admin_id=?
          AND status='approved'
        """,
        (message.from_user.id,),
    ).fetchone()["s"]

    await message.answer(
        "🔗 Sizning referral linkingiz:\n\n"
        f"{link}\n\n"
        f"👥 Silka orqali kirganlar: {count}\n"
        f"💰 Tasdiqlangan tushum: "
        f"{revenue:,} so'm"
    )


# ============================================================
# ADMINS MENU
# ============================================================

@router.message(
    F.text == "👥 Adminlar"
)
async def admins_menu_handler(
    message: Message,
    state: FSMContext,
):
    if not is_superadmin(
        message.from_user.id
    ):
        await message.answer(
            "❌ Bu bo'lim faqat bosh admin uchun."
        )

        return

    await state.clear()

    await message.answer(
        "👥 Adminlar",
        reply_markup=admin_manage_menu(),
    )


# ============================================================
# ADD ADMIN
# ============================================================

@router.message(
    F.text == "➕ Admin qo'shish"
)
async def admin_add_start(
    message: Message,
    state: FSMContext,
):
    if not is_superadmin(
        message.from_user.id
    ):
        await message.answer(
            "❌ Faqat @omono_v admin qo'sha oladi."
        )

        return

    await state.set_state(
        AdminAddState.user_id
    )

    await message.answer(
        "➕ Admin qo'shish\n\n"
        "Telegram USER ID yuboring.\n"
        "Foydalanuvchi avval botga "
        "/start bergan bo'lishi kerak.",
        reply_markup=back_cancel_keyboard(),
    )


@router.message(
    AdminAddState.user_id,
    F.text,
)
async def admin_add_handler(
    message: Message,
    state: FSMContext,
):
    try:
        user_id = int(
            message.text.strip()
        )
    except ValueError:
        await message.answer(
            "❌ USER ID faqat raqam bo'lishi kerak."
        )

        return

    user = get_user(
        user_id
    )

    if not user:
        await message.answer(
            "❌ Bu user botga hali /start bermagan."
        )

        return

    if is_admin(
        user_id
    ):
        await message.answer(
            "ℹ️ Bu user allaqachon admin."
        )

        return

    add_admin(
        user_id,
        user["username"],
        message.from_user.id,
    )

    await state.clear()

    await message.answer(
        "✅ Admin qo'shildi.",
        reply_markup=admin_manage_menu(),
    )


# ============================================================
# DELETE ADMIN
# ============================================================

@router.message(
    F.text == "🗑 Admin o'chirish"
)
async def admin_delete_start(
    message: Message,
    state: FSMContext,
):
    if not is_superadmin(
        message.from_user.id
    ):
        await message.answer(
            "❌ Faqat @omono_v admin o'chira oladi."
        )

        return

    await state.set_state(
        AdminDeleteState.user_id
    )

    await message.answer(
        "🗑 O'chiriladigan adminning "
        "Telegram USER ID sini yuboring.",
        reply_markup=back_cancel_keyboard(),
    )


@router.message(
    AdminDeleteState.user_id,
    F.text,
)
async def admin_delete_handler(
    message: Message,
    state: FSMContext,
):
    try:
        user_id = int(
            message.text.strip()
        )
    except ValueError:
        await message.answer(
            "❌ USER ID faqat raqam bo'lishi kerak."
        )

        return

    if (
        user_id == message.from_user.id
        or is_superadmin(user_id)
    ):
        await message.answer(
            "❌ Bosh adminni o'chirib bo'lmaydi."
        )

        return

    if not is_admin(
        user_id
    ):
        await message.answer(
            "❌ Bunday admin topilmadi."
        )

        return

    remove_admin(
        user_id
    )

    await state.clear()

    await message.answer(
        "✅ Admin o'chirildi.",
        reply_markup=admin_manage_menu(),
    )


# ============================================================
# ADMIN LIST
# ============================================================

@router.message(
    F.text == "📋 Adminlar"
)
async def admin_list_handler(
    message: Message,
):
    if not is_superadmin(
        message.from_user.id
    ):
        await message.answer(
            "❌ Faqat bosh admin ko'ra oladi."
        )

        return

    rows = db.execute(
        """
        SELECT *
        FROM admins
        ORDER BY created_at
        """
    ).fetchall()

    if not rows:
        await message.answer(
            "👥 Adminlar ro'yxati bo'sh."
        )

        return

    out = []

    for row in rows:

        username = (
            f"@{escape(row['username'])}"
            if row["username"]
            else "username yo'q"
        )

        label = (
            " (Bosh admin)"
            if (
                SUPERADMIN_ID is not None
                and row["user_id"]
                == SUPERADMIN_ID
            )
            else ""
        )

        out.append(
            f"👤 {username}{label}\n"
            f"🆔 {row['user_id']}"
        )

    await message.answer(
        "👥 Adminlar\n\n"
        + "\n\n".join(out)
    )


# ============================================================
# CHANNEL MENU
# ============================================================

@router.message(
    F.text == "📢 Kanallar"
)
async def channels_menu_handler(
    message: Message,
    state: FSMContext,
):
    if not is_admin(
        message.from_user.id
    ):
        return

    await state.clear()

    await message.answer(
        "📢 Kanallar",
        reply_markup=channel_menu(),
    )


# ============================================================
# CHANNEL NORMALIZE
# ============================================================

def normalize_channel(value):
    value = value.strip()

    if value.startswith(
        "https://t.me/"
    ):
        value = (
            "@"
            + value.rstrip("/")
            .split("/")[-1]
        )

    elif value.startswith(
        "http://t.me/"
    ):
        value = (
            "@"
            + value.rstrip("/")
            .split("/")[-1]
        )

    elif not value.startswith("@"):
        value = "@" + value

    return value


# ============================================================
# ADD CHANNEL
# ============================================================

@router.message(
    F.text == "➕ Kanal qo'shish"
)
async def channel_add_start(
    message: Message,
    state: FSMContext,
):
    if not is_admin(
        message.from_user.id
    ):
        return

    await state.set_state(
        ChannelAddState.username
    )

    await message.answer(
        "➕ Kanal qo'shish\n\n"
        "@username yoki "
        "https://t.me/username yuboring.",
        reply_markup=back_cancel_keyboard(),
    )


@router.message(
    ChannelAddState.username,
    F.text,
)
async def channel_add_handler(
    message: Message,
    state: FSMContext,
    bot: Bot,
):
    username = normalize_channel(
        message.text
    )

    try:
        chat = await bot.get_chat(
            username
        )

        me = await bot.get_me()

        member = await bot.get_chat_member(
            chat.id,
            me.id,
        )

        if member.status != ChatMemberStatus.ADMINISTRATOR:
            await message.answer(
                "❌ Bot bu kanalda administrator emas."
            )

            return

    except TelegramBadRequest:
        await message.answer(
            "❌ Kanal topilmadi."
        )

        return

    except TelegramForbiddenError:
        await message.answer(
            "❌ Bot bu kanalga kira olmaydi."
        )

        return

    except Exception:
        logger.exception(
            "Kanal qo'shishda xato"
        )

        await message.answer(
            "❌ Kanalni tekshirishda "
            "xatolik yuz berdi."
        )

        return

    try:
        db.execute(
            """
            INSERT INTO channels(
                username,
                title
            )
            VALUES(?,?)
            """,
            (
                username,
                chat.title or username,
            ),
        )

        db.commit()

    except sqlite3.IntegrityError:
        await message.answer(
            "ℹ️ Bu kanal allaqachon mavjud."
        )

        return

    await state.clear()

    await message.answer(
        "✅ Kanal qo'shildi.",
        reply_markup=channel_menu(),
    )


# ============================================================
# DELETE CHANNEL
# ============================================================

@router.message(
    F.text == "🗑 Kanal o'chirish"
)
async def channel_delete_start(
    message: Message,
    state: FSMContext,
):
    if not is_admin(
        message.from_user.id
    ):
        return

    await state.set_state(
        ChannelDeleteState.username
    )

    await message.answer(
        "🗑 Kanal username'sini yuboring.",
        reply_markup=back_cancel_keyboard(),
    )


@router.message(
    ChannelDeleteState.username,
    F.text,
)
async def channel_delete_handler(
    message: Message,
    state: FSMContext,
):
    username = normalize_channel(
        message.text
    )

    row = db.execute(
        """
        SELECT *
        FROM channels
        WHERE username=?
        """,
        (username,),
    ).fetchone()

    if not row:
        await message.answer(
            "❌ Bunday kanal topilmadi."
        )

        return

    db.execute(
        """
        DELETE FROM channels
        WHERE username=?
        """,
        (username,),
    )

    db.commit()

    await state.clear()

    await message.answer(
        "✅ Kanal o'chirildi.",
        reply_markup=channel_menu(),
    )


# ============================================================
# CHANNEL LIST
# ============================================================

@router.message(
    F.text == "📋 Kanallar"
)
async def channel_list_handler(
    message: Message,
):
    if not is_admin(
        message.from_user.id
    ):
        return

    rows = db.execute(
        """
        SELECT *
        FROM channels
        ORDER BY id
        """
    ).fetchall()

    if not rows:
        await message.answer(
            "📢 Majburiy kanallar "
            "ro'yxati bo'sh."
        )

        return

    text = []

    for row in rows:
        text.append(
            f"📢 {escape(row['username'])}\n"
            f"📝 {escape(row['title'] or '')}"
        )

    await message.answer(
        "📢 Kanallar\n\n"
        + "\n\n".join(text)
    )


# ============================================================
# NUMERIC MOVIE FALLBACK
# ============================================================

@router.message(
    F.text.regexp(r"^\d+$")
)
async def numeric_movie_fallback(
    message: Message,
    state: FSMContext,
    bot: Bot,
):
    # FSM state mavjud bo'lsa fallback ishlamaydi.
    if await state.get_state():
        return

    found = await send_movie_by_code(
        message,
        message.text.strip(),
        bot,
    )

    if found:
        return

    await message.answer(
        "❌ Bunday kino topilmadi."
    )


# ============================================================
# FINAL FALLBACK
# ============================================================

@router.message()
async def final_fallback(
    message: Message,
    state: FSMContext,
):
    # Eng muhim routing himoyasi.
    # FSM ichidagi xabarlar umumiy fallbackga tushmaydi.
    if await state.get_state():
        return

    await message.answer(
        "❌ Buyruq yoki kino topilmadi.\n\n"
        "Kerakli bo'limni menyudan tanlang.",
        reply_markup=main_menu(
            message.from_user.id
        ),
    )


# ============================================================
# RENDER HEALTH SERVER
# ============================================================

async def health_handler(
    request,
):
    return web.Response(
        text="KinoCinema OK"
    )


async def start_web_server():
    app = web.Application()

    app.router.add_get(
        "/",
        health_handler,
    )

    runner = web.AppRunner(
        app
    )

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT,
    )

    await site.start()

    logger.info(
        "Health server %s-portda ishga tushdi",
        PORT,
    )

    return runner


# ============================================================
# MAIN
# ============================================================

async def main():
    global SUPERADMIN_ID
    global BOT_USERNAME

    init_db()

    bot = Bot(
        token=TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML
        ),
    )

    try:
        me = await bot.get_me()

        if me.username:
            BOT_USERNAME = me.username

        # Database'dan @omono_v ni topish.
        row = db.execute(
            """
            SELECT user_id
            FROM users
            WHERE lower(username)=?
            """,
            (
                SUPERADMIN_USERNAME.lower(),
            ),
        ).fetchone()

        if row:
            SUPERADMIN_ID = row["user_id"]

            add_admin(
                SUPERADMIN_ID,
                SUPERADMIN_USERNAME,
                SUPERADMIN_ID,
            )

        else:
            logger.warning(
                "@%s hali botga /start bermagan. "
                "Superadmin ID /start dan keyin aniqlanadi.",
                SUPERADMIN_USERNAME,
            )

        dp = Dispatcher()

        dp.include_router(
            router
        )

        runner = await start_web_server()

        try:
            await dp.start_polling(
                bot
            )

        finally:
            await runner.cleanup()

    finally:
        await bot.session.close()

        try:
            db.close()
        except Exception:
            pass


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    asyncio.run(main())
