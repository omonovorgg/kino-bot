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
        "BOT_TOKEN Render Environment Variables'da topilmadi!"
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

    # -----------------------------------------------------
    # KINOLAR
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # FOYDALANUVCHILAR
    # -----------------------------------------------------

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            prime_until TEXT
        )
    """)

    # -----------------------------------------------------
    # PRIME TO'LOVLAR
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # KINO BUYURTMALARI
    # -----------------------------------------------------

    cur.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            text TEXT,
            created_at TEXT
        )
    """)

    # -----------------------------------------------------
    # BOT SOZLAMALARI
    # -----------------------------------------------------

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # -----------------------------------------------------
    # MAJBURIY KANALLAR
    # -----------------------------------------------------

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

def is_admin_user(user_id):

    admin_id = get_setting("admin_id")

    if not admin_id:
        return False

    try:
        return user_id == int(admin_id)
    except:
        return False


def is_admin(message: Message):

    if is_admin_user(message.from_user.id):
        return True

    username = message.from_user.username

    if username:
        if username.lower() == ADMIN_USERNAME.lower():
            return True

    return False


def save_admin(message: Message):

    username = message.from_user.username

    if username:

        if username.lower() == ADMIN_USERNAME.lower():

            set_setting(
                "admin_id",
                str(message.from_user.id)
            )


# =========================================================
# USER
# =========================================================

def save_user(message: Message):

    user_id = message.from_user.id

    username = message.from_user.username or ""

    db.execute("""
        INSERT INTO users(
            user_id,
            username
        )
        VALUES (?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET username = excluded.username
    """, (
        user_id,
        username
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

    new_until = start + timedelta(
        days=days
    )

    db.execute("""
        UPDATE users
        SET prime_until = ?
        WHERE user_id = ?
    """, (
        new_until.isoformat(),
        user_id
    ))

    db.commit()

    return new_until


# =========================================================
# KANALLARNI OLISH
# =========================================================

def get_channels():

    cur = db.cursor()

    cur.execute("""
        SELECT *
        FROM channels
        ORDER BY id ASC
    """)

    return cur.fetchall()


# =========================================================
# MAJBURIY OBUNA TEKSHIRISH
# =========================================================

async def check_subscription(user_id):

    channels = get_channels()

    # Kanal bo'lmasa majburiy obuna yo'q
    if not channels:
        return True

    for channel in channels:

        try:

            member = await bot.get_chat_member(
                chat_id=channel["chat_id"],
                user_id=user_id
            )

            if member.status in (
                "left",
                "kicked"
            ):
                return False

        except Exception as e:

            print(
                "Kanal tekshirishda xato:",
                channel["username"],
                e
            )

            # Kanal noto'g'ri sozlangan bo'lsa
            # foydalanuvchini ichkariga kiritmaymiz.
            return False

    return True


# =========================================================
# OBUNA KLAVIATURASI
# =========================================================

def subscription_keyboard():

    channels = get_channels()

    buttons = []

    for channel in channels:

        username = channel["username"]

        if username.startswith("@"):

            url = (
                "https://t.me/"
                + username[1:]
            )

        else:

            url = (
                "https://t.me/"
                + username
            )

        buttons.append([
            InlineKeyboardButton(
                text=f"📢 {channel['title']}",
                url=url
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            text="✅ Obuna bo'ldim",
            callback_data="check_sub"
        )
    ])

    return InlineKeyboardMarkup(
        inline_keyboard=buttons
    )


# =========================================================
# OBUNA OYNASI
# =========================================================

async def send_subscription_message(
    message: Message
):

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
        "\n👇 Kanallarga obuna bo'lgach "
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
# CARD MENU
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


# =========================================================
# CHANNEL MENU
# =========================================================

def channel_menu():

    return InlineKeyboardMarkup(
        inline_keyboard=[

            [
                InlineKeyboardButton(
                    text="➕ Kanal qo'shish",
                    callback_data="channel_add"
                )
            ],

            [
                InlineKeyboardButton(
                    text="🗑 Kanal o'chirish",
                    callback_data="channel_delete"
                )
            ],

            [
                InlineKeyboardButton(
                    text="📋 Kanallar ro'yxati",
                    callback_data="channel_list"
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


# =========================================================
# PRIME PLANS
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
            ]

        ]
    )


# =========================================================
# PAYMENT KEYBOARDS
# =========================================================

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
# FSM
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


class MovieRequest(StatesGroup):

    text = State()


class SearchMovie(StatesGroup):

    text = State()


class PaymentScreenshot(StatesGroup):

    screenshot = State()


class ChannelAdd(StatesGroup):

    username = State()


class ChannelDelete(StatesGroup):

    username = State()


# =========================================================
# START
# =========================================================

@dp.message(Command("start"))
async def start_handler(
    message: Message,
    state: FSMContext
):

    await state.clear()

    save_user(message)

    save_admin(message)

    # Adminni avtomatik tanib olamiz
    if is_admin(message):

        await message.answer(
            "🎬 <b>KinoCinema</b> botiga xush kelibsiz!",
            reply_markup=main_menu(
                message.from_user.id
            ),
            parse_mode="HTML"
        )

        return

    # Majburiy obunani tekshiramiz
    subscribed = await check_subscription(
        message.from_user.id
    )

    if not subscribed:

        await send_subscription_message(
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
# OBUNA TASDIQLASH
# =========================================================

@dp.callback_query(
    F.data == "check_sub"
)
async def check_sub_callback(
    callback: CallbackQuery
):

    if is_admin_user(
        callback.from_user.id
    ):

        await callback.message.answer(
            "🎬 Asosiy menyu:",
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
# KANAL SOZLAMALARI
# =========================================================

@dp.message(
    F.text == "📢 Kanal qo'shish"
)
async def channel_settings_add(
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
        "⚠️ Bot kanalga administrator qilib "
        "qo'yilgan bo'lishi kerak.",
        parse_mode="HTML"
    )


# =========================================================
# CHANNEL ADD
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

    username = message.text.strip()

    if not username.startswith("@"):

        username = "@" + username

    try:

        chat = await bot.get_chat(
            username
        )

    except Exception as e:

        print(
            "Kanalni topishda xato:",
            e
        )

        await message.answer(
            "❌ Kanal topilmadi.\n\n"
            "Username'ni to'g'ri yuboring.\n"
            "Masalan: <code>@uz_kinocinema</code>",
            parse_mode="HTML"
        )

        return

    if chat.type not in (
        "channel",
        "supergroup"
    ):

        await message.answer(
            "❌ Bu kanal emas."
        )

        return

    # Bot kanalga kira olishini tekshiramiz
    try:

        bot_member = await bot.get_chat_member(
            chat.id,
            (await bot.get_me()).id
        )

        if bot_member.status not in (
            "administrator",
            "creator"
        ):

            await message.answer(
                "❌ Bot bu kanalga administrator qilib "
                "qo'yilmagan.\n\n"
                "Avval botni kanalga admin qiling."
            )

            return

    except Exception as e:

        print(
            "Bot adminligini tekshirish xatosi:",
            e
        )

        await message.answer(
            "❌ Botning kanalga kirish huquqini "
            "tekshirib bo'lmadi.\n\n"
            "Botni kanalga administrator qilib "
            "qo'yganingizni tekshiring."
        )

        return

    try:

        db.execute("""
            INSERT INTO channels(
                username,
                title,
                chat_id
            )
            VALUES (?, ?, ?)
        """, (
            username,
            chat.title or username,
            str(chat.id)
        ))

        db.commit()

    except sqlite3.IntegrityError:

        await message.answer(
            "❌ Bu kanal allaqachon qo'shilgan."
        )

        await state.clear()

        return

    await state.clear()

    await message.answer(
        "✅ <b>Kanal muvaffaqiyatli qo'shildi!</b>\n\n"
        f"📢 Kanal: <b>{chat.title}</b>\n"
        f"🔗 Username: <code>{username}</code>\n\n"
        "Endi yangi foydalanuvchilardan shu "
        "kanalga obuna bo'lish talab qilinadi.",
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
            "❌ Hozircha majburiy kanal yo'q."
        )

        return

    text = (
        "🗑 <b>Kanal o'chirish</b>\n\n"
        "Mavjud kanallar:\n\n"
    )

    for channel in channels:

        text += (
            f"📢 {channel['title']} — "
            f"<code>{channel['username']}</code>\n"
        )

    text += (
        "\nO'chirmoqchi bo'lgan kanal "
        "username'ini yuboring:"
    )

    await state.set_state(
        ChannelDelete.username
    )

    await message.answer(
        text,
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

    username = message.text.strip()

    if not username.startswith("@"):

        username = "@" + username

    cur = db.cursor()

    cur.execute("""
        SELECT *
        FROM channels
        WHERE username = ?
    """, (
        username,
    ))

    channel = cur.fetchone()

    if not channel:

        await message.answer(
            "❌ Bunday kanal topilmadi."
        )

        await state.clear()

        return

    db.execute(
        "DELETE FROM channels WHERE username = ?",
        (username,)
    )

    db.commit()

    await state.clear()

    await message.answer(
        "🗑 <b>Kanal o'chirildi.</b>\n\n"
        f"📢 {channel['title']}\n"
        f"🔗 {channel['username']}",
        reply_markup=admin_menu(),
        parse_mode="HTML"
    )


# =========================================================
# KANALLAR RO'YXATI
# =========================================================

@dp.callback_query(
    F.data == "channel_list"
)
async def channel_list_callback(
    callback: CallbackQuery
):

    if not is_admin_user(
        callback.from_user.id
    ):
        return

    channels = get_channels()

    if not channels:

        text = (
            "📢 <b>Majburiy kanallar</b>\n\n"
            "❌ Hozircha kanal yo'q."
        )

    else:

        text = (
            "📢 <b>Majburiy kanallar</b>\n\n"
        )

        for i, channel in enumerate(
            channels,
            1
        ):

            text += (
                f"{i}. <b>{channel['title']}</b>\n"
                f"   {channel['username']}\n\n"
            )

    await callback.message.answer(
        text,
        reply_markup=channel_menu(),
        parse_mode="HTML"
    )

    await callback.answer()


# =========================================================
# KARTA SOZLAMALARI
# =========================================================

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
            f"💳 Karta: <code>{card}</code>\n"
            f"👤 Egasi: <b>{owner or 'ko‘rsatilmagan'}</b>\n\n"
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
# CARD ADD
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
            "iborat bo'lishi kerak."
        )

        return

    if len(number) < 12 or len(number) > 19:

        await message.answer(
            "❌ Karta raqami uzunligi noto'g'ri."
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
        "✅ <b>Karta muvaffaqiyatli saqlandi!</b>\n\n"
        f"💳 <code>{data['card_number']}</code>\n"
        f"👤 {owner}",
        reply_markup=card_menu(),
        parse_mode="HTML"
    )


# =========================================================
# CARD VIEW
# =========================================================

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

        text = (
            "❌ Hozircha karta qo'shilmagan."
        )

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


# =========================================================
# CARD DELETE
# =========================================================

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

            await send_subscription_message(
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
            f"⏳ Tugash vaqti: "
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

    if not is_admin_user(
        callback.from_user.id
    ):

        if not await check_subscription(
            callback.from_user.id
        ):

            await callback.answer(
                "Avval kanallarga obuna bo'ling.",
                show_alert=True
            )

            return

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
            "❌ Hozircha karta sozlanmagan."
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

    if order["user_id"] != callback.from_user.id:

        await callback.answer(
            "Bu buyurtma sizniki emas.",
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
        "📸 <b>To'lov skrinshotini yuboring.</b>",
        parse_mode="HTML"
    )

    await callback.answer()


# =========================================================
# SCREENSHOT
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
            "❌ Admin topilmadi.\n\n"
            "Admin akkauntingizdan botga "
            "/start yuboring."
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
            "Admin screenshot xatosi:",
            e
        )

        await message.answer(
            "❌ Screenshotni adminga yuborishda "
            "xatolik yuz berdi."
        )

        await state.clear()

        return

    await message.answer(
        "⏳ <b>To'lovingiz tekshirilmoqda.</b>\n\n"
        "Iltimos, qayta screenshot yubormang.",
        parse_mode="HTML"
    )

    await state.clear()


@dp.message(
    StateFilter(
        PaymentScreenshot.screenshot
    )
)
async def wrong_payment_file(
    message: Message
):

    await message.answer(
        "📸 Iltimos, screenshotni "
        "<b>rasm</b> sifatida yuboring.",
        parse_mode="HTML"
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
            "Userga xabar yuborishda xato:",
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

    except Exception as e:

        print(
            "Caption edit xatosi:",
            e
        )

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

    except Exception as e:

        print(
            "Reject xabar xatosi:",
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
                "❌ <b>BEKOR QILINDI</b>"
            ),
            parse_mode="HTML"
        )

    except Exception as e:

        print(
            "Caption edit xatosi:",
            e
        )

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

            await send_subscription_message(
                message
            )

            return

    await state.set_state(
        SearchMovie.text
    )

    await message.answer(
        "🔎 <b>Kino qidirish</b>\n\n"
        "Kino kodini yozing.\n\n"
        "Masalan: <code>247</code>",
        parse_mode="HTML"
    )


# =========================================================
# KINO QIDIRISH FUNKSIYASI
# =========================================================

async def find_and_send_movies(
    message: Message,
    query: str
):

    query = query.strip()

    cur = db.cursor()

    cur.execute("""
        SELECT *
        FROM movies
        WHERE code = ?
    """, (
        query,
    ))

    movies = cur.fetchall()

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

            await send_subscription_message(
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
            "📚 Hozircha kinolar mavjud emas."
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

    await message.answer(
        "📚 <b>Kinolar ro'yxati</b>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        ),
        parse_mode="HTML"
    )


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
                "Avval kanallarga obuna bo'ling.",
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
# PRIME BACK
# =========================================================

@dp.callback_query(
    F.data == "prime_back"
)
async def prime_back(
    callback: CallbackQuery
):

    await callback.message.answer(
        "⭐ <b>Prime/VIP tariflari</b>",
        reply_markup=prime_plans_keyboard(),
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
                ]
            ]
        )
    )


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

            await send_subscription_message(
                message
            )

            return

    await state.set_state(
        MovieRequest.text
    )

    await message.answer(
        "🎬 <b>Kino buyurtma qilish</b>\n\n"
        "Qaysi kinoni izlayotganingizni yozing:",
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

        except Exception as e:

            print(
                "Buyurtma yuborish xatosi:",
                e
            )

    await state.clear()

    await message.answer(
        "✅ <b>Buyurtmangiz adminga yuborildi.</b>",
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
                ]
            ]
        ),
        parse_mode="HTML"
    )


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

    cur = db.cursor()

    cur.execute(
        "SELECT id FROM movies WHERE code = ?",
        (code,)
    )

    if cur.fetchone():

        await message.answer(
            "❌ Bu kod allaqachon mavjud."
        )

        return

    await state.update_data(
        code=code
    )

    await state.set_state(
        AddMovie.title
    )

    await message.answer(
        "2️⃣ 🎬 Kino nomini yuboring:"
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

    await state.update_data(
        title=message.text.strip()
    )

    await state.set_state(
        AddMovie.video
    )

    await message.answer(
        "3️⃣ 🎥 Kino videosini yuboring:"
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
                ]

            ]
        )
    )


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

    prime = (
        1
        if callback.data == "add_prime"
        else 0
    )

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
        "🗑 O'chirmoqchi bo'lgan kinoning kodini yuboring:"
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
            "❌ Kino topilmadi."
        )

        await state.clear()

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
        f"🔢 <code>{code}</code>",
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
        f"📢 Majburiy kanallar: <b>{channels}</b>\n"
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
        text = text[:4000]

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
# RENDER HEALTH
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
        f"Web server {port} portda ishga tushdi."
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
