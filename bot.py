import os
import re
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.utils.deep_linking import create_start_link


# =========================================================
# SOZLAMALAR
# =========================================================

TOKEN = os.getenv("BOT_TOKEN", "").strip()

# FAQAT SHU USER bosh admin
OWNER_USERNAME = "omono_v"

# Birinchi majburiy kanal
DEFAULT_CHANNEL = "@uz_kinocinema"

INSTAGRAM_URL = "https://www.instagram.com/oemovie/"

DB_FILE = "kino.db"

if not TOKEN:
    raise RuntimeError("BOT_TOKEN Render Environment Variable da topilmadi.")


bot = Bot(
    TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

dp = Dispatcher()
router = Router()

dp.include_router(router)


# =========================================================
# DATABASE
# =========================================================

db = sqlite3.connect(DB_FILE, check_same_thread=False)
db.row_factory = sqlite3.Row


def dbq(sql, params=(), one=False, all=False, commit=False):
    cur = db.execute(sql, params)

    if commit:
        db.commit()

    if one:
        return cur.fetchone()

    if all:
        return cur.fetchall()

    return cur


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def money(number):
    return f"{number:,}".replace(",", " ")


def get_user(user_id):
    return dbq(
        "SELECT * FROM users WHERE user_id=?",
        (user_id,),
        one=True
    )


def is_owner(user_id):
    user = get_user(user_id)

    if not user:
        return False

    return (
        (user["username"] or "")
        .lower()
        .lstrip("@")
        == OWNER_USERNAME.lower()
    )


def is_admin(user_id):
    if is_owner(user_id):
        return True

    return bool(
        dbq(
            "SELECT 1 FROM admins WHERE user_id=?",
            (user_id,),
            one=True
        )
    )


def get_owner_id():
    row = dbq(
        """
        SELECT user_id
        FROM users
        WHERE lower(username)=lower(?)
        ORDER BY created_at
        LIMIT 1
        """,
        (OWNER_USERNAME,),
        one=True
    )

    return row["user_id"] if row else None


def prime_active(user_id):
    user = get_user(user_id)

    if not user or not user["prime_until"]:
        return False

    try:
        return (
            datetime.fromisoformat(user["prime_until"])
            > datetime.now(timezone.utc)
        )
    except Exception:
        return False


def normalize_channel(value):
    value = value.strip()

    if "https://t.me/" in value:
        value = value.split("https://t.me/", 1)[1]
        value = value.strip("/")

    if not value.startswith("@"):
        value = "@" + value

    return value


def init_db():
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            prime_until TEXT,
            referred_by INTEGER,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            added_by INTEGER,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            title TEXT
        );

        CREATE TABLE IF NOT EXISTS cards (
            admin_id INTEGER PRIMARY KEY,
            card_number TEXT,
            card_owner TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS movies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE,
            title TEXT,
            file_id TEXT,
            prime_only INTEGER DEFAULT 0,
            views INTEGER DEFAULT 0,
            added_by INTEGER,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            admin_id INTEGER,
            plan TEXT,
            days INTEGER,
            price INTEGER,
            status TEXT DEFAULT 'pending',
            file_id TEXT,
            created_at TEXT,
            decided_at TEXT
        );

        CREATE TABLE IF NOT EXISTS referrals (
            user_id INTEGER PRIMARY KEY,
            admin_id INTEGER,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            admin_id INTEGER,
            text TEXT,
            created_at TEXT
        );
        """
    )

    db.execute(
        """
        INSERT OR IGNORE INTO channels(username,title)
        VALUES(?,?)
        """,
        (
            DEFAULT_CHANNEL,
            "KinoCinema majburiy kanal"
        )
    )

    db.commit()


init_db()


# =========================================================
# KEYBOARDS
# =========================================================

def keyboard(rows):
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=item) for item in row]
            for row in rows
        ],
        resize_keyboard=True
    )


def back_keyboard():
    return keyboard([
        ["⬅️ Orqaga", "❌ Bekor qilish"]
    ])


def main_keyboard(user_id):
    rows = [
        ["🔎 Kino qidirish", "⭐ Prime status"],
        ["📚 Kinolar ro'yxati"],
        ["📸 Instagramga qaytish"],
        ["🎬 Kino buyurtma qilish"],
        ["🤝 Reklama & Bot olish"]
    ]

    if is_admin(user_id):
        rows.append(["👨‍💻 Admin panel"])

    return keyboard(rows)


def admin_keyboard():
    return keyboard([
        ["🎬 Kino qo'shish", "🗑 Kino o'chirish"],
        ["📚 Kinolar", "📊 Statistika"],
        ["💳 Karta sozlamalari", "📢 Kanallar"],
        ["👥 Adminlar", "🔗 Mening referralim"],
        ["🏠 Asosiy menyu"]
    ])


def card_keyboard():
    return keyboard([
        ["➕ Karta qo'shish", "🔄 Kartani almashtirish"],
        ["👀 Hozirgi kartani ko'rish", "🗑 Kartani o'chirish"],
        ["⬅️ Admin panel"]
    ])


def channel_keyboard():
    return keyboard([
        ["➕ Kanal qo'shish", "🗑 Kanal o'chirish"],
        ["📋 Kanallar"],
        ["⬅️ Admin panel"]
    ])


def subscription_keyboard():
    rows = []

    channels = dbq(
        "SELECT * FROM channels ORDER BY id",
        all=True
    )

    for ch in channels:
        rows.append([
            InlineKeyboardButton(
                text="📢 " + (ch["title"] or ch["username"]),
                url="https://t.me/" + ch["username"].lstrip("@")
            )
        ])

    rows.append([
        InlineKeyboardButton(
            text="✅ Obuna bo'ldim",
            callback_data="subscription_check"
        )
    ])

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


# =========================================================
# SUBSCRIPTION
# =========================================================

async def is_subscribed(user_id):
    channels = dbq(
        "SELECT username FROM channels",
        all=True
    )

    for ch in channels:
        try:
            member = await bot.get_chat_member(
                ch["username"],
                user_id
            )

            if member.status in {
                ChatMemberStatus.LEFT,
                ChatMemberStatus.KICKED
            }:
                return False

        except Exception:
            return False

    return True


async def require_subscription(message):
    # Adminlar kanal tekshiruvidan o'tadi
    if is_admin(message.from_user.id):
        return True

    if await is_subscribed(message.from_user.id):
        return True

    await message.answer(
        "🔒 <b>Avval majburiy kanalga obuna bo'ling.</b>\n\n"
        "Obuna bo'lgach, "
        "<b>✅ Obuna bo'ldim</b> tugmasini bosing.",
        reply_markup=subscription_keyboard()
    )

    return False


class SubscriptionMiddleware(BaseMiddleware):

    async def __call__(self, handler, event, data):

        user = getattr(event, "from_user", None)

        if not user:
            return await handler(event, data)

        # /start ishlashi shart
        if isinstance(event, Message):
            if (event.text or "").startswith("/start"):
                return await handler(event, data)

        # Obuna tekshirish tugmasi ishlashi shart
        if isinstance(event, CallbackQuery):
            if event.data == "subscription_check":
                return await handler(event, data)

        # Adminlar bloklanmaydi
        if is_admin(user.id):
            return await handler(event, data)

        # Obuna bor
        if await is_subscribed(user.id):
            return await handler(event, data)

        # Obuna yo'q
        if isinstance(event, CallbackQuery):
            await event.answer(
                "❌ Avval kanalga obuna bo'ling.",
                show_alert=True
            )
            return

        await event.answer(
            "🔒 <b>Avval majburiy kanalga obuna bo'ling.</b>",
            reply_markup=subscription_keyboard()
        )


router.message.outer_middleware(
    SubscriptionMiddleware()
)

router.callback_query.outer_middleware(
    SubscriptionMiddleware()
)


# =========================================================
# USER / REFERRAL
# =========================================================

def ensure_user(message, referred_by=None):

    u = message.from_user
    old = get_user(u.id)

    if old:
        dbq(
            """
            UPDATE users
            SET username=?, first_name=?
            WHERE user_id=?
            """,
            (
                u.username or "",
                u.first_name or "",
                u.id
            ),
            commit=True
        )

        return

    dbq(
        """
        INSERT INTO users(
            user_id,
            username,
            first_name,
            prime_until,
            referred_by,
            created_at
        )
        VALUES(?,?,?,?,?,?)
        """,
        (
            u.id,
            u.username or "",
            u.first_name or "",
            None,
            referred_by,
            now_iso()
        ),
        commit=True
    )

    if referred_by:
        dbq(
            """
            INSERT OR IGNORE INTO referrals(
                user_id,
                admin_id,
                created_at
            )
            VALUES(?,?,?)
            """,
            (
                u.id,
                referred_by,
                now_iso()
            ),
            commit=True
        )


def payment_admin(user_id):

    row = dbq(
        """
        SELECT admin_id
        FROM referrals
        WHERE user_id=?
        """,
        (user_id,),
        one=True
    )

    if row and is_admin(row["admin_id"]):
        return row["admin_id"]

    return get_owner_id()


# =========================================================
# FSM
# =========================================================

class SearchState(StatesGroup):
    code = State()


class MovieAddState(StatesGroup):
    code = State()
    title = State()
    video = State()
    kind = State()


class MovieDeleteState(StatesGroup):
    code = State()


class CardState(StatesGroup):
    number = State()
    owner = State()


class ChannelAddState(StatesGroup):
    username = State()


class ChannelDeleteState(StatesGroup):
    username = State()


class AdminAddState(StatesGroup):
    user_id = State()


class AdminDeleteState(StatesGroup):
    user_id = State()


class PaymentState(StatesGroup):
    screenshot = State()


class OrderState(StatesGroup):
    text = State()


# =========================================================
# START
# =========================================================

@router.message(CommandStart())
async def start(message: Message, state: FSMContext):

    await state.clear()

    ref = None

    parts = (message.text or "").split(maxsplit=1)

    if len(parts) == 2:
        arg = parts[1].strip()

        if arg.startswith("ref_"):
            try:
                ref = int(arg[4:])
            except Exception:
                ref = None

    ensure_user(message, ref)

    if not await require_subscription(message):
        return

    await message.answer(
        "🎬 <b>KinoCinema botiga xush kelibsiz!</b>\n\n"
        "Kerakli bo'limni tanlang:",
        reply_markup=main_keyboard(message.from_user.id)
    )


@router.callback_query(F.data == "subscription_check")
async def subscription_check(
    callback: CallbackQuery,
    state: FSMContext
):

    await callback.answer()

    if await is_subscribed(callback.from_user.id):

        await state.clear()

        await callback.message.answer(
            "✅ <b>Obuna tasdiqlandi!</b>\n\n"
            "Kerakli bo'limni tanlang.",
            reply_markup=main_keyboard(
                callback.from_user.id
            )
        )

    else:

        await callback.message.answer(
            "❌ Hali majburiy kanalga obuna bo'lmagansiz.",
            reply_markup=subscription_keyboard()
        )


# =========================================================
# BACK / CANCEL
# =========================================================

@router.message(
    F.text.in_({
        "⬅️ Orqaga",
        "❌ Bekor qilish"
    })
)
async def back_cancel(
    message: Message,
    state: FSMContext
):

    await state.clear()

    if (
        message.text == "⬅️ Orqaga"
        and is_admin(message.from_user.id)
    ):
        await message.answer(
            "👨‍💻 <b>Admin panel</b>",
            reply_markup=admin_keyboard()
        )
        return

    await message.answer(
        "↩️ Bekor qilindi.",
        reply_markup=main_keyboard(
            message.from_user.id
        )
    )


@router.message(F.text == "🏠 Asosiy menyu")
async def home(
    message: Message,
    state: FSMContext
):

    await state.clear()

    await message.answer(
        "🏠 <b>Asosiy menyu</b>",
        reply_markup=main_keyboard(
            message.from_user.id
        )
    )


# =========================================================
# MOVIE SEARCH
# =========================================================

@router.message(F.text == "🔎 Kino qidirish")
async def movie_search_start(
    message: Message,
    state: FSMContext
):

    await state.set_state(
        SearchState.code
    )

    await message.answer(
        "🔎 <b>Kino kodini yuboring.</b>\n\n"
        "Masalan: <code>247</code>",
        reply_markup=back_keyboard()
    )


@router.message(SearchState.code, F.text)
async def movie_search_code(
    message: Message,
    state: FSMContext
):

    code = message.text.strip()

    movie = dbq(
        """
        SELECT *
        FROM movies
        WHERE lower(code)=lower(?)
        """,
        (code,),
        one=True
    )

    await state.clear()

    if not movie:

        await message.answer(
            "❌ Bunday kino topilmadi.",
            reply_markup=main_keyboard(
                message.from_user.id
            )
        )
        return

    if movie["prime_only"] and not prime_active(
        message.from_user.id
    ):

        await message.answer(
            "⭐ Bu kino faqat Prime uchun.",
            reply_markup=main_keyboard(
                message.from_user.id
            )
        )
        return

    dbq(
        """
        UPDATE movies
        SET views=views+1
        WHERE id=?
        """,
        (movie["id"],),
        commit=True
    )

    await bot.send_video(
        message.chat.id,
        movie["file_id"],
        caption=(
            f"🎬 <b>{movie['title']}</b>\n"
            f"🔢 Kod: <code>{movie['code']}</code>\n\n"
            "🍿 Yoqimli tomosha!"
        )
    )


# =========================================================
# MOVIE LIST
# =========================================================

@router.message(F.text == "📚 Kinolar ro'yxati")
async def movie_list(message: Message):

    movies = dbq(
        """
        SELECT code,title,prime_only,views
        FROM movies
        ORDER BY id DESC
        """,
        all=True
    )

    if not movies:

        await message.answer(
            "📚 Hozircha kino yo'q."
        )
        return

    text = "📚 <b>Kinolar ro'yxati</b>\n\n"

    for movie in movies:

        star = "⭐ " if movie["prime_only"] else ""

        text += (
            f"{star}<code>{movie['code']}</code> — "
            f"{movie['title']} | 👁 {movie['views']}\n"
        )

    await message.answer(text)


# =========================================================
# PRIME
# =========================================================

PLANS = {
    "7 kun": (7, 7000),
    "1 oy": (30, 20000),
    "3 oy": (90, 50000),
    "Umrbod": (36500, 150000)
}


@router.message(F.text == "⭐ Prime status")
async def prime_status(message: Message):

    if prime_active(message.from_user.id):

        user = get_user(message.from_user.id)

        await message.answer(
            "⭐ <b>Prime status</b>\n\n"
            f"✅ Faol\n"
            f"📅 Tugash vaqti:\n"
            f"<code>{user['prime_until']}</code>",
            reply_markup=keyboard([
                ["💳 Prime sotib olish"],
                ["🏠 Asosiy menyu"]
            ])
        )

    else:

        await message.answer(
            "⭐ <b>Prime status</b>\n\n"
            "❌ Prime faol emas.",
            reply_markup=keyboard([
                ["💳 Prime sotib olish"],
                ["🏠 Asosiy menyu"]
            ])
        )


@router.message(F.text == "💳 Prime sotib olish")
async def prime_buy(message: Message):

    rows = []

    for name, (_, price) in PLANS.items():

        rows.append([
            KeyboardButton(
                text=f"{name} — {money(price)} so'm"
            )
        ])

    rows.append([
        KeyboardButton(
            text="🏠 Asosiy menyu"
        )
    ])

    await message.answer(
        "⭐ <b>Prime tarifini tanlang:</b>",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=rows,
            resize_keyboard=True
        )
    )


@router.message(
    F.text.regexp(
        r"^(7 kun|1 oy|3 oy|Umrbod) — "
    )
)
async def choose_plan(
    message: Message,
    state: FSMContext
):

    plan_name = message.text.split(" — ")[0]

    days, price = PLANS[plan_name]

    admin_id = payment_admin(
        message.from_user.id
    )

    if not admin_id:

        await message.answer(
            "❌ Admin topilmadi."
        )
        return

    card = dbq(
        """
        SELECT *
        FROM cards
        WHERE admin_id=?
        """,
        (admin_id,),
        one=True
    )

    if not card:

        await message.answer(
            "❌ Sizga xizmat ko'rsatuvchi admin "
            "hali karta qo'shmagan."
        )
        return

    await state.update_data(
        plan=plan_name,
        days=days,
        price=price,
        admin_id=admin_id
    )

    await state.set_state(
        PaymentState.screenshot
    )

    await message.answer(
        "💳 <b>To'lov ma'lumotlari</b>\n\n"
        f"💳 Karta: <code>{card['card_number']}</code>\n"
        f"👤 Egasi: <b>{card['card_owner']}</b>\n"
        f"💰 Narx: <b>{money(price)} so'm</b>\n\n"
        "📸 To'lov screenshotini "
        "<b>rasm ko'rinishida</b> yuboring.",
        reply_markup=back_keyboard()
    )


@router.message(
    PaymentState.screenshot,
    F.photo
)
async def payment_screenshot(
    message: Message,
    state: FSMContext
):

    data = await state.get_data()

    file_id = message.photo[-1].file_id

    cur = dbq(
        """
        INSERT INTO payments(
            user_id,
            admin_id,
            plan,
            days,
            price,
            status,
            file_id,
            created_at
        )
        VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            message.from_user.id,
            data["admin_id"],
            data["plan"],
            data["days"],
            data["price"],
            "pending",
            file_id,
            now_iso()
        ),
        commit=True
    )

    payment_id = cur.lastrowid

    await state.clear()

    buttons = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Tasdiqlash",
                    callback_data=f"payment_ok_{payment_id}"
                ),
                InlineKeyboardButton(
                    text="❌ Bekor qilish",
                    callback_data=f"payment_no_{payment_id}"
                )
            ]
        ]
    )

    caption = (
        "🧾 <b>Yangi PRIME to'lov!</b>\n\n"
        f"👤 User ID: <code>{message.from_user.id}</code>\n"
        f"📦 Plan: <b>{data['plan']}</b>\n"
        f"📅 Muddat: <b>{data['days']} kun</b>\n"
        f"💰 Narx: <b>{money(data['price'])} so'm</b>\n"
        f"🆔 Payment ID: <code>{payment_id}</code>"
    )

    try:

        await bot.send_photo(
            data["admin_id"],
            file_id,
            caption=caption,
            reply_markup=buttons
        )

    except Exception:
        pass

    await message.answer(
        "⏳ <b>To'lovingiz tekshirilmoqda.</b>\n\n"
        "Admin tasdiqlagandan keyin Prime avtomatik ochiladi.",
        reply_markup=main_keyboard(
            message.from_user.id
        )
    )


@router.message(PaymentState.screenshot)
async def wrong_payment_file(
    message: Message
):

    await message.answer(
        "❌ Iltimos, to'lov screenshotini "
        "<b>rasm</b> sifatida yuboring.",
        reply_markup=back_keyboard()
    )


# =========================================================
# PAYMENT APPROVE / REJECT
# =========================================================

@router.callback_query(
    F.data.regexp(
        r"^payment_(ok|no)_\d+$"
    )
)
async def payment_decision(
    callback: CallbackQuery
):

    parts = callback.data.split("_")

    action = parts[1]
    payment_id = int(parts[2])

    payment = dbq(
        """
        SELECT *
        FROM payments
        WHERE id=?
        """,
        (payment_id,),
        one=True
    )

    if not payment:

        await callback.answer(
            "❌ To'lov topilmadi.",
            show_alert=True
        )
        return

    if payment["admin_id"] != callback.from_user.id:

        await callback.answer(
            "❌ Bu to'lov sizga tegishli emas.",
            show_alert=True
        )
        return

    if payment["status"] != "pending":

        await callback.answer(
            "Bu to'lov allaqachon ko'rilgan.",
            show_alert=True
        )
        return

    if action == "ok":

        user = get_user(
            payment["user_id"]
        )

        old_until = None

        if user and user["prime_until"]:

            try:
                old_until = datetime.fromisoformat(
                    user["prime_until"]
                )
            except Exception:
                old_until = None

        base = max(
            old_until or datetime.now(timezone.utc),
            datetime.now(timezone.utc)
        )

        until = (
            base +
            timedelta(days=payment["days"])
        ).isoformat()

        dbq(
            """
            UPDATE users
            SET prime_until=?
            WHERE user_id=?
            """,
            (
                until,
                payment["user_id"]
            ),
            commit=True
        )

        dbq(
            """
            UPDATE payments
            SET status='approved',
                decided_at=?
            WHERE id=?
            """,
            (
                now_iso(),
                payment_id
            ),
            commit=True
        )

        try:

            await bot.send_message(
                payment["user_id"],
                "✅ <b>To'lov tasdiqlandi!</b>\n\n"
                f"⭐ Prime: <b>{payment['plan']}</b>\n"
                f"📅 Muddat: <code>{until}</code>"
            )

        except Exception:
            pass

        try:
            await callback.message.edit_caption(
                caption=(
                    (callback.message.caption or "")
                    + "\n\n✅ <b>TASDIQLANDI</b>"
                )
            )
        except Exception:
            pass

        await callback.answer(
            "✅ Prime ochildi."
        )

    else:

        dbq(
            """
            UPDATE payments
            SET status='rejected',
                decided_at=?
            WHERE id=?
            """,
            (
                now_iso(),
                payment_id
            ),
            commit=True
        )

        try:

            await bot.send_message(
                payment["user_id"],
                "❌ <b>To'lov bekor qilindi.</b>\n\n"
                "Prime ochilmadi."
            )

        except Exception:
            pass

        try:
            await callback.message.edit_caption(
                caption=(
                    (callback.message.caption or "")
                    + "\n\n❌ <b>BEKOR QILINDI</b>"
                )
            )
        except Exception:
            pass

        await callback.answer(
            "❌ To'lov bekor qilindi."
        )


# =========================================================
# ADMIN PANEL
# =========================================================

@router.message(F.text == "👨‍💻 Admin panel")
async def admin_panel(
    message: Message,
    state: FSMContext
):

    if not is_admin(message.from_user.id):
        return

    await state.clear()

    await message.answer(
        "👨‍💻 <b>Admin panel</b>\n\n"
        "Kerakli bo'limni tanlang:",
        reply_markup=admin_keyboard()
    )


# =========================================================
# CARD SETTINGS
# =========================================================

@router.message(F.text == "💳 Karta sozlamalari")
async def card_settings(
    message: Message,
    state: FSMContext
):

    if not is_admin(message.from_user.id):
        return

    await state.clear()

    card = dbq(
        """
        SELECT *
        FROM cards
        WHERE admin_id=?
        """,
        (message.from_user.id,),
        one=True
    )

    if card:

        text = (
            "💳 <b>Sizning kartangiz</b>\n\n"
            f"💳 <code>{card['card_number']}</code>\n"
            f"👤 {card['card_owner']}"
        )

    else:

        text = (
            "💳 <b>Karta sozlamalari</b>\n\n"
            "❌ Hozircha karta qo'shilmagan."
        )

    await message.answer(
        text,
        reply_markup=card_keyboard()
    )


@router.message(
    F.text.in_({
        "➕ Karta qo'shish",
        "🔄 Kartani almashtirish"
    })
)
async def card_start(
    message: Message,
    state: FSMContext
):

    if not is_admin(message.from_user.id):
        return

    await state.set_state(
        CardState.number
    )

    await message.answer(
        "💳 <b>Karta raqamini yuboring.</b>\n\n"
        "12–19 ta raqam.\n\n"
        "Masalan:\n"
        "<code>9860600435412504</code>",
        reply_markup=back_keyboard()
    )


@router.message(
    CardState.number,
    F.text
)
async def card_number(
    message: Message,
    state: FSMContext
):

    number = re.sub(
        r"\D",
        "",
        message.text
    )

    if not 12 <= len(number) <= 19:

        await message.answer(
            "❌ Karta raqami noto'g'ri.\n"
            "12–19 ta raqam yuboring.",
            reply_markup=back_keyboard()
        )
        return

    await state.update_data(
        card_number=number
    )

    await state.set_state(
        CardState.owner
    )

    await message.answer(
        "👤 <b>Karta egasining ism-familiyasini yuboring.</b>\n\n"
        "Masalan: Muhammad Ali.O.",
        reply_markup=back_keyboard()
    )


@router.message(
    CardState.owner,
    F.text
)
async def card_owner(
    message: Message,
    state: FSMContext
):

    owner_name = message.text.strip()

    if len(owner_name) < 2:

        await message.answer(
            "❌ Ism noto'g'ri.",
            reply_markup=back_keyboard()
        )
        return

    data = await state.get_data()

    dbq(
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
            message.from_user.id,
            data["card_number"],
            owner_name,
            now_iso()
        ),
        commit=True
    )

    await state.clear()

    await message.answer(
        "✅ <b>Karta muvaffaqiyatli saqlandi.</b>\n\n"
        "Bu karta faqat sizning admin profilingizga tegishli.",
        reply_markup=card_keyboard()
    )


@router.message(
    F.text == "👀 Hozirgi kartani ko'rish"
)
async def card_view(message: Message):

    card = dbq(
        """
        SELECT *
        FROM cards
        WHERE admin_id=?
        """,
        (message.from_user.id,),
        one=True
    )

    if not card:

        await message.answer(
            "❌ Karta yo'q.",
            reply_markup=card_keyboard()
        )
        return

    await message.answer(
        "💳 <b>Sizning kartangiz</b>\n\n"
        f"💳 <code>{card['card_number']}</code>\n"
        f"👤 {card['card_owner']}",
        reply_markup=card_keyboard()
    )


@router.message(
    F.text == "🗑 Kartani o'chirish"
)
async def card_delete(message: Message):

    dbq(
        "DELETE FROM cards WHERE admin_id=?",
        (message.from_user.id,),
        commit=True
    )

    await message.answer(
        "🗑 Karta o'chirildi.",
        reply_markup=card_keyboard()
    )


# =========================================================
# CHANNEL MANAGEMENT
# =========================================================

@router.message(F.text == "📢 Kanallar")
async def channels_menu(
    message: Message,
    state: FSMContext
):

    if not is_admin(message.from_user.id):
        return

    await state.clear()

    await message.answer(
        "📢 <b>Kanal sozlamalari</b>",
        reply_markup=channel_keyboard()
    )


@router.message(F.text == "📋 Kanallar")
async def channels_list(message: Message):

    channels = dbq(
        "SELECT * FROM channels ORDER BY id",
        all=True
    )

    if not channels:

        await message.answer(
            "❌ Kanal yo'q.",
            reply_markup=channel_keyboard()
        )
        return

    text = "📢 <b>Majburiy kanallar:</b>\n\n"

    for ch in channels:
        text += (
            f"🆔 {ch['id']} — "
            f"{ch['username']}\n"
        )

    await message.answer(
        text,
        reply_markup=channel_keyboard()
    )


@router.message(F.text == "➕ Kanal qo'shish")
async def channel_add(
    message: Message,
    state: FSMContext
):

    if not is_admin(message.from_user.id):
        return

    await state.set_state(
        ChannelAddState.username
    )

    await message.answer(
        "📢 <b>Kanal username yoki havolasini yuboring.</b>\n\n"
        "Masalan:\n"
        "<code>@uz_kinocinema</code>",
        reply_markup=back_keyboard()
    )


@router.message(
    ChannelAddState.username,
    F.text
)
async def channel_add_save(
    message: Message,
    state: FSMContext
):

    username = normalize_channel(
        message.text
    )

    try:

        chat = await bot.get_chat(
            username
        )

        await bot.get_chat_member(
            username,
            message.from_user.id
        )

        dbq(
            """
            INSERT OR IGNORE INTO channels(
                username,
                title
            )
            VALUES(?,?)
            """,
            (
                username,
                chat.title or username
            ),
            commit=True
        )

        await state.clear()

        await message.answer(
            "✅ <b>Kanal qo'shildi.</b>",
            reply_markup=channel_keyboard()
        )

    except Exception:

        await message.answer(
            "❌ Kanal topilmadi yoki bot "
            "kanalda administrator emas.",
            reply_markup=back_keyboard()
        )


@router.message(F.text == "🗑 Kanal o'chirish")
async def channel_delete(
    message: Message,
    state: FSMContext
):

    if not is_admin(message.from_user.id):
        return

    await state.set_state(
        ChannelDeleteState.username
    )

    await message.answer(
        "🗑 <b>O'chiriladigan kanal usernameini yuboring.</b>\n\n"
        "Masalan: <code>@uz_kinocinema</code>",
        reply_markup=back_keyboard()
    )


@router.message(
    ChannelDeleteState.username,
    F.text
)
async def channel_delete_save(
    message: Message,
    state: FSMContext
):

    username = normalize_channel(
        message.text
    )

    dbq(
        "DELETE FROM channels WHERE username=?",
        (username,),
        commit=True
    )

    await state.clear()

    await message.answer(
        "🗑 <b>Kanal o'chirildi.</b>",
        reply_markup=channel_keyboard()
    )


# =========================================================
# ADMIN MANAGEMENT
# FAQAT OWNER
# =========================================================

@router.message(F.text == "👥 Adminlar")
async def admins_menu(message: Message):

    if not is_owner(message.from_user.id):
        return

    await message.answer(
        "👥 <b>Adminlar</b>",
        reply_markup=keyboard([
            ["➕ Admin qo'shish", "🗑 Admin o'chirish"],
            ["📋 Adminlar"],
            ["⬅️ Admin panel"]
        ])
    )


@router.message(F.text == "➕ Admin qo'shish")
async def admin_add(
    message: Message,
    state: FSMContext
):

    if not is_owner(message.from_user.id):
        return

    await state.set_state(
        AdminAddState.user_id
    )

    await message.answer(
        "👤 <b>Admin qilinadigan odamning Telegram ID sini yuboring.</b>\n\n"
        "U odam avval botga /start bergan bo'lishi kerak.",
        reply_markup=back_keyboard()
    )


@router.message(
    AdminAddState.user_id,
    F.text
)
async def admin_add_save(
    message: Message,
    state: FSMContext
):

    if not is_owner(message.from_user.id):
        return

    try:
        user_id = int(
            message.text.strip()
        )
    except Exception:

        await message.answer(
            "❌ Telegram ID noto'g'ri.",
            reply_markup=back_keyboard()
        )
        return

    user = get_user(user_id)

    if not user:

        await message.answer(
            "❌ Bu foydalanuvchi hali botga "
            "/start bermagan.",
            reply_markup=back_keyboard()
        )
        return

    dbq(
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
            user["username"] or "",
            message.from_user.id,
            now_iso()
        ),
        commit=True
    )

    await state.clear()

    await message.answer(
        "✅ <b>Admin qo'shildi.</b>",
        reply_markup=admin_keyboard()
    )


@router.message(F.text == "🗑 Admin o'chirish")
async def admin_delete(
    message: Message,
    state: FSMContext
):

    if not is_owner(message.from_user.id):
        return

    await state.set_state(
        AdminDeleteState.user_id
    )

    await message.answer(
        "🗑 Adminning Telegram ID sini yuboring.",
        reply_markup=back_keyboard()
    )


@router.message(
    AdminDeleteState.user_id,
    F.text
)
async def admin_delete_save(
    message: Message,
    state: FSMContext
):

    if not is_owner(message.from_user.id):
        return

    try:
        user_id = int(
            message.text.strip()
        )
    except Exception:

        await message.answer(
            "❌ ID noto'g'ri.",
            reply_markup=back_keyboard()
        )
        return

    dbq(
        "DELETE FROM admins WHERE user_id=?",
        (user_id,),
        commit=True
    )

    await state.clear()

    await message.answer(
        "🗑 <b>Admin o'chirildi.</b>",
        reply_markup=admin_keyboard()
    )


@router.message(F.text == "📋 Adminlar")
async def admin_list(message: Message):

    if not is_owner(message.from_user.id):
        return

    admins = dbq(
        "SELECT * FROM admins ORDER BY created_at",
        all=True
    )

    if not admins:

        await message.answer(
            "👥 Hozircha boshqa admin yo'q."
        )
        return

    text = "👥 <b>Adminlar:</b>\n\n"

    for a in admins:

        username = (
            "@"
            + a["username"]
            if a["username"]
            else "username yo'q"
        )

        text += (
            f"👤 {username}\n"
            f"🆔 <code>{a['user_id']}</code>\n\n"
        )

    await message.answer(text)


# =========================================================
# REFERRAL
# =========================================================

@router.message(F.text == "🔗 Mening referralim")
async def my_referral(message: Message):

    if not is_admin(message.from_user.id):
        return

    link = await create_start_link(
        bot,
        f"ref_{message.from_user.id}",
        encode=False
    )

    users_count = dbq(
        """
        SELECT COUNT(*) AS c
        FROM referrals
        WHERE admin_id=?
        """,
        (message.from_user.id,),
        one=True
    )["c"]

    income = dbq(
        """
        SELECT COALESCE(SUM(price),0) AS s
        FROM payments
        WHERE admin_id=?
        AND status='approved'
        """,
        (message.from_user.id,),
        one=True
    )["s"]

    await message.answer(
        "🔗 <b>Sizning referral linkingiz</b>\n\n"
        f"<code>{link}</code>\n\n"
        f"👥 Silka orqali kirganlar: "
        f"<b>{users_count}</b>\n"
        f"💰 Tasdiqlangan tushum: "
        f"<b>{money(income)} so'm</b>"
    )


# =========================================================
# STATISTIKA
# =========================================================

@router.message(F.text == "📊 Statistika")
async def statistics(message: Message):

    if not is_admin(message.from_user.id):
        return

    users = dbq(
        "SELECT COUNT(*) AS c FROM users",
        one=True
    )["c"]

    movies = dbq(
        "SELECT COUNT(*) AS c FROM movies",
        one=True
    )["c"]

    referrals = dbq(
        """
        SELECT COUNT(*) AS c
        FROM referrals
        WHERE admin_id=?
        """,
        (message.from_user.id,),
        one=True
    )["c"]

    income = dbq(
        """
        SELECT COALESCE(SUM(price),0) AS s
        FROM payments
        WHERE admin_id=?
        AND status='approved'
        """,
        (message.from_user.id,),
        one=True
    )["s"]

    await message.answer(
        "📊 <b>Statistika</b>\n\n"
        f"👥 Umumiy foydalanuvchilar: "
        f"<b>{users}</b>\n"
        f"🎬 Kinolar: <b>{movies}</b>\n"
        f"🔗 Sizning referral foydalanuvchilaringiz: "
        f"<b>{referrals}</b>\n"
        f"💰 Sizning tushumingiz: "
        f"<b>{money(income)} so'm</b>"
    )


# =========================================================
# ADD MOVIE
# =========================================================

@router.message(F.text == "🎬 Kino qo'shish")
async def movie_add_start(
    message: Message,
    state: FSMContext
):

    if not is_admin(message.from_user.id):
        return

    await state.set_state(
        MovieAddState.code
    )

    await message.answer(
        "🎬 <b>Kino qo'shish — 1/4</b>\n\n"
        "Kino kodini yuboring.\n"
        "Masalan: <code>247</code>",
        reply_markup=back_keyboard()
    )


@router.message(
    MovieAddState.code,
    F.text
)
async def movie_add_code(
    message: Message,
    state: FSMContext
):

    code = message.text.strip()

    if not re.fullmatch(
        r"[A-Za-z0-9_-]{1,30}",
        code
    ):

        await message.answer(
            "❌ Kod noto'g'ri.",
            reply_markup=back_keyboard()
        )
        return

    exists = dbq(
        """
        SELECT 1
        FROM movies
        WHERE lower(code)=lower(?)
        """,
        (code,),
        one=True
    )

    if exists:

        await message.answer(
            "❌ Bu kod allaqachon mavjud.",
            reply_markup=back_keyboard()
        )
        return

    await state.update_data(
        code=code
    )

    await state.set_state(
        MovieAddState.title
    )

    await message.answer(
        "🎬 <b>Kino nomini yuboring.</b>",
        reply_markup=back_keyboard()
    )


@router.message(
    MovieAddState.title,
    F.text
)
async def movie_add_title(
    message: Message,
    state: FSMContext
):

    title = message.text.strip()

    if not title:

        await message.answer(
            "❌ Kino nomini yuboring.",
            reply_markup=back_keyboard()
        )
        return

    await state.update_data(
        title=title
    )

    await state.set_state(
        MovieAddState.video
    )

    await message.answer(
        "🎥 <b>Kino videosini yuboring.</b>",
        reply_markup=back_keyboard()
    )


@router.message(
    MovieAddState.video,
    F.video
)
async def movie_add_video(
    message: Message,
    state: FSMContext
):

    await state.update_data(
        file_id=message.video.file_id
    )

    await state.set_state(
        MovieAddState.kind
    )

    await message.answer(
        "🔐 <b>Kino turini tanlang:</b>",
        reply_markup=keyboard([
            ["🆓 Oddiy kino", "⭐ Faqat Prime"],
            ["❌ Bekor qilish"]
        ])
    )


@router.message(
    MovieAddState.video
)
async def movie_add_wrong_video(
    message: Message
):

    await message.answer(
        "❌ Iltimos, kinoni <b>video</b> sifatida yuboring.",
        reply_markup=back_keyboard()
    )


@router.message(
    MovieAddState.kind,
    F.text.in_({
        "🆓 Oddiy kino",
        "⭐ Faqat Prime"
    })
)
async def movie_add_finish(
    message: Message,
    state: FSMContext
):

    data = await state.get_data()

    prime_only = (
        1
        if message.text == "⭐ Faqat Prime"
        else 0
    )

    dbq(
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
            message.from_user.id,
            now_iso()
        ),
        commit=True
    )

    await state.clear()

    await message.answer(
        "✅ <b>Kino qo'shildi!</b>\n\n"
        f"🔢 Kod: <code>{data['code']}</code>\n"
        f"🎬 Nomi: <b>{data['title']}</b>\n"
        f"🔐 Turi: "
        f"{'⭐ Prime' if prime_only else '🆓 Oddiy'}",
        reply_markup=admin_keyboard()
    )


# =========================================================
# DELETE MOVIE
# =========================================================

@router.message(F.text == "🗑 Kino o'chirish")
async def movie_delete_start(
    message: Message,
    state: FSMContext
):

    if not is_admin(message.from_user.id):
        return

    await state.set_state(
        MovieDeleteState.code
    )

    await message.answer(
        "🗑 <b>Kino o'chirish</b>\n\n"
        "Kino kodini yuboring.",
        reply_markup=back_keyboard()
    )


@router.message(
    MovieDeleteState.code,
    F.text
)
async def movie_delete_finish(
    message: Message,
    state: FSMContext
):

    code = message.text.strip()

    movie = dbq(
        """
        SELECT *
        FROM movies
        WHERE lower(code)=lower(?)
        """,
        (code,),
        one=True
    )

    if not movie:

        await message.answer(
            "❌ Bunday kino topilmadi.",
            reply_markup=back_keyboard()
        )
        return

    dbq(
        "DELETE FROM movies WHERE id=?",
        (movie["id"],),
        commit=True
    )

    await state.clear()

    await message.answer(
        f"🗑 <b>{movie['title']}</b> o'chirildi.",
        reply_markup=admin_keyboard()
    )


# =========================================================
# ADMIN MOVIES
# =========================================================

@router.message(F.text == "📚 Kinolar")
async def admin_movies(message: Message):

    if not is_admin(message.from_user.id):
        return

    movies = dbq(
        """
        SELECT code,title,views,prime_only
        FROM movies
        ORDER BY id DESC
        """,
        all=True
    )

    if not movies:

        await message.answer(
            "📚 Kino yo'q.",
            reply_markup=admin_keyboard()
        )
        return

    text = "📚 <b>Kinolar</b>\n\n"

    for movie in movies:

        star = "⭐ " if movie["prime_only"] else ""

        text += (
            f"{star}<code>{movie['code']}</code> — "
            f"{movie['title']} | "
            f"👁 {movie['views']}\n"
        )

    await message.answer(
        text,
        reply_markup=admin_keyboard()
    )


# =========================================================
# KINO BUYURTMA
# =========================================================

@router.message(F.text == "🎬 Kino buyurtma qilish")
async def order_start(
    message: Message,
    state: FSMContext
):

    await state.set_state(
        OrderState.text
    )

    await message.answer(
        "🎬 <b>Kino buyurtma qilish</b>\n\n"
        "Qaysi kino kerakligini yozing:",
        reply_markup=back_keyboard()
    )


@router.message(
    OrderState.text,
    F.text
)
async def order_finish(
    message: Message,
    state: FSMContext
):

    admin_id = payment_admin(
        message.from_user.id
    )

    dbq(
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
            message.text.strip(),
            now_iso()
        ),
        commit=True
    )

    if admin_id:

        try:

            await bot.send_message(
                admin_id,
                "🎬 <b>Yangi kino buyurtmasi!</b>\n\n"
                f"👤 User: "
                f"<code>{message.from_user.id}</code>\n"
                f"📝 So'rov: "
                f"<b>{message.text.strip()}</b>"
            )

        except Exception:
            pass

    await state.clear()

    await message.answer(
        "✅ <b>Buyurtmangiz yuborildi.</b>",
        reply_markup=main_keyboard(
            message.from_user.id
        )
    )


# =========================================================
# INSTAGRAM / ADS
# =========================================================

@router.message(F.text == "📸 Instagramga qaytish")
async def instagram(message: Message):

    await message.answer(
        "📸 <b>Instagram</b>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📸 Instagramga o'tish",
                        url=INSTAGRAM_URL
                    )
                ]
            ]
        )
    )


@router.message(F.text == "🤝 Reklama & Bot olish")
async def ads(message: Message):

    await message.answer(
        "🤝 <b>Reklama & Bot olish</b>\n\n"
        "Admin: @omono_v"
    )


# =========================================================
# FALLBACK — KINO KODI
# =========================================================

@router.message()
async def fallback(
    message: Message,
    state: FSMContext
):

    # Agar biror FSM jarayoni davom etayotgan bo'lsa,
    # bu handler aralashmaydi.
    if await state.get_state():
        return

    text = (message.text or "").strip()

    if not text:
        return

    movie = dbq(
        """
        SELECT *
        FROM movies
        WHERE lower(code)=lower(?)
        """,
        (text,),
        one=True
    )

    if movie:

        if (
            movie["prime_only"]
            and not prime_active(
                message.from_user.id
            )
        ):

            await message.answer(
                "⭐ Bu kino faqat Prime uchun."
            )
            return

        dbq(
            """
            UPDATE movies
            SET views=views+1
            WHERE id=?
            """,
            (movie["id"],),
            commit=True
        )

        await bot.send_video(
            message.chat.id,
            movie["file_id"],
            caption=(
                f"🎬 <b>{movie['title']}</b>\n"
                f"🔢 Kod: <code>{movie['code']}</code>\n\n"
                "🍿 Yoqimli tomosha!"
            )
        )

        return

    await message.answer(
        "❓ <b>Buyruq topilmadi.</b>\n\n"
        "🔎 Kino qidirish tugmasini bosing "
        "yoki kino kodini yuboring.",
        reply_markup=main_keyboard(
            message.from_user.id
        )
    )


# =========================================================
# RENDER HEALTH SERVER
# =========================================================

async def health(request):
    return web.Response(
        text="KinoCinema OK"
    )


async def start_web_server():

    app = web.Application()

    app.router.add_get(
        "/",
        health
    )

    app.router.add_get(
        "/health",
        health
    )

    runner = web.AppRunner(app)

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        int(os.getenv("PORT", "10000"))
    )

    await site.start()

    return runner


# =========================================================
# START BOT
# =========================================================

async def main():

    await bot.delete_webhook(
        drop_pending_updates=True
    )

    await start_web_server()

    print("KinoCinema bot ishga tushdi.")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
