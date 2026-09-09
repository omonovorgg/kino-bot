import os
import asyncio
import sqlite3
from datetime import datetime, timedelta

from aiohttp import web

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton
)
from aiogram.utils.keyboard import InlineKeyboardBuilder


# =========================================================
# SOZLAMALAR
# =========================================================

TOKEN = os.getenv("BOT_TOKEN")

ADMIN_USERNAME = "omono_v"

# Keyin o'zingiznikiga almashtirasiz
INSTAGRAM_URL = "https://instagram.com/"

PAYMENT_CARD = "KARTA RAQAMINI SHU YERGA YOZING"
PAYMENT_OWNER = "KARTA EGASI"

bot = Bot(TOKEN)
dp = Dispatcher()


# =========================================================
# DATABASE
# =========================================================

db = sqlite3.connect("movies.db")
cursor = db.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS movies (
    code TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    file_id TEXT NOT NULL,
    prime INTEGER DEFAULT 0
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    prime_until TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    username TEXT,
    plan TEXT,
    days INTEGER,
    status TEXT DEFAULT 'pending',
    created_at TEXT
)
""")

db.commit()


# =========================================================
# ADMIN TEKSHIRISH
# =========================================================

def is_admin(user: types.User):

    return (
        user.username
        and user.username.lower() == ADMIN_USERNAME.lower()
    )


# =========================================================
# PRIME TEKSHIRISH
# =========================================================

def get_prime_until(user_id):

    cursor.execute(
        "SELECT prime_until FROM users WHERE user_id = ?",
        (user_id,)
    )

    result = cursor.fetchone()

    if not result or not result[0]:
        return None

    try:
        date = datetime.fromisoformat(result[0])

        if date > datetime.now():
            return date

    except Exception:
        pass

    return None


def has_prime(user_id):

    return get_prime_until(user_id) is not None


# =========================================================
# ASOSIY MENYU
# =========================================================

def main_menu(user=None):

    buttons = [
        [
            KeyboardButton(text="🔎 Kino qidirish"),
            KeyboardButton(text="⭐ Prime status")
        ],
        [
            KeyboardButton(text="📚 Kinolar ro'yxati"),
            KeyboardButton(text="📸 Instagramga qaytish")
        ],
        [
            KeyboardButton(text="🎬 Kino buyurtma qilish"),
            KeyboardButton(text="🤝 Reklama & Bot olish")
        ]
    ]

    if user and is_admin(user):
        buttons.append([
            KeyboardButton(text="👨‍💻 Admin panel")
        ])

    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True
    )


# =========================================================
# /START
# =========================================================

@dp.message(Command("start"))
async def start(message: types.Message):

    user = message.from_user

    cursor.execute("""
        INSERT OR IGNORE INTO users (user_id, username)
        VALUES (?, ?)
    """, (
        user.id,
        user.username
    ))

    cursor.execute("""
        UPDATE users
        SET username = ?
        WHERE user_id = ?
    """, (
        user.username,
        user.id
    ))

    db.commit()

    await message.answer(
        "🎬 <b>KinoCinema botiga xush kelibsiz!</b>\n\n"
        "Kinoni topish uchun 🔎 <b>Kino qidirish</b> "
        "tugmasini bosing.\n\n"
        "⭐ Prime orqali barcha imkoniyatlardan foydalanishingiz mumkin.",
        parse_mode="HTML",
        reply_markup=main_menu(user)
    )


# =========================================================
# KINO QIDIRISH
# =========================================================

@dp.message(F.text == "🔎 Kino qidirish")
async def search_movie_start(message: types.Message):

    await message.answer(
        "🎬 <b>Kino qidirish</b>\n\n"
        "Kino kodini yuboring.\n"
        "Masalan: <code>231</code>\n\n"
        "Yoki kino nomini yozing.",
        parse_mode="HTML"
    )


# =========================================================
# KINO KODI / NOMI
# =========================================================

@dp.message()
async def movie_search(message: types.Message):

    # Menyu tugmalarini bu yerda qayta ishlamaymiz
    menu_words = [
        "⭐ Prime status",
        "📚 Kinolar ro'yxati",
        "📸 Instagramga qaytish",
        "🎬 Kino buyurtma qilish",
        "🤝 Reklama & Bot olish",
        "👨‍💻 Admin panel"
    ]

    if message.text in menu_words:
        return

    if not message.text:
        return

    text = message.text.strip()

    # KOD BO'YICHA
    cursor.execute("""
        SELECT code, title, file_id, prime
        FROM movies
        WHERE code = ?
    """, (text,))

    movie = cursor.fetchone()

    # NOM BO'YICHA
    if not movie:

        cursor.execute("""
            SELECT code, title, file_id, prime
            FROM movies
            WHERE title LIKE ?
            LIMIT 10
        """, (
            "%" + text + "%",
        ))

        results = cursor.fetchall()

        if results:

            text_result = "🎬 <b>Topilgan kinolar:</b>\n\n"

            for code, title, file_id, prime in results:

                icon = "⭐" if prime else "🎬"

                text_result += (
                    f"{icon} <b>{title}</b>\n"
                    f"🆔 Kod: <code>{code}</code>\n\n"
                )

            await message.answer(
                text_result,
                parse_mode="HTML"
            )

            return

        await message.answer(
            "❌ Bunday kino topilmadi.\n\n"
            "Kino kodi yoki nomini to'g'ri kiriting."
        )

        return

    code, title, file_id, prime = movie

    # PRIME KINO
    if prime and not has_prime(message.from_user.id):

        builder = InlineKeyboardBuilder()

        builder.row(
            InlineKeyboardButton(
                text="⭐ Prime olish",
                callback_data="prime_menu"
            )
        )

        await message.answer(
            "🔒 <b>Bu kino faqat Prime foydalanuvchilari uchun.</b>\n\n"
            "⭐ Prime obuna olib, ushbu kinoni tomosha qilishingiz mumkin.",
            parse_mode="HTML",
            reply_markup=builder.as_markup()
        )

        return

    # KINONI YUBORISH
    await message.answer_video(
        video=file_id,
        caption=(
            f"🎬 <b>Kino nomi:</b> {title}\n"
            f"🆔 <b>Kino kodi:</b> #{code}\n\n"
            "🍿 Yoqimli tomosha!"
        ),
        parse_mode="HTML"
    )


# =========================================================
# PRIME STATUS
# =========================================================

@dp.message(F.text == "⭐ Prime status")
async def prime_status(message: types.Message):

    prime_until = get_prime_until(message.from_user.id)

    if prime_until:

        await message.answer(
            "⭐ <b>Sizda Prime faol!</b>\n\n"
            f"📅 Amal qilish muddati: "
            f"<b>{prime_until.strftime('%d.%m.%Y %H:%M')}</b>\n\n"
            "🎬 Barcha Prime kinolar ochiq.",
            parse_mode="HTML"
        )

        return

    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text="⭐ Prime olish",
            callback_data="prime_menu"
        )
    )

    await message.answer(
        "⭐ <b>Prime status</b>\n\n"
        "Sizda hozir Prime mavjud emas.\n\n"
        "Prime bilan:\n"
        "✅ Barcha kinolar ochiladi\n"
        "✅ Majburiy kanal obunasi yo'q\n"
        "✅ Tez va qulay foydalanish\n\n"
        "🚀 Prime olish uchun tugmani bosing.",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )


# =========================================================
# PRIME MENYU
# =========================================================

@dp.callback_query(F.data == "prime_menu")
async def prime_menu(callback: types.CallbackQuery):

    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text="⭐ 7 kun — 7 000 so'm",
            callback_data="buy_7"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="⭐ 1 oy — 20 000 so'm",
            callback_data="buy_30"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="⭐ 3 oy — 50 000 so'm",
            callback_data="buy_90"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="♾️ Umrbod — 150 000 so'm",
            callback_data="buy_forever"
        )
    )

    await callback.message.edit_text(
        "⭐ <b>Prime obuna</b>\n\n"
        "💰 7 kun — 7 000 so'm\n"
        "💰 1 oy — 20 000 so'm\n"
        "💰 3 oy — 50 000 so'm\n"
        "♾️ Umrbod — 150 000 so'm\n\n"
        "✅ Barcha kinolar ochiq\n"
        "✅ Majburiy kanal obunasi yo'q\n"
        "✅ Tez va qulay kirish\n\n"
        "🚀 Hozir tanlang:",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )

    await callback.answer()


# =========================================================
# PRIME SOTIB OLISH
# =========================================================

@dp.callback_query(F.data.startswith("buy_"))
async def buy_prime(callback: types.CallbackQuery):

    plan = callback.data

    if plan == "buy_7":
        plan_name = "7 kun"
        days = 7
        price = "7 000"

    elif plan == "buy_30":
        plan_name = "1 oy"
        days = 30
        price = "20 000"

    elif plan == "buy_90":
        plan_name = "3 oy"
        days = 90
        price = "50 000"

    else:
        plan_name = "Umrbod"
        days = 36500
        price = "150 000"

    cursor.execute("""
        INSERT INTO orders
        (user_id, username, plan, days, status, created_at)
        VALUES (?, ?, ?, ?, 'pending', ?)
    """, (
        callback.from_user.id,
        callback.from_user.username,
        plan_name,
        days,
        datetime.now().isoformat()
    ))

    db.commit()

    order_id = cursor.lastrowid

    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text="✅ To'ladim",
            callback_data=f"paid_{order_id}"
        )
    )

    await callback.message.edit_text(
        f"⭐ <b>Prime obuna</b>\n\n"
        f"📅 Muddat: <b>{plan_name}</b>\n"
        f"💰 Narx: <b>{price} so'm</b>\n\n"
        f"💳 <b>To'lov uchun karta:</b>\n"
        f"<code>{PAYMENT_CARD}</code>\n\n"
        f"👤 Egasi: <b>{PAYMENT_OWNER}</b>\n\n"
        "To'lovni amalga oshirgach, "
        "«✅ To'ladim» tugmasini bosing.",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )

    await callback.answer()


# =========================================================
# TO'LADIM
# =========================================================

@dp.callback_query(F.data.startswith("paid_"))
async def paid(callback: types.CallbackQuery):

    order_id = int(callback.data.split("_")[1])

    cursor.execute("""
        SELECT plan
        FROM orders
        WHERE id = ? AND user_id = ?
    """, (
        order_id,
        callback.from_user.id
    ))

    result = cursor.fetchone()

    if not result:
        await callback.answer(
            "Buyurtma topilmadi.",
            show_alert=True
        )
        return

    plan = result[0]

    cursor.execute("""
        UPDATE orders
        SET status = 'checking'
        WHERE id = ?
    """, (order_id,))

    db.commit()

    await callback.message.edit_text(
        "📸 <b>To'lov tasdiqlash</b>\n\n"
        "Iltimos, to'lov screenshotini shu chatga yuboring.\n\n"
        "Admin tekshirganidan keyin Prime yoqiladi.",
        parse_mode="HTML"
    )

    await callback.answer()


# =========================================================
# TO'LOV SCREENSHOT
# =========================================================

@dp.message(F.photo)
async def payment_photo(message: types.Message):

    user = message.from_user

    cursor.execute("""
        SELECT id, plan
        FROM orders
        WHERE user_id = ?
        AND status = 'checking'
        ORDER BY id DESC
        LIMIT 1
    """, (user.id,))

    result = cursor.fetchone()

    if not result:
        return

    order_id, plan = result

    admin_text = (
        "💳 <b>Yangi Prime to'lovi!</b>\n\n"
        f"👤 Username: @{user.username or 'username yo‘q'}\n"
        f"🆔 ID: <code>{user.id}</code>\n"
        f"⭐ Tarif: <b>{plan}</b>\n"
        f"📦 Buyurtma: <code>#{order_id}</code>"
    )

    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text="✅ Tasdiqlash",
            callback_data=f"approve_{order_id}"
        ),
        InlineKeyboardButton(
            text="❌ Rad etish",
            callback_data=f"reject_{order_id}"
        )
    )

    await bot.send_photo(
        chat_id=user.id,
        photo=message.photo[-1].file_id,
        caption="⏳ To'lovingiz adminga yuborildi."
    )

    # Admin username orqali xabar yuborishga urinish
    try:

        await bot.send_photo(
            chat_id=f"@{ADMIN_USERNAME}",
            photo=message.photo[-1].file_id,
            caption=admin_text,
            parse_mode="HTML",
            reply_markup=builder.as_markup()
        )

    except Exception:

        await message.answer(
            "⏳ Screenshot qabul qilindi.\n"
            "Admin tekshiradi."
        )


# =========================================================
# PRIME TASDIQLASH
# =========================================================

@dp.callback_query(F.data.startswith("approve_"))
async def approve_payment(callback: types.CallbackQuery):

    if not is_admin(callback.from_user):
        await callback.answer(
            "❌ Siz admin emassiz.",
            show_alert=True
        )
        return

    order_id = int(callback.data.split("_")[1])

    cursor.execute("""
        SELECT user_id, plan, days
        FROM orders
        WHERE id = ?
    """, (order_id,))

    result = cursor.fetchone()

    if not result:
        await callback.answer("Buyurtma topilmadi.")
        return

    user_id, plan, days = result

    if days >= 36500:

        prime_until = datetime.now() + timedelta(days=36500)

    else:

        old = get_prime_until(user_id)

        if old and old > datetime.now():
            prime_until = old + timedelta(days=days)
        else:
            prime_until = datetime.now() + timedelta(days=days)

    cursor.execute("""
        INSERT OR REPLACE INTO users
        (user_id, username, prime_until)
        VALUES (
            ?,
            (SELECT username FROM users WHERE user_id = ?),
            ?
        )
    """, (
        user_id,
        user_id,
        prime_until.isoformat()
    ))

    cursor.execute("""
        UPDATE orders
        SET status = 'approved'
        WHERE id = ?
    """, (order_id,))

    db.commit()

    await bot.send_message(
        user_id,
        "🎉 <b>Prime faollashtirildi!</b>\n\n"
        f"⭐ Tarif: <b>{plan}</b>\n"
        f"📅 Amal qilish muddati: "
        f"<b>{prime_until.strftime('%d.%m.%Y %H:%M')}</b>\n\n"
        "🍿 Yoqimli tomosha!",
        parse_mode="HTML"
    )

    await callback.message.edit_caption(
        caption="✅ <b>To'lov tasdiqlandi.</b>",
        parse_mode="HTML"
    )

    await callback.answer("Prime faollashtirildi.")


# =========================================================
# PRIME RAD ETISH
# =========================================================

@dp.callback_query(F.data.startswith("reject_"))
async def reject_payment(callback: types.CallbackQuery):

    if not is_admin(callback.from_user):
        await callback.answer(
            "❌ Siz admin emassiz.",
            show_alert=True
        )
        return

    order_id = int(callback.data.split("_")[1])

    cursor.execute("""
        SELECT user_id
        FROM orders
        WHERE id = ?
    """, (order_id,))

    result = cursor.fetchone()

    if result:

        user_id = result[0]

        cursor.execute("""
            UPDATE orders
            SET status = 'rejected'
            WHERE id = ?
        """, (order_id,))

        db.commit()

        await bot.send_message(
            user_id,
            "❌ <b>To'lovingiz rad etildi.</b>\n\n"
            "Iltimos, to'lov screenshotini tekshirib qayta yuboring.",
            parse_mode="HTML"
        )

    await callback.message.edit_caption(
        caption="❌ <b>To'lov rad etildi.</b>",
        parse_mode="HTML"
    )

    await callback.answer()


# =========================================================
# KINOLAR RO'YXATI
# =========================================================

@dp.message(F.text == "📚 Kinolar ro'yxati")
async def movie_list(message: types.Message):

    cursor.execute("""
        SELECT code, title, prime
        FROM movies
        ORDER BY rowid DESC
        LIMIT 30
    """)

    movies = cursor.fetchall()

    if not movies:

        await message.answer(
            "📚 Hozircha kinolar mavjud emas."
        )

        return

    result = "📚 <b>Kinolar ro'yxati</b>\n\n"

    for code, title, prime in movies:

        icon = "⭐" if prime else "🎬"

        result += (
            f"{icon} <b>{title}</b>\n"
            f"🆔 Kod: <code>{code}</code>\n\n"
        )

    await message.answer(
        result,
        parse_mode="HTML"
    )


# =========================================================
# INSTAGRAM
# =========================================================

@dp.message(F.text == "📸 Instagramga qaytish")
async def instagram(message: types.Message):

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
        "📸 <b>Instagram sahifamizga o'tish:</b>",
        parse_mode="HTML",
        reply_markup=keyboard
    )


# =========================================================
# KINO BUYURTMA
# =========================================================

@dp.message(F.text == "🎬 Kino buyurtma qilish")
async def movie_order(message: types.Message):

    await message.answer(
        "🎬 <b>Kino buyurtma qilish</b>\n\n"
        "Qaysi kinoni izlayapsiz?\n"
        "Kino nomini yozib yuboring.\n\n"
        "Xabaringiz adminlarga yuboriladi.",
        parse_mode="HTML"
    )


# =========================================================
# REKLAMA
# =========================================================

@dp.message(F.text == "🤝 Reklama & Bot olish")
async def advertising(message: types.Message):

    await message.answer(
        "🤝 <b>Reklama & Bot olish</b>\n\n"
        "📢 Reklama joylashtirish yoki bot buyurtma qilish "
        "uchun admin bilan bog'laning.\n\n"
        "👤 Admin: @omono_v",
        parse_mode="HTML"
    )


# =========================================================
# ADMIN PANEL
# =========================================================

@dp.message(F.text == "👨‍💻 Admin panel")
async def admin_panel(message: types.Message):

    if not is_admin(message.from_user):

        await message.answer("❌ Siz admin emassiz.")
        return

    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="➕ Kino qo'shish"),
                KeyboardButton(text="🗑 Kino o'chirish")
            ],
            [
                KeyboardButton(text="📊 Statistika"),
                KeyboardButton(text="📚 Kinolar")
            ],
            [
                KeyboardButton(text="🏠 Asosiy menyu")
            ]
        ],
        resize_keyboard=True
    )

    await message.answer(
        "👨‍💻 <b>Admin panel</b>\n\n"
        "Kerakli bo'limni tanlang.",
        parse_mode="HTML",
        reply_markup=keyboard
    )


# =========================================================
# ADMIN KINO QO'SHISH FSM
# =========================================================

class AddMovie(StatesGroup):

    code = State()
    title = State()
    video = State()
    prime = State()


@dp.message(F.text == "➕ Kino qo'shish")
async def add_movie_start(message: types.Message, state: FSMContext):

    if not is_admin(message.from_user):
        return

    await state.set_state(AddMovie.code)

    await message.answer(
        "➕ <b>Kino qo'shish</b>\n\n"
        "1️⃣ Kino kodini yuboring.\n"
        "Masalan: <code>231</code>",
        parse_mode="HTML"
    )


@dp.message(AddMovie.code)
async def add_movie_code(message: types.Message, state: FSMContext):

    code = message.text.strip()

    cursor.execute(
        "SELECT code FROM movies WHERE code = ?",
        (code,)
    )

    if cursor.fetchone():

        await message.answer(
            "❌ Bu kod allaqachon mavjud.\n"
            "Boshqa kod yuboring."
        )
        return

    await state.update_data(code=code)

    await state.set_state(AddMovie.title)

    await message.answer(
        "🎬 Endi kino nomini yuboring."
    )


@dp.message(AddMovie.title)
async def add_movie_title(message: types.Message, state: FSMContext):

    await state.update_data(
        title=message.text.strip()
    )

    await state.set_state(AddMovie.video)

    await message.answer(
        "🎥 Endi kino videosini shu yerga yuboring."
    )


@dp.message(AddMovie.video, F.video)
async def add_movie_video(message: types.Message, state: FSMContext):

    await state.update_data(
        file_id=message.video.file_id
    )

    await state.set_state(AddMovie.prime)

    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="⭐ Prime"),
                KeyboardButton(text="🎬 Oddiy")
            ]
        ],
        resize_keyboard=True
    )

    await message.answer(
        "Bu kino Prime uchunmi?",
        reply_markup=keyboard
    )


@dp.message(AddMovie.prime)
async def add_movie_prime(message: types.Message, state: FSMContext):

    if message.text not in ["⭐ Prime", "🎬 Oddiy"]:
        await message.answer(
            "⭐ Prime yoki 🎬 Oddiy tugmasini bosing."
        )
        return

    data = await state.get_data()

    prime = 1 if message.text == "⭐ Prime" else 0

    cursor.execute("""
        INSERT INTO movies
        (code, title, file_id, prime)
        VALUES (?, ?, ?, ?)
    """, (
        data["code"],
        data["title"],
        data["file_id"],
        prime
    ))

    db.commit()

    await state.clear()

    await message.answer(
        "✅ <b>Kino muvaffaqiyatli qo'shildi!</b>\n\n"
        f"🎬 Nomi: <b>{data['title']}</b>\n"
        f"🆔 Kodi: <code>{data['code']}</code>\n"
        f"⭐ Prime: {'Ha' if prime else 'Yo‘q'}",
        parse_mode="HTML",
        reply_markup=main_menu(message.from_user)
    )


# =========================================================
# ADMIN KINO O'CHIRISH
# =========================================================

@dp.message(F.text == "🗑 Kino o'chirish")
async def delete_movie(message: types.Message):

    if not is_admin(message.from_user):
        return

    await message.answer(
        "🗑 O'chiriladigan kino kodini yuboring.\n"
        "Masalan: <code>231</code>",
        parse_mode="HTML"
    )


@dp.message(F.text.regexp(r"^\d{1,6}$"))
async def delete_or_find(message: types.Message):

    if not is_admin(message.from_user):
        return

    code = message.text.strip()

    cursor.execute(
        "SELECT title FROM movies WHERE code = ?",
        (code,)
    )

    result = cursor.fetchone()

    if not result:
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ O'chirish",
                    callback_data=f"delete_{code}"
                )
            ]
        ]
    )

    await message.answer(
        f"🎬 <b>{result[0]}</b>\n\n"
        f"🆔 Kod: <code>{code}</code>",
        parse_mode="HTML",
        reply_markup=keyboard
    )


@dp.callback_query(F.data.startswith("delete_"))
async def delete_movie_confirm(callback: types.CallbackQuery):

    if not is_admin(callback.from_user):
        await callback.answer(
            "❌ Siz admin emassiz.",
            show_alert=True
        )
        return

    code = callback.data.replace("delete_", "")

    cursor.execute(
        "DELETE FROM movies WHERE code = ?",
        (code,)
    )

    db.commit()

    await callback.message.edit_text(
        f"✅ Kino <code>{code}</code> o'chirildi.",
        parse_mode="HTML"
    )

    await callback.answer()


# =========================================================
# ADMIN STATISTIKA
# =========================================================

@dp.message(F.text == "📊 Statistika")
async def statistics(message: types.Message):

    if not is_admin(message.from_user):
        return

    cursor.execute("SELECT COUNT(*) FROM users")
    users = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM movies")
    movies = cursor.fetchone()[0]

    cursor.execute(
        "SELECT COUNT(*) FROM users WHERE prime_until IS NOT NULL"
    )
    prime_users = cursor.fetchone()[0]

    await message.answer(
        "📊 <b>Statistika</b>\n\n"
        f"👥 Foydalanuvchilar: <b>{users}</b>\n"
        f"🎬 Kinolar: <b>{movies}</b>\n"
        f"⭐ Prime foydalanuvchilar: <b>{prime_users}</b>",
        parse_mode="HTML"
    )


# =========================================================
# ADMIN KINOLAR
# =========================================================

@dp.message(F.text == "📚 Kinolar")
async def admin_movies(message: types.Message):

    if not is_admin(message.from_user):
        return

    cursor.execute("""
        SELECT code, title, prime
        FROM movies
        ORDER BY rowid DESC
    """)

    movies = cursor.fetchall()

    if not movies:
        await message.answer("📚 Kino bazasi bo'sh.")
        return

    result = "📚 <b>Barcha kinolar:</b>\n\n"

    for code, title, prime in movies:

        result += (
            f"{'⭐' if prime else '🎬'} "
            f"<b>{title}</b> — <code>{code}</code>\n"
        )

    await message.answer(
        result,
        parse_mode="HTML"
    )


# =========================================================
# ASOSIY MENYU
# =========================================================

@dp.message(F.text == "🏠 Asosiy menyu")
async def home(message: types.Message):

    await message.answer(
        "🏠 Asosiy menyu",
        reply_markup=main_menu(message.from_user)
    )


# =========================================================
# RENDER HTTP SERVER
# =========================================================

async def health(request):

    return web.Response(
        text="KinoCinema bot ishlayapti!"
    )


async def start_web_server():

    app = web.Application()

    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)

    await runner.setup()

    port = int(
        os.getenv("PORT", "10000")
    )

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port
    )

    await site.start()

    print(
        f"HTTP server ishga tushdi: {port}"
    )

    return runner


# =========================================================
# MAIN
# =========================================================

async def main():

    print("🎬 KinoCinema bot ishga tushmoqda...")

    runner = await start_web_server()

    try:

        await dp.start_polling(bot)

    finally:

        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
