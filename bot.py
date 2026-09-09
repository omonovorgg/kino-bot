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

if not TOKEN:
    raise RuntimeError("BOT_TOKEN topilmadi!")

bot = Bot(TOKEN)
dp = Dispatcher()

DB_NAME = "kino_bot.db"


# =========================================================
# DATABASE
# =========================================================

db = sqlite3.connect(DB_NAME, check_same_thread=False)
db.row_factory = sqlite3.Row


def init_db():
    cur = db.cursor()

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

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            prime_until TEXT
        )
    """)

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

    cur.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            text TEXT,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    db.commit()


init_db()


# =========================================================
# SETTINGS
# =========================================================

def get_setting(key, default=""):
    cur = db.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()

    if row:
        return row["value"]

    return default


def set_setting(key, value):
    cur = db.cursor()

    cur.execute("""
        INSERT INTO settings(key, value)
        VALUES (?, ?)
        ON CONFLICT(key)
        DO UPDATE SET value = excluded.value
    """, (key, value))

    db.commit()


def delete_setting(key):
    db.execute("DELETE FROM settings WHERE key = ?", (key,))
    db.commit()


# =========================================================
# ADMIN
# =========================================================

def is_admin(message: Message):
    username = message.from_user.username

    return username and username.lower() == ADMIN_USERNAME.lower()


# =========================================================
# USER
# =========================================================

def save_user(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username or ""

    db.execute("""
        INSERT INTO users(user_id, username)
        VALUES (?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET username = excluded.username
    """, (user_id, username))

    db.commit()


def is_prime(user_id):
    cur = db.cursor()
    cur.execute(
        "SELECT prime_until FROM users WHERE user_id = ?",
        (user_id,)
    )

    row = cur.fetchone()

    if not row or not row["prime_until"]:
        return False

    try:
        until = datetime.fromisoformat(row["prime_until"])
        return until > datetime.now()
    except Exception:
        return False


def activate_prime(user_id, days):
    now = datetime.now()

    cur = db.cursor()
    cur.execute(
        "SELECT prime_until FROM users WHERE user_id = ?",
        (user_id,)
    )

    row = cur.fetchone()

    if row and row["prime_until"]:
        try:
            old_until = datetime.fromisoformat(row["prime_until"])

            if old_until > now:
                start = old_until
            else:
                start = now
        except Exception:
            start = now
    else:
        start = now

    new_until = start + timedelta(days=days)

    db.execute("""
        UPDATE users
        SET prime_until = ?
        WHERE user_id = ?
    """, (new_until.isoformat(), user_id))

    db.commit()

    return new_until


# =========================================================
# MAIN MENU
# =========================================================

def main_menu(user_id=None):

    buttons = [
        [
            KeyboardButton(text="🔎 Kino qidirish"),
            KeyboardButton(text="⭐ Prime status")
        ],
        [
            KeyboardButton(text="📚 Kinolar ro'yxati")
        ],
        [
            KeyboardButton(text="📸 Instagramga qaytish")
        ],
        [
            KeyboardButton(text="🎬 Kino buyurtma qilish")
        ],
        [
            KeyboardButton(text="🤝 Reklama & Bot olish")
        ],
    ]

    if user_id:
        cur = db.cursor()
        cur.execute(
            "SELECT username FROM users WHERE user_id = ?",
            (user_id,)
        )

        row = cur.fetchone()

        if row and row["username"] == ADMIN_USERNAME:
            buttons.append([
                KeyboardButton(text="👨‍💻 Admin panel")
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
                KeyboardButton(text="➕ Kino qo'shish"),
                KeyboardButton(text="🗑 Kino o'chirish")
            ],
            [
                KeyboardButton(text="💳 Karta sozlamalari")
            ],
            [
                KeyboardButton(text="📊 Statistika"),
                KeyboardButton(text="📚 Admin kinolar")
            ],
            [
                KeyboardButton(text="🏠 Asosiy menyu")
            ],
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
            ],
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
            ],
        ]
    )


# =========================================================
# PAYMENT
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
                    text="❌ Rad etish",
                    callback_data=f"reject_{order_id}"
                )
            ]
        ]
    )


# =========================================================
# FSM STATES
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


# =========================================================
# /START
# =========================================================

@dp.message(Command("start"))
async def start_handler(message: Message):

    save_user(message)

    text = (
        "🎬 <b>KinoCinema</b> botiga xush kelibsiz!\n\n"
        "Kerakli bo'limni tanlang:"
    )

    await message.answer(
        text,
        reply_markup=main_menu(message.from_user.id),
        parse_mode="HTML"
    )


# =========================================================
# ADMIN PANEL
# =========================================================

@dp.message(F.text == "👨‍💻 Admin panel")
async def admin_panel(message: Message):

    if not is_admin(message):
        return

    await message.answer(
        "👨‍💻 <b>Admin panel</b>\n\n"
        "Kerakli bo'limni tanlang:",
        reply_markup=admin_menu(),
        parse_mode="HTML"
    )


# =========================================================
# CARD SETTINGS
# =========================================================

@dp.message(F.text == "💳 Karta sozlamalari")
async def card_settings(message: Message):

    if not is_admin(message):
        return

    card = get_setting("payment_card")
    owner = get_setting("payment_owner")

    if card:
        status = (
            "💳 <b>Hozirgi karta:</b>\n"
            f"<code>{card}</code>\n\n"
            f"👤 <b>Egasi:</b> {owner or 'ko‘rsatilmagan'}"
        )
    else:
        status = "❌ Hozircha karta qo'shilmagan."

    await message.answer(
        status + "\n\n⚙️ Karta sozlamalari:",
        reply_markup=card_menu(),
        parse_mode="HTML"
    )


# =========================================================
# ADD CARD
# =========================================================

@dp.callback_query(F.data == "card_add")
async def card_add_start(callback: CallbackQuery, state: FSMContext):

    if callback.from_user.username != ADMIN_USERNAME:
        await callback.answer("Ruxsat yo'q!", show_alert=True)
        return

    await state.set_state(CardAdd.number)

    await callback.message.answer(
        "💳 <b>Karta raqamini yuboring.</b>\n\n"
        "Masalan:\n"
        "<code>9860600435412504</code>",
        parse_mode="HTML"
    )

    await callback.answer()


@dp.message(StateFilter(CardAdd.number))
async def card_number_received(message: Message, state: FSMContext):

    if not is_admin(message):
        return

    number = message.text.strip().replace(" ", "")

    if not number.isdigit():
        await message.answer(
            "❌ Karta raqami faqat raqamlardan iborat bo'lishi kerak.\n"
            "Qaytadan yuboring."
        )
        return

    if len(number) < 12 or len(number) > 19:
        await message.answer(
            "❌ Karta raqami uzunligi noto'g'ri.\n"
            "Qaytadan yuboring."
        )
        return

    await state.update_data(card_number=number)
    await state.set_state(CardAdd.owner)

    await message.answer(
        "👤 <b>Karta egasining ism-familiyasini yuboring.</b>\n\n"
        "Masalan:\n"
        "<code>Muhammad Ali.O.</code>",
        parse_mode="HTML"
    )


@dp.message(StateFilter(CardAdd.owner))
async def card_owner_received(message: Message, state: FSMContext):

    if not is_admin(message):
        return

    owner = message.text.strip()

    data = await state.get_data()
    number = data["card_number"]

    set_setting("payment_card", number)
    set_setting("payment_owner", owner)

    await state.clear()

    await message.answer(
        "✅ <b>Karta muvaffaqiyatli saqlandi!</b>\n\n"
        f"💳 Karta: <code>{number}</code>\n"
        f"👤 Egasi: {owner}",
        reply_markup=card_menu(),
        parse_mode="HTML"
    )


# =========================================================
# VIEW CARD
# =========================================================

@dp.callback_query(F.data == "card_view")
async def card_view(callback: CallbackQuery):

    if callback.from_user.username != ADMIN_USERNAME:
        await callback.answer("Ruxsat yo'q!", show_alert=True)
        return

    card = get_setting("payment_card")
    owner = get_setting("payment_owner")

    if not card:
        text = "❌ Hozircha karta qo'shilmagan."
    else:
        text = (
            "💳 <b>Hozirgi karta</b>\n\n"
            f"💳 Raqam: <code>{card}</code>\n"
            f"👤 Egasi: {owner or 'ko‘rsatilmagan'}"
        )

    await callback.message.answer(
        text,
        reply_markup=card_menu(),
        parse_mode="HTML"
    )

    await callback.answer()


# =========================================================
# DELETE CARD
# =========================================================

@dp.callback_query(F.data == "card_delete")
async def card_delete(callback: CallbackQuery):

    if callback.from_user.username != ADMIN_USERNAME:
        await callback.answer("Ruxsat yo'q!", show_alert=True)
        return

    card = get_setting("payment_card")

    if not card:
        await callback.answer(
            "Karta mavjud emas.",
            show_alert=True
        )
        return

    delete_setting("payment_card")
    delete_setting("payment_owner")

    await callback.message.answer(
        "🗑 <b>Karta o'chirildi.</b>",
        reply_markup=card_menu(),
        parse_mode="HTML"
    )

    await callback.answer()


# =========================================================
# ADMIN BACK
# =========================================================

@dp.callback_query(F.data == "admin_back")
async def admin_back(callback: CallbackQuery):

    if callback.from_user.username != ADMIN_USERNAME:
        return

    await callback.message.answer(
        "👨‍💻 Admin panel:",
        reply_markup=admin_menu()
    )

    await callback.answer()


# =========================================================
# PRIME STATUS
# =========================================================

@dp.message(F.text == "⭐ Prime status")
async def prime_status(message: Message):

    save_user(message)

    if is_prime(message.from_user.id):

        cur = db.cursor()
        cur.execute(
            "SELECT prime_until FROM users WHERE user_id = ?",
            (message.from_user.id,)
        )

        row = cur.fetchone()

        until = row["prime_until"]

        try:
            date_text = datetime.fromisoformat(
                until
            ).strftime("%d.%m.%Y %H:%M")
        except Exception:
            date_text = until

        await message.answer(
            "⭐ <b>Sizda Prime faol!</b>\n\n"
            f"⏳ Amal qilish muddati: <b>{date_text}</b>",
            parse_mode="HTML"
        )

    else:

        await message.answer(
            "⭐ <b>Prime Status</b>\n\n"
            "Sizda Prime mavjud emas.\n\n"
            "Prime tariflardan birini tanlang:",
            reply_markup=prime_plans_keyboard(),
            parse_mode="HTML"
        )


# =========================================================
# PRIME PLAN
# =========================================================

@dp.callback_query(F.data.startswith("plan_"))
async def choose_plan(callback: CallbackQuery):

    plan_id = callback.data.replace("plan_", "")

    if plan_id not in PLANS:
        await callback.answer("Xatolik!")
        return

    plan = PLANS[plan_id]

    card = get_setting("payment_card")
    owner = get_setting("payment_owner")

    if not card:
        await callback.message.answer(
            "❌ Hozircha to'lov kartasi sozlanmagan.\n\n"
            "Iltimos, keyinroq urinib ko'ring."
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

    text = (
        f"⭐ <b>Prime — {plan['name']}</b>\n\n"
        f"💰 Narxi: <b>{plan['price']:,} so'm</b>\n\n"
        f"💳 Karta:\n"
        f"<code>{card}</code>\n\n"
        f"👤 Karta egasi:\n"
        f"<b>{owner}</b>\n\n"
        "To'lovni amalga oshirgach, "
        "«💳 To'ladim» tugmasini bosing."
    )

    await callback.message.answer(
        text,
        reply_markup=payment_keyboard(order_id),
        parse_mode="HTML"
    )

    await callback.answer()


# =========================================================
# PAID
# =========================================================

@dp.callback_query(F.data.startswith("paid_"))
async def paid_handler(callback: CallbackQuery, state: FSMContext):

    order_id = int(callback.data.replace("paid_", ""))

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

    await state.update_data(order_id=order_id)
    await state.set_state(PaymentScreenshot.screenshot)

    await callback.message.answer(
        "📸 <b>To'lov skrinshotini yuboring.</b>\n\n"
        "Skrinshotni rasm ko'rinishida yuboring.",
        parse_mode="HTML"
    )

    await callback.answer()


# =========================================================
# PAYMENT SCREENSHOT
# =========================================================

@dp.message(StateFilter(PaymentScreenshot.screenshot), F.photo)
async def payment_screenshot(
    message: Message,
    state: FSMContext
):

    data = await state.get_data()
    order_id = data["order_id"]

    cur = db.cursor()

    cur.execute(
        "SELECT * FROM orders WHERE id = ?",
        (order_id,)
    )

    order = cur.fetchone()

    if not order:
        await state.clear()
        await message.answer("❌ Buyurtma topilmadi.")
        return

    photo = message.photo[-1]

    caption = (
        "💳 <b>Yangi Prime to'lovi!</b>\n\n"
        f"🆔 Buyurtma: <code>#{order_id}</code>\n"
        f"👤 User ID: <code>{order['user_id']}</code>\n"
        f"👤 Username: @{order['username'] or 'yo‘q'}\n"
        f"⭐ Tarif: <b>{order['plan']}</b>\n"
        f"💰 Summa: <b>{order['price']:,} so'm</b>"
    )

    try:

        await bot.send_photo(
            chat_id=f"@{ADMIN_USERNAME}",
            photo=photo.file_id,
            caption=caption,
            parse_mode="HTML",
            reply_markup=admin_payment_keyboard(order_id)
        )

    except Exception:

        await message.answer(
            "❌ Admin bilan bog'lanib bo'lmadi.\n\n"
            "Admin botga /start yuborganini tekshiring."
        )

        await state.clear()
        return

    await message.answer(
        "✅ <b>Skrinshot admin ga yuborildi.</b>\n\n"
        "To'lov tekshirilgach Prime faollashtiriladi.",
        parse_mode="HTML"
    )

    await state.clear()


@dp.message(StateFilter(PaymentScreenshot.screenshot))
async def payment_wrong_file(message: Message):

    await message.answer(
        "📸 Iltimos, to'lov skrinshotini <b>rasm</b> sifatida yuboring.",
        parse_mode="HTML"
    )


# =========================================================
# APPROVE PAYMENT
# =========================================================

@dp.callback_query(F.data.startswith("approve_"))
async def approve_payment(callback: CallbackQuery):

    if callback.from_user.username != ADMIN_USERNAME:
        await callback.answer(
            "Ruxsat yo'q!",
            show_alert=True
        )
        return

    order_id = int(
        callback.data.replace("approve_", "")
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

    if order["status"] == "approved":
        await callback.answer(
            "Bu to'lov allaqachon tasdiqlangan.",
            show_alert=True
        )
        return

    activate_prime(
        order["user_id"],
        order["days"]
    )

    db.execute("""
        UPDATE orders
        SET status = 'approved'
        WHERE id = ?
    """, (order_id,))

    db.commit()

    await bot.send_message(
        order["user_id"],
        "🎉 <b>To'lov tasdiqlandi!</b>\n\n"
        f"⭐ Prime: <b>{order['plan']}</b>\n"
        "✅ Prime hisobingizga faollashtirildi.\n\n"
        "Yoqimli foydalaning!",
        parse_mode="HTML"
    )

    await callback.message.edit_caption(
        caption=(
            callback.message.caption or ""
            + "\n\n"
            "✅ <b>TASDIQLANDI</b>"
        ),
        parse_mode="HTML"
    )

    await callback.answer("Prime faollashtirildi!")


# =========================================================
# REJECT PAYMENT
# =========================================================

@dp.callback_query(F.data.startswith("reject_"))
async def reject_payment(callback: CallbackQuery):

    if callback.from_user.username != ADMIN_USERNAME:
        await callback.answer(
            "Ruxsat yo'q!",
            show_alert=True
        )
        return

    order_id = int(
        callback.data.replace("reject_", "")
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

    db.execute("""
        UPDATE orders
        SET status = 'rejected'
        WHERE id = ?
    """, (order_id,))

    db.commit()

    await bot.send_message(
        order["user_id"],
        "❌ <b>To'lov tasdiqlanmadi.</b>\n\n"
        "Iltimos, to'lov ma'lumotlarini tekshirib, "
        "qaytadan urinib ko'ring.",
        parse_mode="HTML"
    )

    await callback.message.edit_caption(
        caption=(
            callback.message.caption or ""
            + "\n\n"
            "❌ <b>RAD ETILDI</b>"
        ),
        parse_mode="HTML"
    )

    await callback.answer("To'lov rad etildi.")


# =========================================================
# SEARCH
# =========================================================

@dp.message(F.text == "🔎 Kino qidirish")
async def search_start(message: Message, state: FSMContext):

    await state.set_state(SearchMovie.text)

    await message.answer(
        "🔎 <b>Kino qidirish</b>\n\n"
        "Kino kodini yoki nomini yozing:",
        parse_mode="HTML"
    )


@dp.message(StateFilter(SearchMovie.text))
async def search_movie(message: Message, state: FSMContext):

    query = message.text.strip()

    cur = db.cursor()

    cur.execute("""
        SELECT *
        FROM movies
        WHERE code = ?
        OR title LIKE ?
        ORDER BY id DESC
    """, (query, f"%{query}%"))

    movies = cur.fetchall()

    if not movies:

        await message.answer(
            "❌ Kino topilmadi.\n\n"
            "Kod yoki nomini tekshirib qayta urinib ko'ring."
        )

        await state.clear()
        return

    await state.clear()

    for movie in movies[:10]:

        if movie["prime"] and not is_prime(message.from_user.id):

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="⭐ Prime olish",
                            callback_data="prime_back"
                        )
                    ]
                ]
            )

            await message.answer(
                f"🔒 <b>{movie['title']}</b>\n\n"
                "Bu kino faqat Prime foydalanuvchilar uchun.",
                reply_markup=keyboard,
                parse_mode="HTML"
            )

            continue

        db.execute("""
            UPDATE movies
            SET views = views + 1
            WHERE id = ?
        """, (movie["id"],))

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


# =========================================================
# MOVIE LIST
# =========================================================

@dp.message(F.text == "📚 Kinolar ro'yxati")
async def movie_list(message: Message):

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
        "📚 <b>Kinolar ro'yxati</b>\n\n"
        "Kerakli kinoni tanlang:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        ),
        parse_mode="HTML"
    )


# =========================================================
# MOVIE CLICK
# =========================================================

@dp.callback_query(F.data.startswith("movie_"))
async def movie_click(callback: CallbackQuery):

    movie_id = int(
        callback.data.replace("movie_", "")
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

    if movie["prime"] and not is_prime(callback.from_user.id):

        await callback.message.answer(
            "🔒 Bu kino faqat Prime uchun.",
            reply_markup=prime_plans_keyboard()
        )

        await callback.answer()
        return

    db.execute("""
        UPDATE movies
        SET views = views + 1
        WHERE id = ?
    """, (movie_id,))

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

@dp.callback_query(F.data == "prime_back")
async def prime_back(callback: CallbackQuery):

    await callback.message.answer(
        "⭐ <b>Prime tariflar</b>\n\n"
        "Kerakli tarifni tanlang:",
        reply_markup=prime_plans_keyboard(),
        parse_mode="HTML"
    )

    await callback.answer()


# =========================================================
# INSTAGRAM
# =========================================================

@dp.message(F.text == "📸 Instagramga qaytish")
async def instagram(message: Message):

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📸 Instagram",
                    url=INSTAGRAM_URL
                )
            ]
        ]
    )

    await message.answer(
        "📸 Instagram sahifamiz:",
        reply_markup=keyboard
    )


# =========================================================
# MOVIE REQUEST
# =========================================================

@dp.message(F.text == "🎬 Kino buyurtma qilish")
async def movie_request_start(
    message: Message,
    state: FSMContext
):

    await state.set_state(MovieRequest.text)

    await message.answer(
        "🎬 <b>Kino buyurtma qilish</b>\n\n"
        "Qaysi kinoni izlayotganingizni yozing:",
        parse_mode="HTML"
    )


@dp.message(StateFilter(MovieRequest.text))
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

    admin_text = (
        "🎬 <b>Yangi kino buyurtmasi!</b>\n\n"
        f"👤 User ID: <code>{message.from_user.id}</code>\n"
        f"👤 Username: @{message.from_user.username or 'yo‘q'}\n\n"
        f"📝 So'rov:\n{request_text}"
    )

    try:
        await bot.send_message(
            f"@{ADMIN_USERNAME}",
            admin_text,
            parse_mode="HTML"
        )
    except Exception:
        pass

    await state.clear()

    await message.answer(
        "✅ Buyurtmangiz adminga yuborildi.\n\n"
        "Tez orada ko'rib chiqiladi."
    )


# =========================================================
# ADVERTISEMENT
# =========================================================

@dp.message(F.text == "🤝 Reklama & Bot olish")
async def advertisement(message: Message):

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👨‍💻 Admin bilan bog'lanish",
                    url=f"https://t.me/{ADMIN_USERNAME}"
                )
            ]
        ]
    )

    await message.answer(
        "🤝 <b>Reklama & Bot olish</b>\n\n"
        "Reklama berish yoki bot buyurtma qilish uchun "
        "admin bilan bog'laning.",
        reply_markup=keyboard,
        parse_mode="HTML"
    )


# =========================================================
# ADMIN: ADD MOVIE
# =========================================================

@dp.message(F.text == "➕ Kino qo'shish")
async def add_movie_start(
    message: Message,
    state: FSMContext
):

    if not is_admin(message):
        return

    await state.set_state(AddMovie.code)

    await message.answer(
        "➕ <b>Kino qo'shish</b>\n\n"
        "Kino kodini yuboring:\n"
        "Masalan: <code>1234</code>",
        parse_mode="HTML"
    )


@dp.message(StateFilter(AddMovie.code))
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
            "❌ Bu kod allaqachon mavjud.\n"
            "Boshqa kod yuboring."
        )
        return

    await state.update_data(code=code)
    await state.set_state(AddMovie.title)

    await message.answer(
        "🎬 Kino nomini yuboring:"
    )


@dp.message(StateFilter(AddMovie.title))
async def add_movie_title(
    message: Message,
    state: FSMContext
):

    if not is_admin(message):
        return

    await state.update_data(
        title=message.text.strip()
    )

    await state.set_state(AddMovie.video)

    await message.answer(
        "🎥 Endi kino videosini yuboring:"
    )


@dp.message(StateFilter(AddMovie.video), F.video)
async def add_movie_video(
    message: Message,
    state: FSMContext
):

    if not is_admin(message):
        return

    await state.update_data(
        file_id=message.video.file_id
    )

    await state.set_state(AddMovie.prime)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🆓 Oddiy kino",
                    callback_data="add_normal"
                )
            ],
            [
                InlineKeyboardButton(
                    text="⭐ Prime kino",
                    callback_data="add_prime"
                )
            ]
        ]
    )

    await message.answer(
        "Kino turini tanlang:",
        reply_markup=keyboard
    )


@dp.message(StateFilter(AddMovie.video))
async def add_movie_video_wrong(message: Message):

    await message.answer(
        "❌ Iltimos, videoni video sifatida yuboring."
    )


@dp.callback_query(F.data.in_({"add_normal", "add_prime"}))
async def finish_add_movie(
    callback: CallbackQuery,
    state: FSMContext
):

    if callback.from_user.username != ADMIN_USERNAME:
        await callback.answer(
            "Ruxsat yo'q!",
            show_alert=True
        )
        return

    data = await state.get_data()

    if not data:
        await callback.answer(
            "Jarayon topilmadi.",
            show_alert=True
        )
        return

    prime = 1 if callback.data == "add_prime" else 0

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
            "❌ Bu kino kodi allaqachon mavjud."
        )

        await state.clear()
        await callback.answer()
        return

    await state.clear()

    await callback.message.answer(
        "✅ <b>Kino muvaffaqiyatli qo'shildi!</b>\n\n"
        f"🎬 Nomi: {data['title']}\n"
        f"🔢 Kodi: <code>{data['code']}</code>\n"
        f"⭐ Prime: {'Ha' if prime else 'Yo‘q'}",
        parse_mode="HTML",
        reply_markup=admin_menu()
    )

    await callback.answer()


# =========================================================
# ADMIN: DELETE MOVIE
# =========================================================

@dp.message(F.text == "🗑 Kino o'chirish")
async def delete_movie_start(
    message: Message,
    state: FSMContext
):

    if not is_admin(message):
        return

    await state.set_state(DeleteMovie.code)

    await message.answer(
        "🗑 O'chirmoqchi bo'lgan kinoning kodini yuboring:"
    )


@dp.message(StateFilter(DeleteMovie.code))
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
            "❌ Bunday kodli kino topilmadi."
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
        f"🔢 Kod: <code>{code}</code>",
        parse_mode="HTML",
        reply_markup=admin_menu()
    )


# =========================================================
# ADMIN STATISTICS
# =========================================================

@dp.message(F.text == "📊 Statistika")
async def statistics(message: Message):

    if not is_admin(message):
        return

    cur = db.cursor()

    cur.execute("SELECT COUNT(*) AS c FROM users")
    users = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) AS c FROM movies")
    movies = cur.fetchone()["c"]

    cur.execute(
        "SELECT COUNT(*) AS c FROM movies WHERE prime = 1"
    )
    prime_movies = cur.fetchone()["c"]

    cur.execute(
        "SELECT COUNT(*) AS c FROM users "
        "WHERE prime_until IS NOT NULL"
    )
    prime_users = cur.fetchone()["c"]

    cur.execute(
        "SELECT SUM(views) AS v FROM movies"
    )
    views = cur.fetchone()["v"] or 0

    await message.answer(
        "📊 <b>Bot statistikasi</b>\n\n"
        f"👥 Foydalanuvchilar: <b>{users}</b>\n"
        f"🎬 Kinolar: <b>{movies}</b>\n"
        f"⭐ Prime kinolar: <b>{prime_movies}</b>\n"
        f"⭐ Prime foydalanuvchilar: <b>{prime_users}</b>\n"
        f"👁 Ko'rishlar: <b>{views}</b>",
        parse_mode="HTML"
    )


# =========================================================
# ADMIN MOVIES
# =========================================================

@dp.message(F.text == "📚 Admin kinolar")
async def admin_movies(message: Message):

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

    text = "📚 <b>Barcha kinolar:</b>\n\n"

    for movie in movies:

        text += (
            f"🎬 <b>{movie['title']}</b>\n"
            f"🔢 Kod: <code>{movie['code']}</code>\n"
            f"⭐ Prime: {'Ha' if movie['prime'] else 'Yo‘q'}\n"
            f"👁 Ko'rish: {movie['views']}\n\n"
        )

    if len(text) > 4000:
        text = text[:4000] + "\n..."

    await message.answer(
        text,
        parse_mode="HTML"
    )


# =========================================================
# MAIN MENU
# =========================================================

@dp.message(F.text == "🏠 Asosiy menyu")
async def back_main(message: Message, state: FSMContext):

    await state.clear()

    await message.answer(
        "🏠 <b>Asosiy menyu</b>",
        reply_markup=main_menu(message.from_user.id),
        parse_mode="HTML"
    )


# =========================================================
# HEALTH SERVER — RENDER UCHUN
# =========================================================

async def health(request):
    return web.Response(text="KinoCinema bot ishlayapti!")


async def start_web_server():

    app = web.Application()

    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    port = int(os.getenv("PORT", "10000"))

    runner = web.AppRunner(app)

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port
    )

    await site.start()

    print(f"Web server {port} portda ishga tushdi.")


# =========================================================
# RUN
# =========================================================

async def main():

    print("KinoCinema bot ishga tushmoqda...")

    await start_web_server()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
