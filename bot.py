import os
import asyncio
import sqlite3
from datetime import datetime, timedelta

from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from aiogram.filters import Command, StateFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext


# =========================================================
# SOZLAMALAR
# =========================================================

TOKEN = os.getenv("BOT_TOKEN")

ADMIN_USERNAME = "omono_v"

INSTAGRAM_URL = "https://www.instagram.com/oemovie/"

DB_NAME = "kino_bot.db"


if not TOKEN:
    raise RuntimeError(
        "BOT_TOKEN topilmadi!"
    )


bot = Bot(TOKEN)
dp = Dispatcher()


# =========================================================
# DATABASE
# =========================================================

db = sqlite3.connect(
    DB_NAME,
    check_same_thread=False
)

db.row_factory = sqlite3.Row


def init_db():

    cur = db.cursor()

    # Kinolar
    cur.execute("""
        CREATE TABLE IF NOT EXISTS movies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            file_id TEXT NOT NULL,
            prime INTEGER DEFAULT 0,
            views INTEGER DEFAULT 0
        )
    """)

    # Foydalanuvchilar
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            prime_until TEXT
        )
    """)

    # Prime to'lovlar
    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            plan TEXT,
            days INTEGER,
            price INTEGER,
            status TEXT DEFAULT 'pending',
            created_at TEXT
        )
    """)

    # Kino buyurtmalari
    cur.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            text TEXT,
            created_at TEXT
        )
    """)

    # Sozlamalar
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # Majburiy kanallar
    cur.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            chat_id TEXT NOT NULL
        )
    """)

    db.commit()


init_db()


# =========================================================
# SETTINGS
# =========================================================

def get_setting(key, default=""):

    cur = db.cursor()

    cur.execute(
        "SELECT value FROM settings WHERE key = ?",
        (key,)
    )

    row = cur.fetchone()

    if row:
        return row["value"]

    return default


def set_setting(key, value):

    db.execute("""
        INSERT INTO settings(key, value)
        VALUES (?, ?)
        ON CONFLICT(key)
        DO UPDATE SET value = excluded.value
    """, (
        key,
        value
    ))

    db.commit()


def delete_setting(key):

    db.execute(
        "DELETE FROM settings WHERE key = ?",
        (key,)
    )

    db.commit()


# =========================================================
# ADMIN
# =========================================================

def save_admin(message: Message):

    username = message.from_user.username

    if username:

        if username.lower() == ADMIN_USERNAME.lower():

            set_setting(
                "admin_id",
                str(message.from_user.id)
            )


def is_admin_user(user_id):

    admin_id = get_setting(
        "admin_id"
    )

    if not admin_id:
        return False

    try:
        return user_id == int(admin_id)
    except:
        return False


def is_admin(message: Message):

    # Avval saqlangan ID
    if is_admin_user(
        message.from_user.id
    ):
        return True

    # Username orqali birinchi marta aniqlash
    username = message.from_user.username

    if username:

        if username.lower() == ADMIN_USERNAME.lower():
            return True

    return False


# =========================================================
# USER
# =========================================================

def save_user(message: Message):

    db.execute("""
        INSERT INTO users(
            user_id,
            username
        )
        VALUES (?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET username = excluded.username
    """, (
        message.from_user.id,
        message.from_user.username or ""
    ))

    db.commit()


# =========================================================
# PRIME
# =========================================================

def is_prime(user_id):

    cur = db.cursor()

    cur.execute("""
        SELECT prime_until
        FROM users
        WHERE user_id = ?
    """, (
        user_id,
    ))

    row = cur.fetchone()

    if not row:
        return False

    if not row["prime_until"]:
        return False

    try:

        until = datetime.fromisoformat(
            row["prime_until"]
        )

        return until > datetime.now()

    except:

        return False


def activate_prime(user_id, days):

    now = datetime.now()

    cur = db.cursor()

    cur.execute("""
        SELECT prime_until
        FROM users
        WHERE user_id = ?
    """, (
        user_id,
    ))

    row = cur.fetchone()

    if row and row["prime_until"]:

        try:

            old_until = datetime.fromisoformat(
                row["prime_until"]
            )

            if old_until > now:
                start = old_until
            else:
                start = now

        except:

            start = now

    else:

        start = now

    until = start + timedelta(
        days=days
    )

    db.execute("""
        UPDATE users
        SET prime_until = ?
        WHERE user_id = ?
    """, (
        until.isoformat(),
        user_id
    ))

    db.commit()

    return until


# =========================================================
# KANALLAR
# =========================================================

def get_channels():

    cur = db.cursor()

    cur.execute("""
        SELECT *
        FROM channels
        ORDER BY id ASC
    """)

    return cur.fetchall()


async def check_subscription(user_id):

    channels = get_channels()

    # Kanal umuman bo'lmasa
    if not channels:
        return True

    for channel in channels:

        try:

            member = await bot.get_chat_member(
                chat_id=channel["chat_id"],
                user_id=user_id
            )

            if member.status == "kicked":
                return False

            if member.status == "left":
                return False

            # restricted bo'lsa, lekin kanal a'zosi bo'lsa
            if (
                member.status == "restricted"
                and hasattr(member, "is_member")
                and not member.is_member
            ):
                return False

        except Exception as e:

            print(
                "OBUNA TEKSHIRISH XATOSI:",
                channel["username"],
                e
            )

            return False

    return True


def subscription_keyboard():

    buttons = []

    for channel in get_channels():

        username = channel["username"]

        if username.startswith("@"):
            link_name = username[1:]
        else:
            link_name = username

        buttons.append([
            InlineKeyboardButton(
                text=f"📢 {channel['title']}",
                url=f"https://t.me/{link_name}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            text="✅ Obuna bo'ldim",
            callback_data="check_subscription"
        )
    ])

    return InlineKeyboardMarkup(
        inline_keyboard=buttons
    )


async def show_subscription(message: Message):

    channels = get_channels()

    if not channels:
        return False

    text = (
        "🔐 <b>Botdan foydalanish uchun</b>\n\n"
        "Quyidagi kanallarga obuna bo'ling:\n\n"
    )

    for channel in channels:

        text += (
            f"📢 <b>{channel['title']}</b>\n"
        )

    text += (
        "\nObuna bo'lgach "
        "«✅ Obuna bo'ldim» tugmasini bosing."
    )

    await message.answer(
        text,
        reply_markup=subscription_keyboard(),
        parse_mode="HTML"
    )

    return True


# =========================================================
# ASOSIY MENU
# =========================================================

def main_menu(user_id=None):

    buttons = [

        [
            KeyboardButton(
                text="🔎 Kino qidirish"
            ),
            KeyboardButton(
                text="⭐ Prime status"
            )
        ],

        [
            KeyboardButton(
                text="📚 Kinolar ro'yxati"
            )
        ],

        [
            KeyboardButton(
                text="📸 Instagramga qaytish"
            )
        ],

        [
            KeyboardButton(
                text="🎬 Kino buyurtma qilish"
            )
        ],

        [
            KeyboardButton(
                text="🤝 Reklama & Bot olish"
            )
        ]

    ]

    if user_id and is_admin_user(user_id):

        buttons.append([
            KeyboardButton(
                text="👨‍💻 Admin panel"
            )
        ])

    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True
    )


# =========================================================
# ADMIN MENU
# =========================================================

def admin_menu():

    return ReplyKeyboardMarkup(
        keyboard=[

            [
                KeyboardButton(
                    text="➕ Kino qo'shish"
                ),
                KeyboardButton(
                    text="🗑 Kino o'chirish"
                )
            ],

            [
                KeyboardButton(
                    text="📢 Kanal qo'shish"
                ),
                KeyboardButton(
                    text="🗑 Kanal o'chirish"
                )
            ],

            [
                KeyboardButton(
                    text="📋 Kanallar"
                )
            ],

            [
                KeyboardButton(
                    text="💳 Karta sozlamalari"
                )
            ],

            [
                KeyboardButton(
                    text="📊 Statistika"
                ),
                KeyboardButton(
                    text="📚 Admin kinolar"
                )
            ],

            [
                KeyboardButton(
                    text="🏠 Asosiy menyu"
                )
            ]

        ],
        resize_keyboard=True
    )


# =========================================================
# BACK / CANCEL KEYBOARD
# =========================================================

def cancel_keyboard():

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="⬅️ Orqaga"
                ),
                KeyboardButton(
                    text="❌ Bekor qilish"
                )
            ]
        ],
        resize_keyboard=True
    )


def admin_cancel_keyboard():

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="⬅️ Admin panel"
                ),
                KeyboardButton(
                    text="❌ Bekor qilish"
                )
            ]
        ],
        resize_keyboard=True
    )


# =========================================================
# PRIME
# =========================================================

PLANS = {

    "7": {
        "name": "7 kun",
        "days": 7,
        "price": 7000
    },

    "30": {
        "name": "1 oy",
        "days": 30,
        "price": 20000
    },

    "90": {
        "name": "3 oy",
        "days": 90,
        "price": 50000
    },

    "36500": {
        "name": "Umrbod",
        "days": 36500,
        "price": 150000
    }

}


def prime_plans_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[

            [
                InlineKeyboardButton(
                    text="7 kun — 7 000 so'm",
                    callback_data="plan_7"
                )
            ],

            [
                InlineKeyboardButton(
                    text="1 oy — 20 000 so'm",
                    callback_data="plan_30"
                )
            ],

            [
                InlineKeyboardButton(
                    text="3 oy — 50 000 so'm",
                    callback_data="plan_90"
                )
            ],

            [
                InlineKeyboardButton(
                    text="Umrbod — 150 000 so'm",
                    callback_data="plan_36500"
                )
            ],

            [
                InlineKeyboardButton(
                    text="❌ Yopish",
                    callback_data="close_prime"
                )
            ]

        ]
    )


def payment_keyboard(order_id):

    return InlineKeyboardMarkup(
        inline_keyboard=[

            [
                InlineKeyboardButton(
                    text="💳 To'ladim",
                    callback_data=f"paid_{order_id}"
                )
            ],

            [
                InlineKeyboardButton(
                    text="⬅️ Orqaga",
                    callback_data="prime_back"
                )
            ]

        ]
    )


def admin_payment_keyboard(order_id):

    return InlineKeyboardMarkup(
        inline_keyboard=[

            [
                InlineKeyboardButton(
                    text="✅ Tasdiqlash",
                    callback_data=f"approve_{order_id}"
                ),

                InlineKeyboardButton(
                    text="❌ Bekor qilish",
                    callback_data=f"reject_{order_id}"
                )
            ]

        ]
    )


# =========================================================
# STATES
# =========================================================

class AddMovie(StatesGroup):

    code = State()
    title = State()
    video = State()
    prime = State()


class DeleteMovie(StatesGroup):

    code = State()


class CardAdd(StatesGroup):

    number = State()
    owner = State()


class ChannelAdd(StatesGroup):

    username = State()


class ChannelDelete(StatesGroup):

    username = State()


class SearchMovie(StatesGroup):

    text = State()


class MovieRequest(StatesGroup):

    text = State()


class PaymentScreenshot(StatesGroup):

    screenshot = State()


# =========================================================
# /START
# =========================================================

@dp.message(Command("start"))
async def start_handler(
    message: Message,
    state: FSMContext
):

    await state.clear()

    save_user(message)
    save_admin(message)

    # Admin uchun majburiy obuna yo'q
    if is_admin(message):

        await message.answer(
            "🎬 <b>KinoCinema</b> botiga xush kelibsiz!",
            reply_markup=main_menu(
                message.from_user.id
            ),
            parse_mode="HTML"
        )

        return

    # Oddiy foydalanuvchi uchun obuna
    subscribed = await check_subscription(
        message.from_user.id
    )

    if not subscribed:

        await show_subscription(
            message
        )

        return

    await message.answer(
        "🎬 <b>KinoCinema</b> botiga xush kelibsiz!\n\n"
        "Kerakli bo'limni tanlang:",
        reply_markup=main_menu(
            message.from_user.id
        ),
        parse_mode="HTML"
    )


# =========================================================
# OBUNA TEKSHIRISH
# =========================================================

@dp.callback_query(
    F.data == "check_subscription"
)
async def check_subscription_callback(
    callback: CallbackQuery
):

    if is_admin_user(
        callback.from_user.id
    ):

        await callback.message.answer(
            "🏠 Asosiy menyu:",
            reply_markup=main_menu(
                callback.from_user.id
            )
        )

        await callback.answer()
        return

    subscribed = await check_subscription(
        callback.from_user.id
    )

    if subscribed:

        await callback.message.answer(
            "✅ <b>Obuna tasdiqlandi!</b>\n\n"
            "Endi botdan foydalanishingiz mumkin.",
            reply_markup=main_menu(
                callback.from_user.id
            ),
            parse_mode="HTML"
        )

        await callback.answer(
            "Obuna tasdiqlandi!"
        )

    else:

        await callback.answer(
            "❌ Hali barcha kanallarga obuna bo'lmagansiz.",
            show_alert=True
        )


# =========================================================
# ADMIN PANEL
# =========================================================

@dp.message(
    F.text == "👨‍💻 Admin panel"
)
async def admin_panel(
    message: Message
):

    if not is_admin(message):
        return

    save_admin(message)

    await message.answer(
        "👨‍💻 <b>Admin panel</b>\n\n"
        "Kerakli bo'limni tanlang:",
        reply_markup=admin_menu(),
        parse_mode="HTML"
    )


# =========================================================
# ADMIN KANAL QO'SHISH
# =========================================================

@dp.message(
    F.text == "📢 Kanal qo'shish"
)
async def channel_add_start(
    message: Message,
    state: FSMContext
):

    if not is_admin(message):
        return

    await state.set_state(
        ChannelAdd.username
    )

    await message.answer(
        "📢 <b>Kanal qo'shish</b>\n\n"
        "Kanal username'ini yuboring.\n\n"
        "Masalan:\n"
        "<code>@uz_kinocinema</code>\n\n"
        "Yoki kanal ID'si:\n"
        "<code>-1001234567890</code>\n\n"
        "⚠️ Bot kanalga administrator qilib "
        "qo'yilgan bo'lishi kerak.",
        reply_markup=admin_cancel_keyboard(),
        parse_mode="HTML"
    )


# =========================================================
# KANAL QO'SHISH
# =========================================================

@dp.message(
    StateFilter(ChannelAdd.username)
)
async def channel_add_received(
    message: Message,
    state: FSMContext
):

    if not is_admin(message):
        return

    value = message.text.strip()

    # Username
    if value.startswith("@"):

        lookup = value

        saved_username = value

    # Username @siz
    elif not value.startswith("-100"):

        saved_username = "@" + value
        lookup = saved_username

    # Telegram ID
    else:

        try:
            lookup = int(value)
            saved_username = value
        except:

            await message.answer(
                "❌ Kanal ID noto'g'ri."
            )

            return

    try:

        chat = await bot.get_chat(
            lookup
        )

    except Exception as e:

        print(
            "KANAL TOPISH XATOSI:",
            e
        )

        await message.answer(
            "❌ <b>Kanal topilmadi.</b>\n\n"
            "Username yoki ID'ni tekshiring.\n\n"
            "Masalan:\n"
            "<code>@uz_kinocinema</code>\n"
            "yoki\n"
            "<code>-1001234567890</code>",
            reply_markup=admin_cancel_keyboard(),
            parse_mode="HTML"
        )

        return

    if chat.type not in (
        "channel",
        "supergroup"
    ):

        await message.answer(
            "❌ Bu kanal emas.",
            reply_markup=admin_cancel_keyboard()
        )

        return

    # Bot o'z ID sini oladi
    me = await bot.get_me()

    try:

        bot_member = await bot.get_chat_member(
            chat.id,
            me.id
        )

        if bot_member.status not in (
            "administrator",
            "creator"
        ):

            await message.answer(
                "❌ <b>Bot kanalga admin qilinmagan.</b>\n\n"
                "Avval botni kanalga administrator "
                "qilib qo'ying.",
                reply_markup=admin_cancel_keyboard(),
                parse_mode="HTML"
            )

            return

    except Exception as e:

        print(
            "BOT ADMIN TEKSHIRISH XATOSI:",
            e
        )

        await message.answer(
            "❌ Botning kanalga huquqini tekshirib bo'lmadi.\n\n"
            "Botni kanalga administrator qilib qo'ying.",
            reply_markup=admin_cancel_keyboard()
        )

        return

    # Saqlash
    try:

        db.execute("""
            INSERT INTO channels(
                username,
                title,
                chat_id
            )
            VALUES (?, ?, ?)
        """, (
            saved_username,
            chat.title or saved_username,
            str(chat.id)
        ))

        db.commit()

    except sqlite3.IntegrityError:

        await message.answer(
            "❌ Bu kanal allaqachon qo'shilgan.",
            reply_markup=admin_menu()
        )

        await state.clear()
        return

    await state.clear()

    await message.answer(
        "✅ <b>Kanal muvaffaqiyatli qo'shildi!</b>\n\n"
        f"📢 Nomi: <b>{chat.title}</b>\n"
        f"🔗 {saved_username}\n"
        f"🆔 ID: <code>{chat.id}</code>\n\n"
        "Endi yangi foydalanuvchilardan "
        "shu kanalga obuna bo'lish talab qilinadi.",
        reply_markup=admin_menu(),
        parse_mode="HTML"
    )


# =========================================================
# KANAL O'CHIRISH
# =========================================================

@dp.message(
    F.text == "🗑 Kanal o'chirish"
)
async def channel_delete_start(
    message: Message,
    state: FSMContext
):

    if not is_admin(message):
        return

    channels = get_channels()

    if not channels:

        await message.answer(
            "📢 Hozircha majburiy kanal yo'q.",
            reply_markup=admin_menu()
        )

        return

    text = (
        "🗑 <b>Kanal o'chirish</b>\n\n"
        "Mavjud kanallar:\n\n"
    )

    for channel in channels:

        text += (
            f"📢 <b>{channel['title']}</b>\n"
            f"🔗 {channel['username']}\n"
            f"🆔 {channel['chat_id']}\n\n"
        )

    text += (
        "O'chirmoqchi bo'lgan kanal "
        "username yoki ID'sini yuboring."
    )

    await state.set_state(
        ChannelDelete.username
    )

    await message.answer(
        text,
        reply_markup=admin_cancel_keyboard(),
        parse_mode="HTML"
    )


@dp.message(
    StateFilter(ChannelDelete.username)
)
async def channel_delete_received(
    message: Message,
    state: FSMContext
):

    if not is_admin(message):
        return

    value = message.text.strip()

    cur = db.cursor()

    cur.execute("""
        SELECT *
        FROM channels
        WHERE username = ?
        OR chat_id = ?
    """, (
        value,
        value
    ))

    channel = cur.fetchone()

    if not channel:

        if not value.startswith("@"):

            value2 = "@" + value

            cur.execute("""
                SELECT *
                FROM channels
                WHERE username = ?
            """, (
                value2,
            ))

            channel = cur.fetchone()

    if not channel:

        await message.answer(
            "❌ Bunday kanal topilmadi.\n\n"
            "Username yoki ID'ni qayta yuboring.",
            reply_markup=admin_cancel_keyboard()
        )

        return

    db.execute(
        "DELETE FROM channels WHERE id = ?",
        (channel["id"],)
    )

    db.commit()

    await state.clear()

    await message.answer(
        "🗑 <b>Kanal o'chirildi.</b>\n\n"
        f"📢 {channel['title']}",
        reply_markup=admin_menu(),
        parse_mode="HTML"
    )


# =========================================================
# KANALLAR RO'YXATI
# =========================================================

@dp.message(
    F.text == "📋 Kanallar"
)
async def channel_list(
    message: Message
):

    if not is_admin(message):
        return

    channels = get_channels()

    if not channels:

        await message.answer(
            "📢 <b>Majburiy kanallar</b>\n\n"
            "❌ Hozircha kanal qo'shilmagan.",
            parse_mode="HTML"
        )

        return

    text = "📢 <b>Majburiy kanallar</b>\n\n"

    for i, channel in enumerate(
        channels,
        1
    ):

        text += (
            f"{i}. <b>{channel['title']}</b>\n"
            f"🔗 {channel['username']}\n"
            f"🆔 <code>{channel['chat_id']}</code>\n\n"
        )

    await message.answer(
        text,
        parse_mode="HTML"
    )


# =========================================================
# KARTA SOZLAMALARI
# =========================================================

def card_menu():

    return InlineKeyboardMarkup(
        inline_keyboard=[

            [
                InlineKeyboardButton(
                    text="➕ Karta qo'shish",
                    callback_data="card_add"
                )
            ],

            [
                InlineKeyboardButton(
                    text="🔄 Kartani almashtirish",
                    callback_data="card_add"
                )
            ],

            [
                InlineKeyboardButton(
                    text="👀 Hozirgi kartani ko'rish",
                    callback_data="card_view"
                )
            ],

            [
                InlineKeyboardButton(
                    text="🗑 Kartani o'chirish",
                    callback_data="card_delete"
                )
            ],

            [
                InlineKeyboardButton(
                    text="⬅️ Admin panel",
                    callback_data="admin_back"
                )
            ]

        ]
    )


@dp.message(
    F.text == "💳 Karta sozlamalari"
)
async def card_settings(
    message: Message
):

    if not is_admin(message):
        return

    card = get_setting(
        "payment_card"
    )

    owner = get_setting(
        "payment_owner"
    )

    if card:

        text = (
            "💳 <b>Hozirgi karta</b>\n\n"
            f"💳 <code>{card}</code>\n"
            f"👤 <b>{owner}</b>\n\n"
            "Kerakli amalni tanlang:"
        )

    else:

        text = (
            "💳 <b>Karta sozlamalari</b>\n\n"
            "❌ Hozircha karta qo'shilmagan."
        )

    await message.answer(
        text,
        reply_markup=card_menu(),
        parse_mode="HTML"
    )


# =========================================================
# KARTA QO'SHISH
# =========================================================

@dp.callback_query(
    F.data == "card_add"
)
async def card_add_start(
    callback: CallbackQuery,
    state: FSMContext
):

    if not is_admin_user(
        callback.from_user.id
    ):

        await callback.answer(
            "Ruxsat yo'q!",
            show_alert=True
        )

        return

    await state.set_state(
        CardAdd.number
    )

    await callback.message.answer(
        "💳 <b>Karta raqamini yuboring.</b>\n\n"
        "Masalan:\n"
        "<code>9860600435412504</code>",
        reply_markup=admin_cancel_keyboard(),
        parse_mode="HTML"
    )

    await callback.answer()


@dp.message(
    StateFilter(CardAdd.number)
)
async def card_number_received(
    message: Message,
    state: FSMContext
):

    if not is_admin(message):
        return

    number = (
        message.text
        .strip()
        .replace(" ", "")
        .replace("-", "")
    )

    if not number.isdigit():

        await message.answer(
            "❌ Karta raqami faqat raqamlardan "
            "iborat bo'lishi kerak.",
            reply_markup=admin_cancel_keyboard()
        )

        return

    if len(number) < 12 or len(number) > 19:

        await message.answer(
            "❌ Karta raqami noto'g'ri.",
            reply_markup=admin_cancel_keyboard()
        )

        return

    await state.update_data(
        card_number=number
    )

    await state.set_state(
        CardAdd.owner
    )

    await message.answer(
        "👤 <b>Karta egasining ism-familiyasini yuboring.</b>",
        reply_markup=admin_cancel_keyboard(),
        parse_mode="HTML"
    )


@dp.message(
    StateFilter(CardAdd.owner)
)
async def card_owner_received(
    message: Message,
    state: FSMContext
):

    if not is_admin(message):
        return

    owner = message.text.strip()

    data = await state.get_data()

    set_setting(
        "payment_card",
        data["card_number"]
    )

    set_setting(
        "payment_owner",
        owner
    )

    await state.clear()

    await message.answer(
        "✅ <b>Karta saqlandi!</b>\n\n"
        f"💳 <code>{data['card_number']}</code>\n"
        f"👤 {owner}",
        reply_markup=admin_menu(),
        parse_mode="HTML"
    )


@dp.callback_query(
    F.data == "card_view"
)
async def card_view(
    callback: CallbackQuery
):

    if not is_admin_user(
        callback.from_user.id
    ):
        return

    card = get_setting(
        "payment_card"
    )

    owner = get_setting(
        "payment_owner"
    )

    if not card:

        text = "❌ Karta qo'shilmagan."

    else:

        text = (
            "💳 <b>Hozirgi karta</b>\n\n"
            f"💳 <code>{card}</code>\n"
            f"👤 <b>{owner}</b>"
        )

    await callback.message.answer(
        text,
        reply_markup=card_menu(),
        parse_mode="HTML"
    )

    await callback.answer()


@dp.callback_query(
    F.data == "card_delete"
)
async def card_delete(
    callback: CallbackQuery
):

    if not is_admin_user(
        callback.from_user.id
    ):
        return

    delete_setting(
        "payment_card"
    )

    delete_setting(
        "payment_owner"
    )

    await callback.message.answer(
        "🗑 <b>Karta o'chirildi.</b>",
        reply_markup=card_menu(),
        parse_mode="HTML"
    )

    await callback.answer()


# =========================================================
# ADMIN BACK
# =========================================================

@dp.callback_query(
    F.data == "admin_back"
)
async def admin_back(
    callback: CallbackQuery
):

    if not is_admin_user(
        callback.from_user.id
    ):
        return

    await callback.message.answer(
        "👨‍💻 <b>Admin panel</b>",
        reply_markup=admin_menu(),
        parse_mode="HTML"
    )

    await callback.answer()


# =========================================================
# PRIME STATUS
# =========================================================

@dp.message(
    F.text == "⭐ Prime status"
)
async def prime_status(
    message: Message
):

    if not is_admin(message):

        if not await check_subscription(
            message.from_user.id
        ):

            await show_subscription(
                message
            )

            return

    save_user(message)

    if is_prime(
        message.from_user.id
    ):

        cur = db.cursor()

        cur.execute(
            "SELECT prime_until FROM users WHERE user_id = ?",
            (message.from_user.id,)
        )

        row = cur.fetchone()

        until = datetime.fromisoformat(
            row["prime_until"]
        )

        await message.answer(
            "⭐ <b>Prime/VIP faol!</b>\n\n"
            f"⏳ Tugash vaqti:\n"
            f"<b>{until.strftime('%d.%m.%Y %H:%M')}</b>",
            parse_mode="HTML"
        )

    else:

        await message.answer(
            "⭐ <b>Prime/VIP</b>\n\n"
            "Sizda Prime mavjud emas.\n\n"
            "Tarifni tanlang:",
            reply_markup=prime_plans_keyboard(),
            parse_mode="HTML"
        )


# =========================================================
# PRIME PLAN
# =========================================================

@dp.callback_query(
    F.data.startswith("plan_")
)
async def choose_plan(
    callback: CallbackQuery
):

    plan_id = callback.data.replace(
        "plan_",
        ""
    )

    if plan_id not in PLANS:

        await callback.answer(
            "Xatolik!",
            show_alert=True
        )

        return

    plan = PLANS[plan_id]

    card = get_setting(
        "payment_card"
    )

    owner = get_setting(
        "payment_owner"
    )

    if not card:

        await callback.message.answer(
            "❌ Hozircha to'lov kartasi sozlanmagan."
        )

        await callback.answer()

        return

    cur = db.cursor()

    cur.execute("""
        INSERT INTO orders(
            user_id,
            username,
            plan,
            days,
            price,
            status,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, 'pending', ?)
    """, (
        callback.from_user.id,
        callback.from_user.username or "",
        plan["name"],
        plan["days"],
        plan["price"],
        datetime.now().isoformat()
    ))

    order_id = cur.lastrowid

    db.commit()

    await callback.message.answer(
        f"⭐ <b>Prime — {plan['name']}</b>\n\n"
        f"💰 Narxi: <b>{plan['price']:,} so'm</b>\n\n"
        f"💳 Karta:\n"
        f"<code>{card}</code>\n\n"
        f"👤 Karta egasi:\n"
        f"<b>{owner}</b>\n\n"
        "To'lovni amalga oshirgach "
        "«💳 To'ladim» tugmasini bosing.",
        reply_markup=payment_keyboard(
            order_id
        ),
        parse_mode="HTML"
    )

    await callback.answer()


# =========================================================
# TO'LADIM
# =========================================================

@dp.callback_query(
    F.data.startswith("paid_")
)
async def paid_handler(
    callback: CallbackQuery,
    state: FSMContext
):

    order_id = int(
        callback.data.replace(
            "paid_",
            ""
        )
    )

    cur = db.cursor()

    cur.execute(
        "SELECT * FROM orders WHERE id = ?",
        (order_id,)
    )

    order = cur.fetchone()

    if not order:

        await callback.answer(
            "Buyurtma topilmadi.",
            show_alert=True
        )

        return

    if order["status"] != "pending":

        await callback.answer(
            "Bu buyurtma allaqachon ko'rib chiqilgan.",
            show_alert=True
        )

        return

    await state.update_data(
        order_id=order_id
    )

    await state.set_state(
        PaymentScreenshot.screenshot
    )

    await callback.message.answer(
        "📸 <b>To'lov screenshotini yuboring.</b>\n\n"
        "Rasm sifatida yuboring.",
        reply_markup=cancel_keyboard(),
        parse_mode="HTML"
    )

    await callback.answer()


# =========================================================
# PAYMENT SCREENSHOT
# =========================================================

@dp.message(
    StateFilter(
        PaymentScreenshot.screenshot
    ),
    F.photo
)
async def payment_screenshot(
    message: Message,
    state: FSMContext
):

    data = await state.get_data()

    order_id = data.get(
        "order_id"
    )

    if not order_id:

        await state.clear()

        await message.answer(
            "❌ Buyurtma topilmadi.",
            reply_markup=main_menu(
                message.from_user.id
            )
        )

        return

    cur = db.cursor()

    cur.execute(
        "SELECT * FROM orders WHERE id = ?",
        (order_id,)
    )

    order = cur.fetchone()

    if not order:

        await state.clear()

        await message.answer(
            "❌ Buyurtma topilmadi."
        )

        return

    admin_id = get_setting(
        "admin_id"
    )

    if not admin_id:

        await state.clear()

        await message.answer(
            "❌ Admin hali botni /start qilmagan."
        )

        return

    photo = message.photo[-1]

    username = (
        f"@{order['username']}"
        if order["username"]
        else "Username yo'q"
    )

    caption = (
        "🧾 <b>Yangi PRIME to'lovi!</b>\n\n"
        f"👤 User: {username}\n"
        f"🆔 ID: <code>{order['user_id']}</code>\n"
        f"📦 Tarif: <b>{order['plan']}</b>\n"
        f"⏳ Muddat: <b>{order['days']} kun</b>\n"
        f"💰 Narx: <b>{order['price']:,} so'm</b>\n\n"
        "📸 To'lov skrinshoti:"
    )

    try:

        await bot.send_photo(
            chat_id=int(admin_id),
            photo=photo.file_id,
            caption=caption,
            parse_mode="HTML",
            reply_markup=admin_payment_keyboard(
                order_id
            )
        )

    except Exception as e:

        print(
            "ADMIN PAYMENT XATOSI:",
            e
        )

        await message.answer(
            "❌ Screenshotni adminga yuborib bo'lmadi."
        )

        await state.clear()

        return

    await state.clear()

    await message.answer(
        "⏳ <b>To'lovingiz tekshirilmoqda.</b>\n\n"
        "Iltimos, qayta screenshot yubormang.",
        reply_markup=main_menu(
            message.from_user.id
        ),
        parse_mode="HTML"
    )


@dp.message(
    StateFilter(
        PaymentScreenshot.screenshot
    )
)
async def payment_wrong_file(
    message: Message
):

    await message.answer(
        "📸 Iltimos, screenshotni rasm sifatida yuboring.",
        reply_markup=cancel_keyboard()
    )


# =========================================================
# APPROVE
# =========================================================

@dp.callback_query(
    F.data.startswith("approve_")
)
async def approve_payment(
    callback: CallbackQuery
):

    if not is_admin_user(
        callback.from_user.id
    ):

        await callback.answer(
            "Ruxsat yo'q!",
            show_alert=True
        )

        return

    order_id = int(
        callback.data.replace(
            "approve_",
            ""
        )
    )

    cur = db.cursor()

    cur.execute(
        "SELECT * FROM orders WHERE id = ?",
        (order_id,)
    )

    order = cur.fetchone()

    if not order:

        await callback.answer(
            "Buyurtma topilmadi.",
            show_alert=True
        )

        return

    if order["status"] != "pending":

        await callback.answer(
            "Bu to'lov allaqachon ko'rib chiqilgan.",
            show_alert=True
        )

        return

    until = activate_prime(
        order["user_id"],
        order["days"]
    )

    db.execute("""
        UPDATE orders
        SET status = 'approved'
        WHERE id = ?
    """, (
        order_id,
    ))

    db.commit()

    try:

        await bot.send_message(
            order["user_id"],
            "🎉 <b>To'lov tasdiqlandi!</b>\n\n"
            f"⭐ Prime/VIP: <b>{order['plan']}</b>\n"
            f"⏳ Muddat: <b>{order['days']} kun</b>\n\n"
            f"📅 Tugash vaqti:\n"
            f"<b>{until.strftime('%d.%m.%Y %H:%M')}</b>\n\n"
            "✅ Prime/VIP ochildi!",
            parse_mode="HTML"
        )

    except Exception as e:

        print(
            "USER APPROVE XATOSI:",
            e
        )

    try:

        old_caption = (
            callback.message.caption
            or ""
        )

        await callback.message.edit_caption(
            caption=(
                old_caption
                + "\n\n"
                "✅ <b>TASDIQLANDI</b>"
            ),
            parse_mode="HTML"
        )

    except:
        pass

    await callback.answer(
        "✅ Prime/VIP ochildi!"
    )


# =========================================================
# REJECT
# =========================================================

@dp.callback_query(
    F.data.startswith("reject_")
)
async def reject_payment(
    callback: CallbackQuery
):

    if not is_admin_user(
        callback.from_user.id
    ):

        await callback.answer(
            "Ruxsat yo'q!",
            show_alert=True
        )

        return

    order_id = int(
        callback.data.replace(
            "reject_",
            ""
        )
    )

    cur = db.cursor()

    cur.execute(
        "SELECT * FROM orders WHERE id = ?",
        (order_id,)
    )

    order = cur.fetchone()

    if not order:

        await callback.answer(
            "Buyurtma topilmadi.",
            show_alert=True
        )

        return

    if order["status"] != "pending":

        await callback.answer(
            "Bu to'lov allaqachon ko'rib chiqilgan.",
            show_alert=True
        )

        return

    db.execute("""
        UPDATE orders
        SET status = 'rejected'
        WHERE id = ?
    """, (
        order_id,
    ))

    db.commit()

    try:

        await bot.send_message(
            order["user_id"],
            "❌ <b>To'lov tasdiqlanmadi.</b>\n\n"
            "Prime/VIP ochilmadi.",
            parse_mode="HTML"
        )

    except:
        pass

    try:

        old_caption = (
            callback.message.caption
            or ""
        )

        await callback.message.edit_caption(
            caption=(
                old_caption
                + "\n\n"
                "❌ <b>BEKOR QILINDI</b>"
            ),
            parse_mode="HTML"
        )

    except:
        pass

    await callback.answer(
        "❌ To'lov bekor qilindi."
    )


# =========================================================
# KINO QIDIRISH
# =========================================================

@dp.message(
    F.text == "🔎 Kino qidirish"
)
async def search_start(
    message: Message,
    state: FSMContext
):

    if not is_admin(message):

        if not await check_subscription(
            message.from_user.id
        ):

            await show_subscription(
                message
            )

            return

    await state.set_state(
        SearchMovie.text
    )

    await message.answer(
        "🔎 <b>Kino qidirish</b>\n\n"
        "Kino kodini yoki nomini yozing.\n\n"
        "Masalan: <code>247</code>",
        reply_markup=cancel_keyboard(),
        parse_mode="HTML"
    )


# =========================================================
# KINO TOPISH VA YUBORISH
# =========================================================

async def find_and_send_movies(
    message: Message,
    query: str
):

    query = query.strip()

    cur = db.cursor()

    # Avval kod
    cur.execute("""
        SELECT *
        FROM movies
        WHERE code = ?
    """, (
        query,
    ))

    movies = cur.fetchall()

    # Keyin nom
    if not movies:

        cur.execute("""
            SELECT *
            FROM movies
            WHERE title LIKE ?
            ORDER BY id DESC
        """, (
            f"%{query}%",
        ))

        movies = cur.fetchall()

    if not movies:

        await message.answer(
            "❌ <b>Kino topilmadi.</b>",
            parse_mode="HTML"
        )

        return

    for movie in movies[:10]:

        if movie["prime"]:

            if not is_prime(
                message.from_user.id
            ):

                await message.answer(
                    f"🔒 <b>{movie['title']}</b>\n\n"
                    "Bu kino faqat Prime/VIP uchun.",
                    reply_markup=prime_plans_keyboard(),
                    parse_mode="HTML"
                )

                continue

        db.execute("""
            UPDATE movies
            SET views = views + 1
            WHERE id = ?
        """, (
            movie["id"],
        ))

        db.commit()

        await bot.send_video(
            message.chat.id,
            movie["file_id"],
            caption=(
                f"🎬 <b>{movie['title']}</b>\n\n"
                f"🔢 Kod: <code>{movie['code']}</code>\n\n"
                "🍿 Yoqimli tomosha!"
            ),
            parse_mode="HTML"
        )


@dp.message(
    StateFilter(SearchMovie.text)
)
async def search_movie(
    message: Message,
    state: FSMContext
):

    query = message.text.strip()

    await state.clear()

    await find_and_send_movies(
        message,
        query
    )

    await message.answer(
        "🏠 Asosiy menyu:",
        reply_markup=main_menu(
            message.from_user.id
        )
    )


# =========================================================
# KINO RO'YXATI
# =========================================================

@dp.message(
    F.text == "📚 Kinolar ro'yxati"
)
async def movie_list(
    message: Message
):

    if not is_admin(message):

        if not await check_subscription(
            message.from_user.id
        ):

            await show_subscription(
                message
            )

            return

    cur = db.cursor()

    cur.execute("""
        SELECT *
        FROM movies
        ORDER BY id DESC
    """)

    movies = cur.fetchall()

    if not movies:

        await message.answer(
            "📚 Hozircha kinolar yo'q."
        )

        return

    buttons = []

    for movie in movies[:50]:

        title = movie["title"]

        if movie["prime"]:
            title = "⭐ " + title

        buttons.append([
            InlineKeyboardButton(
                text=title[:50],
                callback_data=f"movie_{movie['id']}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            text="❌ Yopish",
            callback_data="close_movies"
        )
    ])

    await message.answer(
        "📚 <b>Kinolar ro'yxati</b>\n\n"
        "Kerakli kinoni tanlang:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        ),
        parse_mode="HTML"
    )


@dp.callback_query(
    F.data == "close_movies"
)
async def close_movies(
    callback: CallbackQuery
):

    try:
        await callback.message.delete()
    except:
        pass

    await callback.answer()


# =========================================================
# KINO CLICK
# =========================================================

@dp.callback_query(
    F.data.startswith("movie_")
)
async def movie_click(
    callback: CallbackQuery
):

    if not is_admin_user(
        callback.from_user.id
    ):

        if not await check_subscription(
            callback.from_user.id
        ):

            await callback.answer(
                "Avval kanalga obuna bo'ling.",
                show_alert=True
            )

            return

    movie_id = int(
        callback.data.replace(
            "movie_",
            ""
        )
    )

    cur = db.cursor()

    cur.execute(
        "SELECT * FROM movies WHERE id = ?",
        (movie_id,)
    )

    movie = cur.fetchone()

    if not movie:

        await callback.answer(
            "Kino topilmadi.",
            show_alert=True
        )

        return

    if movie["prime"]:

        if not is_prime(
            callback.from_user.id
        ):

            await callback.message.answer(
                "🔒 Bu kino faqat Prime/VIP uchun.",
                reply_markup=prime_plans_keyboard()
            )

            await callback.answer()

            return

    db.execute("""
        UPDATE movies
        SET views = views + 1
        WHERE id = ?
    """, (
        movie_id,
    ))

    db.commit()

    await bot.send_video(
        callback.from_user.id,
        movie["file_id"],
        caption=(
            f"🎬 <b>{movie['title']}</b>\n\n"
            f"🔢 Kod: <code>{movie['code']}</code>\n\n"
            "🍿 Yoqimli tomosha!"
        ),
        parse_mode="HTML"
    )

    await callback.answer()


# =========================================================
# INSTAGRAM
# =========================================================

@dp.message(
    F.text == "📸 Instagramga qaytish"
)
async def instagram(
    message: Message
):

    await message.answer(
        "📸 Instagram:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📸 Instagram",
                        url=INSTAGRAM_URL
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ Yopish",
                        callback_data="close_instagram"
                    )
                ]
            ]
        )
    )


@dp.callback_query(
    F.data == "close_instagram"
)
async def close_instagram(
    callback: CallbackQuery
):

    try:
        await callback.message.delete()
    except:
        pass

    await callback.answer()


# =========================================================
# KINO BUYURTMA
# =========================================================

@dp.message(
    F.text == "🎬 Kino buyurtma qilish"
)
async def movie_request_start(
    message: Message,
    state: FSMContext
):

    if not is_admin(message):

        if not await check_subscription(
            message.from_user.id
        ):

            await show_subscription(
                message
            )

            return

    await state.set_state(
        MovieRequest.text
    )

    await message.answer(
        "🎬 <b>Kino buyurtma qilish</b>\n\n"
        "Qaysi kinoni izlayotganingizni yozing:",
        reply_markup=cancel_keyboard(),
        parse_mode="HTML"
    )


@dp.message(
    StateFilter(MovieRequest.text)
)
async def movie_request_received(
    message: Message,
    state: FSMContext
):

    request_text = message.text.strip()

    db.execute("""
        INSERT INTO requests(
            user_id,
            username,
            text,
            created_at
        )
        VALUES (?, ?, ?, ?)
    """, (
        message.from_user.id,
        message.from_user.username or "",
        request_text,
        datetime.now().isoformat()
    ))

    db.commit()

    admin_id = get_setting(
        "admin_id"
    )

    if admin_id:

        try:

            await bot.send_message(
                int(admin_id),
                "🎬 <b>Yangi kino buyurtmasi!</b>\n\n"
                f"👤 ID: <code>{message.from_user.id}</code>\n"
                f"👤 Username: @{message.from_user.username or 'yo‘q'}\n\n"
                f"📝 So'rov:\n{request_text}",
                parse_mode="HTML"
            )

        except:
            pass

    await state.clear()

    await message.answer(
        "✅ <b>Buyurtmangiz adminga yuborildi.</b>",
        reply_markup=main_menu(
            message.from_user.id
        ),
        parse_mode="HTML"
    )


# =========================================================
# REKLAMA
# =========================================================

@dp.message(
    F.text == "🤝 Reklama & Bot olish"
)
async def advertisement(
    message: Message
):

    await message.answer(
        "🤝 <b>Reklama & Bot olish</b>\n\n"
        "Admin bilan bog'laning.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="👨‍💻 Admin",
                        url=f"https://t.me/{ADMIN_USERNAME}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ Yopish",
                        callback_data="close_ad"
                    )
                ]
            ]
        ),
        parse_mode="HTML"
    )


@dp.callback_query(
    F.data == "close_ad"
)
async def close_ad(
    callback: CallbackQuery
):

    try:
        await callback.message.delete()
    except:
        pass

    await callback.answer()


# =========================================================
# KINO QO'SHISH
# =========================================================

@dp.message(
    F.text == "➕ Kino qo'shish"
)
async def add_movie_start(
    message: Message,
    state: FSMContext
):

    if not is_admin(message):
        return

    await state.set_state(
        AddMovie.code
    )

    await message.answer(
        "➕ <b>Kino qo'shish</b>\n\n"
        "1️⃣ Kino kodini yuboring.\n\n"
        "Masalan: <code>247</code>",
        reply_markup=admin_cancel_keyboard(),
        parse_mode="HTML"
    )


@dp.message(
    StateFilter(AddMovie.code)
)
async def add_movie_code(
    message: Message,
    state: FSMContext
):

    if not is_admin(message):
        return

    code = message.text.strip()

    if not code:

        await message.answer(
            "❌ Kod bo'sh bo'lishi mumkin emas."
        )

        return

    cur = db.cursor()

    cur.execute(
        "SELECT id FROM movies WHERE code = ?",
        (code,)
    )

    if cur.fetchone():

        await message.answer(
            "❌ Bu kod allaqachon mavjud.\n"
            "Boshqa kod yuboring.",
            reply_markup=admin_cancel_keyboard()
        )

        return

    await state.update_data(
        code=code
    )

    await state.set_state(
        AddMovie.title
    )

    await message.answer(
        "2️⃣ 🎬 <b>Kino nomini yuboring.</b>",
        reply_markup=admin_cancel_keyboard(),
        parse_mode="HTML"
    )


@dp.message(
    StateFilter(AddMovie.title)
)
async def add_movie_title(
    message: Message,
    state: FSMContext
):

    if not is_admin(message):
        return

    title = message.text.strip()

    if not title:

        await message.answer(
            "❌ Kino nomi bo'sh bo'lishi mumkin emas."
        )

        return

    await state.update_data(
        title=title
    )

    await state.set_state(
        AddMovie.video
    )

    await message.answer(
        "3️⃣ 🎥 <b>Kino videosini yuboring.</b>",
        reply_markup=admin_cancel_keyboard(),
        parse_mode="HTML"
    )


@dp.message(
    StateFilter(AddMovie.video),
    F.video
)
async def add_movie_video(
    message: Message,
    state: FSMContext
):

    if not is_admin(message):
        return

    await state.update_data(
        file_id=message.video.file_id
    )

    await state.set_state(
        AddMovie.prime
    )

    await message.answer(
        "4️⃣ Kino turini tanlang:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[

                [
                    InlineKeyboardButton(
                        text="🆓 Oddiy kino",
                        callback_data="add_normal"
                    )
                ],

                [
                    InlineKeyboardButton(
                        text="⭐ Prime/VIP kino",
                        callback_data="add_prime"
                    )
                ],

                [
                    InlineKeyboardButton(
                        text="❌ Bekor qilish",
                        callback_data="cancel_add_movie"
                    )
                ]

            ]
        )
    )


@dp.message(
    StateFilter(AddMovie.video)
)
async def add_movie_video_wrong(
    message: Message
):

    await message.answer(
        "❌ Videoni video sifatida yuboring.",
        reply_markup=admin_cancel_keyboard()
    )


@dp.callback_query(
    F.data == "cancel_add_movie"
)
async def cancel_add_movie(
    callback: CallbackQuery,
    state: FSMContext
):

    await state.clear()

    await callback.message.answer(
        "❌ Kino qo'shish bekor qilindi.",
        reply_markup=admin_menu()
    )

    await callback.answer()


# =========================================================
# KINO DATABASEGA SAQLASH
# =========================================================

@dp.callback_query(
    F.data.in_({
        "add_normal",
        "add_prime"
    })
)
async def finish_add_movie(
    callback: CallbackQuery,
    state: FSMContext
):

    if not is_admin_user(
        callback.from_user.id
    ):
        return

    data = await state.get_data()

    if not data:

        await callback.answer(
            "Jarayon topilmadi.",
            show_alert=True
        )

        return

    prime = (
        1
        if callback.data == "add_prime"
        else 0
    )

    try:

        db.execute("""
            INSERT INTO movies(
                code,
                title,
                file_id,
                prime
            )
            VALUES (?, ?, ?, ?)
        """, (
            data["code"],
            data["title"],
            data["file_id"],
            prime
        ))

        db.commit()

    except sqlite3.IntegrityError:

        await callback.message.answer(
            "❌ Bu kino kodi allaqachon mavjud.",
            reply_markup=admin_menu()
        )

        await state.clear()

        await callback.answer()

        return

    await state.clear()

    await callback.message.answer(
        "✅ <b>Kino muvaffaqiyatli qo'shildi!</b>\n\n"
        f"🎬 {data['title']}\n"
        f"🔢 Kod: <code>{data['code']}</code>\n"
        f"⭐ Prime/VIP: "
        f"{'Ha' if prime else 'Yo‘q'}\n\n"
        "Endi foydalanuvchi shu kodni yozsa, "
        "kino chiqadi.",
        reply_markup=admin_menu(),
        parse_mode="HTML"
    )

    await callback.answer()


# =========================================================
# KINO O'CHIRISH
# =========================================================

@dp.message(
    F.text == "🗑 Kino o'chirish"
)
async def delete_movie_start(
    message: Message,
    state: FSMContext
):

    if not is_admin(message):
        return

    await state.set_state(
        DeleteMovie.code
    )

    await message.answer(
        "🗑 <b>Kino o'chirish</b>\n\n"
        "Kino kodini yuboring:",
        reply_markup=admin_cancel_keyboard(),
        parse_mode="HTML"
    )


@dp.message(
    StateFilter(DeleteMovie.code)
)
async def delete_movie(
    message: Message,
    state: FSMContext
):

    if not is_admin(message):
        return

    code = message.text.strip()

    cur = db.cursor()

    cur.execute(
        "SELECT * FROM movies WHERE code = ?",
        (code,)
    )

    movie = cur.fetchone()

    if not movie:

        await message.answer(
            "❌ Bunday kino topilmadi.",
            reply_markup=admin_cancel_keyboard()
        )

        return

    db.execute(
        "DELETE FROM movies WHERE code = ?",
        (code,)
    )

    db.commit()

    await state.clear()

    await message.answer(
        "🗑 <b>Kino o'chirildi.</b>\n\n"
        f"🎬 {movie['title']}\n"
        f"🔢 Kod: <code>{code}</code>",
        reply_markup=admin_menu(),
        parse_mode="HTML"
    )


# =========================================================
# STATISTIKA
# =========================================================

@dp.message(
    F.text == "📊 Statistika"
)
async def statistics(
    message: Message
):

    if not is_admin(message):
        return

    cur = db.cursor()

    cur.execute(
        "SELECT COUNT(*) AS c FROM users"
    )

    users = cur.fetchone()["c"]

    cur.execute(
        "SELECT COUNT(*) AS c FROM movies"
    )

    movies = cur.fetchone()["c"]

    cur.execute(
        "SELECT COUNT(*) AS c FROM channels"
    )

    channels = cur.fetchone()["c"]

    cur.execute(
        "SELECT SUM(views) AS v FROM movies"
    )

    views = cur.fetchone()["v"] or 0

    cur.execute("""
        SELECT COUNT(*) AS c
        FROM orders
        WHERE status = 'approved'
    """)

    payments = cur.fetchone()["c"]

    await message.answer(
        "📊 <b>Bot statistikasi</b>\n\n"
        f"👥 Foydalanuvchilar: <b>{users}</b>\n"
        f"🎬 Kinolar: <b>{movies}</b>\n"
        f"📢 Kanallar: <b>{channels}</b>\n"
        f"👁 Ko'rishlar: <b>{views}</b>\n"
        f"💳 Tasdiqlangan to'lovlar: <b>{payments}</b>",
        parse_mode="HTML"
    )


# =========================================================
# ADMIN KINOLAR
# =========================================================

@dp.message(
    F.text == "📚 Admin kinolar"
)
async def admin_movies(
    message: Message
):

    if not is_admin(message):
        return

    cur = db.cursor()

    cur.execute("""
        SELECT *
        FROM movies
        ORDER BY id DESC
    """)

    movies = cur.fetchall()

    if not movies:

        await message.answer(
            "📚 Hozircha kino yo'q."
        )

        return

    text = "📚 <b>Kinolar:</b>\n\n"

    for movie in movies:

        text += (
            f"🎬 <b>{movie['title']}</b>\n"
            f"🔢 Kod: <code>{movie['code']}</code>\n"
            f"⭐ Prime: "
            f"{'Ha' if movie['prime'] else 'Yo‘q'}\n"
            f"👁 Ko'rish: {movie['views']}\n\n"
        )

    if len(text) > 4000:
        text = text[:4000] + "\n..."

    await message.answer(
        text,
        parse_mode="HTML"
    )


# =========================================================
# ASOSIY MENYU
# =========================================================

@dp.message(
    F.text == "🏠 Asosiy menyu"
)
async def back_main(
    message: Message,
    state: FSMContext
):

    await state.clear()

    await message.answer(
        "🏠 <b>Asosiy menyu</b>",
        reply_markup=main_menu(
            message.from_user.id
        ),
        parse_mode="HTML"
    )


# =========================================================
# ORQAGA
# =========================================================

@dp.message(
    StateFilter("*"),
    F.text == "⬅️ Orqaga"
)
async def universal_back(
    message: Message,
    state: FSMContext
):

    current_state = await state.get_state()

    if current_state is None:

        if is_admin(message):

            await message.answer(
                "👨‍💻 Admin panel:",
                reply_markup=admin_menu()
            )

        else:

            await message.answer(
                "🏠 Asosiy menyu:",
                reply_markup=main_menu(
                    message.from_user.id
                )
            )

        return

    await state.clear()

    if is_admin(message):

        await message.answer(
            "👨‍💻 Admin panel:",
            reply_markup=admin_menu()
        )

    else:

        await message.answer(
            "🏠 Asosiy menyu:",
            reply_markup=main_menu(
                message.from_user.id
            )
        )


# =========================================================
# BEKOR QILISH
# =========================================================

@dp.message(
    StateFilter("*"),
    F.text == "❌ Bekor qilish"
)
async def universal_cancel(
    message: Message,
    state: FSMContext
):

    await state.clear()

    if is_admin(message):

        await message.answer(
            "❌ Jarayon bekor qilindi.\n\n"
            "👨‍💻 Admin panel:",
            reply_markup=admin_menu()
        )

    else:

        await message.answer(
            "❌ Jarayon bekor qilindi.\n\n"
            "🏠 Asosiy menyu:",
            reply_markup=main_menu(
                message.from_user.id
            )
        )


# =========================================================
# RENDER SERVER
# =========================================================

async def health(request):

    return web.Response(
        text="KinoCinema bot ishlayapti!"
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

    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    runner = web.AppRunner(app)

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port
    )

    await site.start()

    print(
        f"Web server {port} portda ishlayapti."
    )


# =========================================================
# RUN
# =========================================================

async def main():

    print(
        "🎬 KinoCinema bot ishga tushmoqda..."
    )

    await start_web_server()

    await dp.start_polling(bot)


if __name__ == "__main__":

    asyncio.run(main())
