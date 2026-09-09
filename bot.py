import os
import asyncio
import sqlite3
import secrets
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
BOT_USERNAME = "kinocinemauz_bot"

# FAQAT SHU AKKAUNT bosh admin:
OWNER_USERNAME = "omono_v"

DB_NAME = "kino_bot.db"

if not TOKEN:
    raise RuntimeError("BOT_TOKEN topilmadi!")


bot = Bot(TOKEN)
dp = Dispatcher()

# =========================================================
# DATABASE
# =========================================================

db = sqlite3.connect(DB_NAME, check_same_thread=False)
db.row_factory = sqlite3.Row


def init_db():
    cur = db.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            prime_until TEXT,
            ref_admin_id INTEGER
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            name TEXT DEFAULT '',
            referral_code TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS movies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            file_id TEXT NOT NULL,
            prime INTEGER DEFAULT 0,
            views INTEGER DEFAULT 0,
            added_by INTEGER
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT DEFAULT '',
            ref_admin_id INTEGER,
            plan TEXT NOT NULL,
            days INTEGER NOT NULL,
            price INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT DEFAULT '',
            text TEXT,
            ref_admin_id INTEGER,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            chat_id TEXT NOT NULL
        )
    """)

    try:
        cur.execute("ALTER TABLE users ADD COLUMN ref_admin_id INTEGER")
    except sqlite3.OperationalError:
        pass

    db.commit()


init_db()


# Default mandatory channel.
# Owner can remove it later from Admin panel -> Kanal o'chirish.
def ensure_default_channel():
    if not db.execute("SELECT 1 FROM channels LIMIT 1").fetchone():
        db.execute("""
            INSERT OR IGNORE INTO channels(username, title, chat_id)
            VALUES (?, ?, ?)
        """, ("@uz_kinocinema", "KinoCinema", "@uz_kinocinema"))
        db.commit()


ensure_default_channel()


# =========================================================
# YORDAMCHI
# =========================================================

def get_setting(key, default=""):
    row = db.execute(
        "SELECT value FROM settings WHERE key = ?",
        (key,)
    ).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    db.execute("""
        INSERT INTO settings(key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
    """, (key, str(value)))
    db.commit()


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def money(n):
    return f"{int(n):,}".replace(",", " ")


def owner_id():
    value = get_setting("owner_id")
    try:
        return int(value)
    except Exception:
        return 0


def is_admin_id(user_id):
    if owner_id() and user_id == owner_id():
        return True
    row = db.execute(
        "SELECT user_id FROM admins WHERE user_id = ?",
        (user_id,)
    ).fetchone()
    return bool(row)


def ensure_owner(user):
    if user.username and user.username.lower() == OWNER_USERNAME.lower():
        set_setting("owner_id", user.id)
        db.execute("""
            INSERT INTO admins(user_id, username, name, referral_code, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                name=excluded.name
        """, (
            user.id,
            user.username or "",
            user.full_name or "",
            "OWNER",
            now_iso()
        ))
        db.commit()
        return True
    return False


def save_user(message, ref_admin_id=None):
    user = message.from_user

    old = db.execute(
        "SELECT ref_admin_id FROM users WHERE user_id = ?",
        (user.id,)
    ).fetchone()

    final_ref = old["ref_admin_id"] if old and old["ref_admin_id"] else ref_admin_id

    db.execute("""
        INSERT INTO users(user_id, username, prime_until, ref_admin_id)
        VALUES (?, ?, NULL, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            ref_admin_id=COALESCE(users.ref_admin_id, excluded.ref_admin_id)
    """, (
        user.id,
        user.username or "",
        final_ref
    ))
    db.commit()


def user_ref_admin_id(user_id):
    row = db.execute(
        "SELECT ref_admin_id FROM users WHERE user_id = ?",
        (user_id,)
    ).fetchone()
    return row["ref_admin_id"] if row else None


def admin_row(admin_id):
    return db.execute(
        "SELECT * FROM admins WHERE user_id = ?",
        (admin_id,)
    ).fetchone()


def admin_card(admin_id):
    card = get_setting(f"card_{admin_id}")
    owner = get_setting(f"card_owner_{admin_id}")
    return card, owner


def set_admin_card(admin_id, card, owner):
    set_setting(f"card_{admin_id}", card)
    set_setting(f"card_owner_{admin_id}", owner)


def delete_admin_card(admin_id):
    db.execute(
        "DELETE FROM settings WHERE key IN (?, ?)",
        (f"card_{admin_id}", f"card_owner_{admin_id}")
    )
    db.commit()


def make_referral_code():
    while True:
        code = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8]
        if not db.execute(
            "SELECT 1 FROM admins WHERE referral_code = ?",
            (code,)
        ).fetchone():
            return code


def referral_link(admin_id):
    row = admin_row(admin_id)
    if not row:
        return ""
    return f"https://t.me/{BOT_USERNAME}?start=admin_{row['referral_code']}"


# =========================================================
# PRIME
# =========================================================

PLANS = {
    "7": ("7 kun", 7, 7000),
    "30": ("1 oy", 30, 20000),
    "90": ("3 oy", 90, 50000),
    "36500": ("Umrbod", 36500, 150000),
}


def is_prime(user_id):
    row = db.execute(
        "SELECT prime_until FROM users WHERE user_id = ?",
        (user_id,)
    ).fetchone()

    if not row or not row["prime_until"]:
        return False

    try:
        return datetime.fromisoformat(row["prime_until"]) > datetime.now()
    except Exception:
        return False


def activate_prime(user_id, days):
    row = db.execute(
        "SELECT prime_until FROM users WHERE user_id = ?",
        (user_id,)
    ).fetchone()

    current = datetime.now()

    if row and row["prime_until"]:
        try:
            old = datetime.fromisoformat(row["prime_until"])
            if old > current:
                current = old
        except Exception:
            pass

    until = current + timedelta(days=days)

    db.execute(
        "UPDATE users SET prime_until = ? WHERE user_id = ?",
        (until.isoformat(timespec="seconds"), user_id)
    )
    db.commit()
    return until


# =========================================================
# MAJBURIY KANAL
# =========================================================

def get_channels():
    return db.execute(
        "SELECT * FROM channels ORDER BY id ASC"
    ).fetchall()


async def check_subscription(user_id):
    channels = get_channels()

    # If owner has removed all channels, subscription is not required.
    if not channels:
        return True

    for channel in channels:
        try:
            member = await bot.get_chat_member(
                chat_id=channel["chat_id"],
                user_id=user_id
            )

            status = member.status

            # These statuses mean the user is NOT subscribed.
            if status in ("left", "kicked"):
                return False

            # Restricted member can still be subscribed if is_member=True.
            if status == "restricted":
                if not getattr(member, "is_member", False):
                    return False

        except Exception as e:
            print(
                f"SUB CHECK ERROR | channel={channel['chat_id']} "
                f"user={user_id}: {e}"
            )
            # If Telegram cannot verify the membership, do NOT unlock the bot.
            return False

    return True


def subscription_keyboard():
    buttons = []

    for ch in get_channels():
        username = ch["username"]

        if username.startswith("@"):
            username = username[1:]

        buttons.append([
            InlineKeyboardButton(
                text=f"📢 {ch['title']}",
                url=f"https://t.me/{username}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            text="✅ Obuna bo'ldim",
            callback_data="check_sub"
        )
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def require_subscription(message):
    # Owner/admins are allowed to use the bot without subscribing.
    if is_admin_id(message.from_user.id):
        return True

    if await check_subscription(message.from_user.id):
        return True

    # IMPORTANT:
    # Remove the old main keyboard so an unsubscribed user cannot keep
    # seeing/using the previous menu.
    from aiogram.types import ReplyKeyboardRemove

    await message.answer(
        "🔐 <b>Avval kanalga obuna bo'ling.</b>\n\n"
        "Botdan foydalanish uchun quyidagi kanalga obuna bo'ling "
        "va «✅ Obuna bo'ldim» tugmasini bosing.",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="HTML"
    )

    await message.answer(
        "📢 <b>Majburiy kanal:</b>",
        reply_markup=subscription_keyboard(),
        parse_mode="HTML"
    )

    return False


# =========================================================
# MENYULAR
# =========================================================

def main_menu(user_id):
    rows = [
        [
            KeyboardButton(text="🔎 Kino qidirish"),
            KeyboardButton(text="⭐ Prime status")
        ],
        [KeyboardButton(text="📚 Kinolar ro'yxati")],
        [KeyboardButton(text="📸 Instagramga qaytish")],
        [KeyboardButton(text="🎬 Kino buyurtma qilish")],
        [KeyboardButton(text="🤝 Reklama & Bot olish")],
    ]

    if is_admin_id(user_id):
        rows.append([
            KeyboardButton(text="👨‍💻 Admin panel")
        ])

    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True
    )


def admin_menu(user_id):
    rows = [
        [
            KeyboardButton(text="➕ Kino qo'shish"),
            KeyboardButton(text="🗑 Kino o'chirish")
        ],
        [
            KeyboardButton(text="💳 Mening kartam"),
            KeyboardButton(text="📊 Mening statistikam")
        ],
        [
            KeyboardButton(text="🔗 Mening silkam")
        ],
    ]

    if user_id == owner_id():
        rows.extend([
            [KeyboardButton(text="👥 Adminlar")],
            [
                KeyboardButton(text="📢 Kanal qo'shish"),
                KeyboardButton(text="🗑 Kanal o'chirish")
            ],
            [
                KeyboardButton(text="📋 Kanallar"),
                KeyboardButton(text="📊 Umumiy statistika")
            ],
        ])

    rows.append([
        KeyboardButton(text="🏠 Asosiy menyu")
    ])

    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True
    )


def cancel_kb(admin=False):
    back = "⬅️ Admin panel" if admin else "⬅️ Asosiy menyu"

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=back),
                KeyboardButton(text="❌ Bekor qilish")
            ]
        ],
        resize_keyboard=True
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


class ChannelAdd(StatesGroup):
    value = State()


class ChannelDelete(StatesGroup):
    value = State()


class AdminAdd(StatesGroup):
    user = State()


class AdminDelete(StatesGroup):
    user = State()


class SearchMovie(StatesGroup):
    query = State()


class MovieRequest(StatesGroup):
    text = State()


class Payment(StatesGroup):
    screenshot = State()


# =========================================================
# NAVIGATION — BU HANDLERLAR FSM HANDLERLARIDAN OLDIN
# =========================================================

@dp.message(F.text == "🏠 Asosiy menyu")
async def home(message: Message, state: FSMContext):
    await state.clear()

    await message.answer(
        "🏠 <b>Asosiy menyu</b>",
        reply_markup=main_menu(message.from_user.id),
        parse_mode="HTML"
    )


@dp.message(F.text == "⬅️ Admin panel")
async def back_admin(message: Message, state: FSMContext):
    await state.clear()

    if not is_admin_id(message.from_user.id):
        await message.answer(
            "🏠 Asosiy menyu:",
            reply_markup=main_menu(message.from_user.id)
        )
        return

    await message.answer(
        "👨‍💻 <b>Admin panel</b>",
        reply_markup=admin_menu(message.from_user.id),
        parse_mode="HTML"
    )


@dp.message(F.text == "⬅️ Asosiy menyu")
async def back_home(message: Message, state: FSMContext):
    await state.clear()

    await message.answer(
        "🏠 <b>Asosiy menyu</b>",
        reply_markup=main_menu(message.from_user.id),
        parse_mode="HTML"
    )


@dp.message(F.text == "❌ Bekor qilish")
async def cancel_all(message: Message, state: FSMContext):
    await state.clear()

    if is_admin_id(message.from_user.id):
        await message.answer(
            "❌ Jarayon bekor qilindi.",
            reply_markup=admin_menu(message.from_user.id)
        )
    else:
        await message.answer(
            "❌ Jarayon bekor qilindi.",
            reply_markup=main_menu(message.from_user.id)
        )


# =========================================================
# START + REFERRAL
# =========================================================

@dp.message(Command("start"))
async def start_handler(message: Message, state: FSMContext):
    await state.clear()

    ensure_owner(message.from_user)

    if (
        message.from_user.username
        and message.from_user.username.lower() == OWNER_USERNAME.lower()
    ):
        set_setting("owner_id", message.from_user.id)

    ref_admin_id = None
    args = (message.text or "").split(maxsplit=1)

    if len(args) > 1:
        arg = args[1].strip()

        if arg.startswith("admin_"):
            code = arg[6:]

            row = db.execute(
                "SELECT user_id FROM admins WHERE referral_code = ?",
                (code,)
            ).fetchone()

            if row:
                ref_admin_id = row["user_id"]

    save_user(message, ref_admin_id)

    if not is_admin_id(message.from_user.id):
        if not await require_subscription(message):
            return

    await message.answer(
        "🎬 <b>KinoCinema</b> botiga xush kelibsiz!\n\n"
        "Kerakli bo'limni tanlang:",
        reply_markup=main_menu(message.from_user.id),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "check_sub")
async def check_sub_callback(callback: CallbackQuery):
    if is_admin_id(callback.from_user.id):
        await callback.message.answer(
            "🏠 Asosiy menyu:",
            reply_markup=main_menu(callback.from_user.id)
        )
        await callback.answer("Tasdiqlandi!")
        return

    if await check_subscription(callback.from_user.id):
        await callback.message.answer(
            "✅ <b>Obuna tasdiqlandi!</b>\n\n"
            "Endi botdan foydalanishingiz mumkin.",
            reply_markup=main_menu(callback.from_user.id),
            parse_mode="HTML"
        )
        await callback.answer("Obuna tasdiqlandi!")
    else:
        await callback.answer(
            "❌ Hali kanalga obuna bo'lmagansiz.",
            show_alert=True
        )


# =========================================================
# ADMIN PANEL
# =========================================================

@dp.message(F.text == "👨‍💻 Admin panel")
async def admin_panel(message: Message):
    if not is_admin_id(message.from_user.id):
        return

    ensure_owner(message.from_user)

    await message.answer(
        "👨‍💻 <b>Admin panel</b>\n\n"
        "Kerakli bo'limni tanlang:",
        reply_markup=admin_menu(message.from_user.id),
        parse_mode="HTML"
    )


# =========================================================
# KARTA
# =========================================================

def card_inline():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="➕/🔄 Kartani kiritish",
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
                callback_data="card_del"
            )
        ],
        [
            InlineKeyboardButton(
                text="⬅️ Admin panel",
                callback_data="admin_panel_cb"
            )
        ]
    ])


@dp.message(F.text == "💳 Mening kartam")
async def my_card(message: Message):
    if not is_admin_id(message.from_user.id):
        return

    card, owner = admin_card(message.from_user.id)

    if card:
        text = (
            "💳 <b>Sizning kartangiz</b>\n\n"
            f"💳 <code>{card}</code>\n"
            f"👤 {owner}\n\n"
            "Bu karta faqat sizning referral silkangizdan "
            "kelgan foydalanuvchilarga ko'rsatiladi."
        )
    else:
        text = (
            "💳 <b>Sizning kartangiz</b>\n\n"
            "❌ Hali karta kiritilmagan.\n\n"
            "➕ Kartani kiriting."
        )

    await message.answer(
        text,
        reply_markup=card_inline(),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "card_add")
async def card_add_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin_id(callback.from_user.id):
        await callback.answer("Ruxsat yo'q.", show_alert=True)
        return

    await state.set_state(CardAdd.number)

    await callback.message.answer(
        "💳 <b>Karta raqamini yuboring.</b>\n\n"
        "Masalan:\n"
        "<code>9860600435412504</code>",
        reply_markup=cancel_kb(admin=True),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.message(StateFilter(CardAdd.number))
async def card_number(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        await state.clear()
        return

    value = message.text.strip().replace(" ", "").replace("-", "")

    if not value.isdigit() or not (12 <= len(value) <= 19):
        await message.answer(
            "❌ Karta raqami noto'g'ri.\n"
            "Faqat 12–19 ta raqam yuboring.",
            reply_markup=cancel_kb(admin=True)
        )
        return

    await state.update_data(card=value)
    await state.set_state(CardAdd.owner)

    await message.answer(
        "👤 <b>Karta egasining ism-familiyasini yuboring.</b>",
        reply_markup=cancel_kb(admin=True),
        parse_mode="HTML"
    )


@dp.message(StateFilter(CardAdd.owner))
async def card_owner(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        await state.clear()
        return

    owner = message.text.strip()

    if len(owner) < 2:
        await message.answer(
            "❌ Ism-familiya noto'g'ri.",
            reply_markup=cancel_kb(admin=True)
        )
        return

    data = await state.get_data()

    set_admin_card(
        message.from_user.id,
        data["card"],
        owner
    )

    await state.clear()

    await message.answer(
        "✅ <b>Karta muvaffaqiyatli saqlandi!</b>\n\n"
        f"💳 <code>{data['card']}</code>\n"
        f"👤 {owner}\n\n"
        "Bu faqat sizning kartangiz. Boshqa adminlarning "
        "kartasiga ta'sir qilmaydi.",
        reply_markup=admin_menu(message.from_user.id),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "card_view")
async def card_view(callback: CallbackQuery):
    if not is_admin_id(callback.from_user.id):
        return

    card, owner = admin_card(callback.from_user.id)

    if not card:
        text = "❌ Sizda hali karta kiritilmagan."
    else:
        text = (
            "💳 <b>Sizning kartangiz</b>\n\n"
            f"💳 <code>{card}</code>\n"
            f"👤 {owner}"
        )

    await callback.message.answer(
        text,
        reply_markup=card_inline(),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "card_del")
async def card_del(callback: CallbackQuery):
    if not is_admin_id(callback.from_user.id):
        return

    delete_admin_card(callback.from_user.id)

    await callback.message.answer(
        "🗑 <b>Sizning kartangiz o'chirildi.</b>",
        reply_markup=card_inline(),
        parse_mode="HTML"
    )
    await callback.answer()


# =========================================================
# REFERRAL LINK + STATS
# =========================================================

@dp.message(F.text == "🔗 Mening silkam")
async def my_link(message: Message):
    if not is_admin_id(message.from_user.id):
        return

    link = referral_link(message.from_user.id)

    await message.answer(
        "🔗 <b>Sizning shaxsiy referral silkangiz:</b>\n\n"
        f"<code>{link}</code>\n\n"
        "👤 Shu silka orqali kirgan foydalanuvchilar "
        "sizga biriktiriladi.\n"
        "💳 Ularning Prime to'lovlari sizning kartangizga "
        "to'lanadi va statistika sizniki bo'ladi.",
        parse_mode="HTML"
    )


@dp.message(F.text == "📊 Mening statistikam")
async def my_stats(message: Message):
    if not is_admin_id(message.from_user.id):
        return

    aid = message.from_user.id

    users = db.execute("""
        SELECT COUNT(*) AS c
        FROM users
        WHERE ref_admin_id = ?
    """, (aid,)).fetchone()["c"]

    paid = db.execute("""
        SELECT COUNT(*) AS c
        FROM orders
        WHERE ref_admin_id = ?
          AND status = 'approved'
    """, (aid,)).fetchone()["c"]

    revenue = db.execute("""
        SELECT COALESCE(SUM(price), 0) AS s
        FROM orders
        WHERE ref_admin_id = ?
          AND status = 'approved'
    """, (aid,)).fetchone()["s"]

    pending = db.execute("""
        SELECT COUNT(*) AS c
        FROM orders
        WHERE ref_admin_id = ?
          AND status = 'pending'
    """, (aid,)).fetchone()["c"]

    await message.answer(
        "📊 <b>Sizning statistkangiz</b>\n\n"
        f"👥 Silkangizdan kirganlar: <b>{users}</b>\n"
        f"💳 Tasdiqlangan to'lovlar: <b>{paid}</b>\n"
        f"💰 Jami tushum: <b>{money(revenue)} so'm</b>\n"
        f"⏳ Kutilayotgan to'lovlar: <b>{pending}</b>",
        parse_mode="HTML"
    )


# =========================================================
# OWNER: ADMINLAR
# =========================================================

@dp.message(F.text == "👥 Adminlar")
async def admins_menu(message: Message):
    if message.from_user.id != owner_id():
        return

    rows = db.execute(
        "SELECT * FROM admins ORDER BY created_at ASC"
    ).fetchall()

    text = "👥 <b>Adminlar</b>\n\n"

    for i, row in enumerate(rows, 1):
        role = "👑 Bosh admin" if row["user_id"] == owner_id() else "🛡 Admin"
        text += (
            f"{i}. {role}\n"
            f"👤 {row['name'] or '-'}\n"
            f"🔗 @{row['username'] or '-'}\n"
            f"🆔 <code>{row['user_id']}</code>\n\n"
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Admin tayinlash", callback_data="admin_add")],
        [InlineKeyboardButton(text="🗑 Adminni o'chirish", callback_data="admin_del")],
        [InlineKeyboardButton(text="⬅️ Admin panel", callback_data="admin_panel_cb")]
    ])

    await message.answer(
        text,
        reply_markup=kb,
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "admin_add")
async def admin_add_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != owner_id():
        await callback.answer("Faqat bosh admin.", show_alert=True)
        return

    await state.set_state(AdminAdd.user)

    await callback.message.answer(
        "➕ <b>Admin tayinlash</b>\n\n"
        "Sardor botga avval /start yuborsin.\n\n"
        "Keyin uning @username'ini yoki Telegram ID'sini yuboring.\n\n"
        "Masalan:\n"
        "<code>@sardor</code>",
        reply_markup=cancel_kb(admin=True),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.message(StateFilter(AdminAdd.user))
async def admin_add_received(message: Message, state: FSMContext):
    if message.from_user.id != owner_id():
        await state.clear()
        return

    value = message.text.strip()
    target = None

    if value.startswith("@"):
        row = db.execute(
            "SELECT user_id FROM users WHERE LOWER(username) = LOWER(?)",
            (value[1:],)
        ).fetchone()

        if row:
            target = row["user_id"]
    else:
        try:
            target = int(value)
        except Exception:
            pass

    if not target:
        await message.answer(
            "❌ Foydalanuvchi topilmadi.\n\n"
            "U botga /start yuborganini tekshiring.",
            reply_markup=cancel_kb(admin=True)
        )
        return

    if target == owner_id():
        await message.answer(
            "❌ Siz allaqachon bosh adminsiz.",
            reply_markup=cancel_kb(admin=True)
        )
        return

    user = db.execute(
        "SELECT * FROM users WHERE user_id = ?",
        (target,)
    ).fetchone()

    username = user["username"] if user else ""
    code = make_referral_code()

    db.execute("""
        INSERT INTO admins(
            user_id, username, name, referral_code, created_at
        )
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            name=excluded.name
    """, (
        target,
        username,
        username or str(target),
        code,
        now_iso()
    ))
    db.commit()

    link = referral_link(target)

    await state.clear()

    await message.answer(
        "✅ <b>Admin tayinlandi!</b>\n\n"
        f"🆔 ID: <code>{target}</code>\n"
        f"👤 @{username or '-'}\n\n"
        f"🔗 Shaxsiy silka:\n<code>{link}</code>\n\n"
        "Endi u o'z panelidan o'z kartasini kiritadi. "
        "Uning kartasi sizning kartangizni o'zgartirmaydi.",
        reply_markup=admin_menu(owner_id()),
        parse_mode="HTML"
    )

    try:
        await bot.send_message(
            target,
            "🎉 <b>Siz KinoCinema botiga admin tayinlandingiz!</b>\n\n"
            "👨‍💻 Admin panelga kirib, o'z kartangizni kiriting.\n\n"
            f"🔗 Sizning referral silkangiz:\n<code>{link}</code>",
            parse_mode="HTML"
        )
    except Exception as e:
        print("YANGI ADMIN XABAR:", e)


@dp.callback_query(F.data == "admin_del")
async def admin_del_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != owner_id():
        await callback.answer("Faqat bosh admin.", show_alert=True)
        return

    await state.set_state(AdminDelete.user)

    await callback.message.answer(
        "🗑 <b>Adminni o'chirish</b>\n\n"
        "O'chiriladigan adminning @username yoki ID'sini yuboring.",
        reply_markup=cancel_kb(admin=True),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.message(StateFilter(AdminDelete.user))
async def admin_del_received(message: Message, state: FSMContext):
    if message.from_user.id != owner_id():
        await state.clear()
        return

    value = message.text.strip()
    target = None

    if value.startswith("@"):
        row = db.execute(
            "SELECT user_id FROM admins WHERE LOWER(username) = LOWER(?)",
            (value[1:],)
        ).fetchone()

        if row:
            target = row["user_id"]
    else:
        try:
            target = int(value)
        except Exception:
            pass

    if not target:
        await message.answer(
            "❌ Admin topilmadi.",
            reply_markup=cancel_kb(admin=True)
        )
        return

    if target == owner_id():
        await message.answer(
            "❌ Bosh adminni o'chirib bo'lmaydi.",
            reply_markup=cancel_kb(admin=True)
        )
        return

    db.execute(
        "DELETE FROM admins WHERE user_id = ?",
        (target,)
    )
    db.commit()

    await state.clear()

    await message.answer(
        "🗑 <b>Admin o'chirildi.</b>\n\n"
        f"ID: <code>{target}</code>",
        reply_markup=admin_menu(owner_id()),
        parse_mode="HTML"
    )

    try:
        await bot.send_message(
            target,
            "⚠️ Sizning KinoCinema admin huquqingiz olib tashlandi."
        )
    except Exception:
        pass


# =========================================================
# KANALLAR — FAQAT BOSH ADMIN
# =========================================================

@dp.message(F.text == "📢 Kanal qo'shish")
async def channel_add_start(message: Message, state: FSMContext):
    if message.from_user.id != owner_id():
        return

    await state.set_state(ChannelAdd.value)

    await message.answer(
        "📢 <b>Kanal qo'shish</b>\n\n"
        "Kanal username'ini yoki ID'sini yuboring.\n\n"
        "Masalan:\n"
        "<code>@uz_kinocinema</code>\n"
        "yoki\n"
        "<code>-1001234567890</code>\n\n"
        "⚠️ Bot kanalga administrator bo'lishi kerak.",
        reply_markup=cancel_kb(admin=True),
        parse_mode="HTML"
    )


@dp.message(StateFilter(ChannelAdd.value))
async def channel_add_received(message: Message, state: FSMContext):
    if message.from_user.id != owner_id():
        await state.clear()
        return

    value = message.text.strip()

    if not value:
        await message.answer(
            "❌ Qiymat bo'sh.",
            reply_markup=cancel_kb(admin=True)
        )
        return

    lookup = value
    saved_username = value

    if not value.startswith("@") and not value.startswith("-100"):
        lookup = "@" + value
        saved_username = "@" + value

    try:
        chat = await bot.get_chat(lookup)
    except Exception as e:
        print("KANAL TOPISH:", e)
        await message.answer(
            "❌ Kanal topilmadi.\n\n"
            "Username/ID ni tekshiring.",
            reply_markup=cancel_kb(admin=True)
        )
        return

    if chat.type != "channel":
        await message.answer(
            "❌ Bu Telegram kanali emas.",
            reply_markup=cancel_kb(admin=True)
        )
        return

    me = await bot.get_me()

    try:
        member = await bot.get_chat_member(chat.id, me.id)
        if member.status not in ("administrator", "creator"):
            raise RuntimeError("Bot admin emas")
    except Exception:
        await message.answer(
            "❌ Bot bu kanalda administrator emas.\n\n"
            "Avval botni kanalga admin qilib qo'ying.",
            reply_markup=cancel_kb(admin=True)
        )
        return

    if not saved_username.startswith("@"):
        saved_username = (
            f"@{chat.username}"
            if chat.username
            else str(chat.id)
        )

    try:
        db.execute("""
            INSERT INTO channels(username, title, chat_id)
            VALUES (?, ?, ?)
        """, (
            saved_username,
            chat.title or saved_username,
            str(chat.id)
        ))
        db.commit()
    except sqlite3.IntegrityError:
        await state.clear()
        await message.answer(
            "❌ Bu kanal allaqachon qo'shilgan.",
            reply_markup=admin_menu(owner_id())
        )
        return

    await state.clear()

    await message.answer(
        "✅ <b>Kanal qo'shildi!</b>\n\n"
        f"📢 {chat.title}\n"
        f"🔗 {saved_username}\n"
        f"🆔 <code>{chat.id}</code>",
        reply_markup=admin_menu(owner_id()),
        parse_mode="HTML"
    )


@dp.message(F.text == "🗑 Kanal o'chirish")
async def channel_del_start(message: Message, state: FSMContext):
    if message.from_user.id != owner_id():
        return

    channels = get_channels()

    if not channels:
        await message.answer(
            "📢 Hozircha kanal yo'q.",
            reply_markup=admin_menu(owner_id())
        )
        return

    text = "🗑 <b>Kanal o'chirish</b>\n\n"

    for ch in channels:
        text += (
            f"📢 {ch['title']}\n"
            f"🔗 {ch['username']}\n"
            f"🆔 <code>{ch['chat_id']}</code>\n\n"
        )

    await state.set_state(ChannelDelete.value)

    await message.answer(
        text + "O'chiriladigan kanal username yoki ID'sini yuboring.",
        reply_markup=cancel_kb(admin=True),
        parse_mode="HTML"
    )


@dp.message(StateFilter(ChannelDelete.value))
async def channel_del_received(message: Message, state: FSMContext):
    if message.from_user.id != owner_id():
        await state.clear()
        return

    value = message.text.strip()

    row = db.execute("""
        SELECT * FROM channels
        WHERE username = ? OR chat_id = ?
    """, (value, value)).fetchone()

    if not row and not value.startswith("@"):
        row = db.execute(
            "SELECT * FROM channels WHERE username = ?",
            ("@" + value,)
        ).fetchone()

    if not row:
        await message.answer(
            "❌ Kanal topilmadi.",
            reply_markup=cancel_kb(admin=True)
        )
        return

    db.execute(
        "DELETE FROM channels WHERE id = ?",
        (row["id"],)
    )
    db.commit()

    await state.clear()

    await message.answer(
        "🗑 <b>Kanal o'chirildi.</b>\n\n"
        f"📢 {row['title']}",
        reply_markup=admin_menu(owner_id()),
        parse_mode="HTML"
    )


@dp.message(F.text == "📋 Kanallar")
async def channels_list(message: Message):
    if message.from_user.id != owner_id():
        return

    channels = get_channels()

    if not channels:
        await message.answer("📢 Majburiy kanal yo'q.")
        return

    text = "📢 <b>Majburiy kanallar</b>\n\n"

    for i, ch in enumerate(channels, 1):
        text += (
            f"{i}. <b>{ch['title']}</b>\n"
            f"🔗 {ch['username']}\n"
            f"🆔 <code>{ch['chat_id']}</code>\n\n"
        )

    await message.answer(
        text,
        parse_mode="HTML"
    )


# =========================================================
# UMUMIY STATISTIKA — FAQAT BOSH ADMIN
# =========================================================

@dp.message(F.text == "📊 Umumiy statistika")
async def global_stats(message: Message):
    if message.from_user.id != owner_id():
        return

    users = db.execute(
        "SELECT COUNT(*) AS c FROM users"
    ).fetchone()["c"]

    movies = db.execute(
        "SELECT COUNT(*) AS c FROM movies"
    ).fetchone()["c"]

    views = db.execute(
        "SELECT COALESCE(SUM(views),0) AS s FROM movies"
    ).fetchone()["s"]

    admins = db.execute(
        "SELECT COUNT(*) AS c FROM admins"
    ).fetchone()["c"]

    paid = db.execute("""
        SELECT COUNT(*) AS c
        FROM orders
        WHERE status='approved'
    """).fetchone()["c"]

    revenue = db.execute("""
        SELECT COALESCE(SUM(price),0) AS s
        FROM orders
        WHERE status='approved'
    """).fetchone()["s"]

    await message.answer(
        "📊 <b>Umumiy statistika</b>\n\n"
        f"👥 Foydalanuvchilar: <b>{users}</b>\n"
        f"🛡 Adminlar: <b>{admins}</b>\n"
        f"🎬 Kinolar: <b>{movies}</b>\n"
        f"👁 Ko'rishlar: <b>{views}</b>\n"
        f"💳 Tasdiqlangan to'lovlar: <b>{paid}</b>\n"
        f"💰 Jami tushum: <b>{money(revenue)} so'm</b>",
        parse_mode="HTML"
    )


# =========================================================
# PRIME STATUS
# =========================================================

@dp.message(F.text == "⭐ Prime status")
async def prime_status(message: Message):
    if not await require_subscription(message):
        return

    if is_prime(message.from_user.id):
        row = db.execute(
            "SELECT prime_until FROM users WHERE user_id = ?",
            (message.from_user.id,)
        ).fetchone()

        until = datetime.fromisoformat(row["prime_until"])

        await message.answer(
            "⭐ <b>Prime/VIP faol!</b>\n\n"
            f"⏳ Tugash vaqti:\n"
            f"<b>{until.strftime('%d.%m.%Y %H:%M')}</b>",
            parse_mode="HTML"
        )
        return

    await message.answer(
        "⭐ <b>Prime/VIP</b>\n\n"
        "Tarifni tanlang:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="7 kun — 7 000 so'm", callback_data="plan_7")],
            [InlineKeyboardButton(text="1 oy — 20 000 so'm", callback_data="plan_30")],
            [InlineKeyboardButton(text="3 oy — 50 000 so'm", callback_data="plan_90")],
            [InlineKeyboardButton(text="Umrbod — 150 000 so'm", callback_data="plan_36500")],
            [InlineKeyboardButton(text="❌ Yopish", callback_data="close_prime")]
        ]),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "close_prime")
async def close_prime(callback: CallbackQuery):
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


# =========================================================
# PRIME TO'LOV
# =========================================================

@dp.callback_query(F.data.startswith("plan_"))
async def choose_plan(callback: CallbackQuery):
    if not is_admin_id(callback.from_user.id):
        if not await check_subscription(callback.from_user.id):
            await callback.answer(
                "Avval kanalga obuna bo'ling.",
                show_alert=True
            )
            return

    key = callback.data.replace("plan_", "")

    if key not in PLANS:
        await callback.answer(
            "Tarif topilmadi.",
            show_alert=True
        )
        return

    name, days, price = PLANS[key]

    ref_id = user_ref_admin_id(callback.from_user.id) or owner_id()

    card, card_owner = admin_card(ref_id)

    if not card:
        await callback.message.answer(
            "❌ <b>To'lov kartasi sozlanmagan.</b>\n\n"
            "Siz kirgan referral admin hali o'z kartasini "
            "botga kiritmagan.",
            parse_mode="HTML"
        )
        await callback.answer()
        return

    cur = db.cursor()

    cur.execute("""
        INSERT INTO orders(
            user_id, username, ref_admin_id,
            plan, days, price, status, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
    """, (
        callback.from_user.id,
        callback.from_user.username or "",
        ref_id,
        name,
        days,
        price,
        now_iso()
    ))

    order_id = cur.lastrowid
    db.commit()

    await callback.message.answer(
        f"⭐ <b>Prime — {name}</b>\n\n"
        f"💰 Narxi: <b>{money(price)} so'm</b>\n\n"
        f"💳 Karta:\n<code>{card}</code>\n"
        f"👤 Karta egasi: <b>{card_owner}</b>\n\n"
        "To'lovni amalga oshirgach, "
        "«💳 To'ladim» tugmasini bosing.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="💳 To'ladim",
                callback_data=f"paid_{order_id}"
            )],
            [InlineKeyboardButton(
                text="⬅️ Orqaga",
                callback_data="prime_back"
            )]
        ]),
        parse_mode="HTML"
    )

    await callback.answer()


@dp.callback_query(F.data == "prime_back")
async def prime_back(callback: CallbackQuery):
    await callback.message.answer(
        "⭐ <b>Prime tariflari:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="7 kun — 7 000 so'm", callback_data="plan_7")],
            [InlineKeyboardButton(text="1 oy — 20 000 so'm", callback_data="plan_30")],
            [InlineKeyboardButton(text="3 oy — 50 000 so'm", callback_data="plan_90")],
            [InlineKeyboardButton(text="Umrbod — 150 000 so'm", callback_data="plan_36500")],
            [InlineKeyboardButton(text="❌ Yopish", callback_data="close_prime")]
        ]),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("paid_"))
async def paid_start(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.replace("paid_", ""))

    order = db.execute(
        "SELECT * FROM orders WHERE id = ?",
        (order_id,)
    ).fetchone()

    if not order or order["user_id"] != callback.from_user.id:
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

    await state.update_data(order_id=order_id)
    await state.set_state(Payment.screenshot)

    await callback.message.answer(
        "📸 <b>To'lov screenshotini yuboring.</b>\n\n"
        "Rasm ko'rinishida yuboring.",
        reply_markup=cancel_kb(admin=False),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.message(StateFilter(Payment.screenshot), F.photo)
async def payment_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("order_id")

    order = db.execute(
        "SELECT * FROM orders WHERE id = ?",
        (order_id,)
    ).fetchone()

    if not order:
        await state.clear()
        await message.answer("❌ Buyurtma topilmadi.")
        return

    if order["status"] != "pending":
        await state.clear()
        await message.answer(
            "❌ Bu buyurtma allaqachon ko'rib chiqilgan."
        )
        return

    target_admin = order["ref_admin_id"] or owner_id()

    if not target_admin:
        await state.clear()
        await message.answer(
            "❌ Admin topilmadi."
        )
        return

    username = (
        f"@{order['username']}"
        if order["username"]
        else "Username yo'q"
    )

    admin = admin_row(target_admin)

    caption = (
        "🧾 <b>Yangi PRIME to'lov!</b>\n\n"
        f"👤 User: {username}\n"
        f"🆔 ID: <code>{order['user_id']}</code>\n"
        f"🛡 Admin: @{admin['username'] if admin else '-'}\n"
        f"📦 Tarif: <b>{order['plan']}</b>\n"
        f"⏳ Muddat: <b>{order['days']} kun</b>\n"
        f"💰 Narx: <b>{money(order['price'])} so'm</b>\n\n"
        "📸 To'lov skrinshoti:"
    )

    try:
        await bot.send_photo(
            chat_id=target_admin,
            photo=message.photo[-1].file_id,
            caption=caption,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
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
            ]),
            parse_mode="HTML"
        )
    except Exception as e:
        print("PAYMENT SEND:", e)
        await state.clear()
        await message.answer(
            "❌ Screenshot adminga yuborilmadi.\n\n"
            "Admin botga /start yuborganini tekshiring."
        )
        return

    await state.clear()

    await message.answer(
        "⏳ <b>To'lovingiz tekshirilmoqda.</b>\n\n"
        "Tasdiqlangach Prime/VIP avtomatik ochiladi.",
        reply_markup=main_menu(message.from_user.id),
        parse_mode="HTML"
    )


@dp.message(StateFilter(Payment.screenshot))
async def payment_wrong(message: Message):
    await message.answer(
        "📸 Screenshotni rasm sifatida yuboring.",
        reply_markup=cancel_kb()
    )


@dp.callback_query(F.data.startswith("approve_"))
async def approve_payment(callback: CallbackQuery):
    order_id = int(callback.data.replace("approve_", ""))

    order = db.execute(
        "SELECT * FROM orders WHERE id = ?",
        (order_id,)
    ).fetchone()

    if not order:
        await callback.answer(
            "Buyurtma topilmadi.",
            show_alert=True
        )
        return

    if not is_admin_id(callback.from_user.id):
        await callback.answer(
            "Ruxsat yo'q.",
            show_alert=True
        )
        return

    if (
        callback.from_user.id != owner_id()
        and callback.from_user.id != order["ref_admin_id"]
    ):
        await callback.answer(
            "Bu to'lov sizga tegishli emas.",
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

    db.execute(
        "UPDATE orders SET status='approved' WHERE id=?",
        (order_id,)
    )
    db.commit()

    try:
        await bot.send_message(
            order["user_id"],
            "🎉 <b>To'lov tasdiqlandi!</b>\n\n"
            f"⭐ Prime/VIP: <b>{order['plan']}</b>\n"
            f"⏳ Muddat: <b>{order['days']} kun</b>\n"
            f"📅 Tugash: <b>{until.strftime('%d.%m.%Y %H:%M')}</b>\n\n"
            "✅ Prime/VIP ochildi!",
            parse_mode="HTML"
        )
    except Exception as e:
        print("APPROVE USER:", e)

    try:
        await callback.message.edit_caption(
            caption=(
                (callback.message.caption or "")
                + "\n\n✅ <b>TASDIQLANDI</b>"
            ),
            parse_mode="HTML"
        )
    except Exception:
        pass

    await callback.answer("✅ Prime/VIP ochildi!")


@dp.callback_query(F.data.startswith("reject_"))
async def reject_payment(callback: CallbackQuery):
    order_id = int(callback.data.replace("reject_", ""))

    order = db.execute(
        "SELECT * FROM orders WHERE id = ?",
        (order_id,)
    ).fetchone()

    if not order:
        await callback.answer(
            "Buyurtma topilmadi.",
            show_alert=True
        )
        return

    if not is_admin_id(callback.from_user.id):
        await callback.answer(
            "Ruxsat yo'q.",
            show_alert=True
        )
        return

    if (
        callback.from_user.id != owner_id()
        and callback.from_user.id != order["ref_admin_id"]
    ):
        await callback.answer(
            "Bu to'lov sizga tegishli emas.",
            show_alert=True
        )
        return

    if order["status"] != "pending":
        await callback.answer(
            "Bu to'lov allaqachon ko'rib chiqilgan.",
            show_alert=True
        )
        return

    db.execute(
        "UPDATE orders SET status='rejected' WHERE id=?",
        (order_id,)
    )
    db.commit()

    try:
        await bot.send_message(
            order["user_id"],
            "❌ <b>To'lov tasdiqlanmadi.</b>\n\n"
            "Prime/VIP ochilmadi.",
            parse_mode="HTML"
        )
    except Exception:
        pass

    try:
        await callback.message.edit_caption(
            caption=(
                (callback.message.caption or "")
                + "\n\n❌ <b>BEKOR QILINDI</b>"
            ),
            parse_mode="HTML"
        )
    except Exception:
        pass

    await callback.answer("❌ To'lov bekor qilindi.")


# =========================================================
# KINO QIDIRISH
# =========================================================

@dp.message(F.text == "🔎 Kino qidirish")
async def search_start(message: Message, state: FSMContext):
    if not await require_subscription(message):
        return

    await state.set_state(SearchMovie.query)

    await message.answer(
        "🔎 <b>Kino qidirish</b>\n\n"
        "Kino kodini yoki nomini yuboring.\n\n"
        "Masalan: <code>247</code>",
        reply_markup=cancel_kb(),
        parse_mode="HTML"
    )


@dp.message(StateFilter(SearchMovie.query))
async def search_received(message: Message, state: FSMContext):
    query = message.text.strip()
    await state.clear()

    rows = db.execute("""
        SELECT * FROM movies
        WHERE code = ?
        OR title LIKE ?
        ORDER BY id DESC
        LIMIT 10
    """, (
        query,
        f"%{query}%"
    )).fetchall()

    if not rows:
        await message.answer(
            "❌ <b>Kino topilmadi.</b>",
            reply_markup=main_menu(message.from_user.id),
            parse_mode="HTML"
        )
        return

    for movie in rows:
        if movie["prime"] and not is_prime(message.from_user.id):
            await message.answer(
                f"🔒 <b>{movie['title']}</b>\n\n"
                "Bu kino faqat Prime/VIP uchun.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text="⭐ Prime olish",
                        callback_data="prime_back"
                    )]
                ]),
                parse_mode="HTML"
            )
            continue

        db.execute(
            "UPDATE movies SET views=views+1 WHERE id=?",
            (movie["id"],)
        )
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

    await message.answer(
        "🏠 Asosiy menyu:",
        reply_markup=main_menu(message.from_user.id)
    )


# =========================================================
# KINOLAR RO'YXATI
# =========================================================

@dp.message(F.text == "📚 Kinolar ro'yxati")
async def movie_list(message: Message):
    if not await require_subscription(message):
        return

    rows = db.execute(
        "SELECT * FROM movies ORDER BY id DESC LIMIT 50"
    ).fetchall()

    if not rows:
        await message.answer("📚 Hozircha kinolar yo'q.")
        return

    buttons = []

    for movie in rows:
        title = ("⭐ " if movie["prime"] else "") + movie["title"]

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
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "close_movies")
async def close_movies(callback: CallbackQuery):
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data.startswith("movie_"))
async def movie_click(callback: CallbackQuery):
    if not is_admin_id(callback.from_user.id):
        if not await check_subscription(callback.from_user.id):
            await callback.answer(
                "Avval kanalga obuna bo'ling.",
                show_alert=True
            )
            return

    movie_id = int(callback.data.replace("movie_", ""))

    movie = db.execute(
        "SELECT * FROM movies WHERE id=?",
        (movie_id,)
    ).fetchone()

    if not movie:
        await callback.answer(
            "Kino topilmadi.",
            show_alert=True
        )
        return

    if movie["prime"] and not is_prime(callback.from_user.id):
        await callback.message.answer(
            "🔒 Bu kino faqat Prime/VIP uchun.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="⭐ Prime olish",
                    callback_data="prime_back"
                )]
            ])
        )
        await callback.answer()
        return

    db.execute(
        "UPDATE movies SET views=views+1 WHERE id=?",
        (movie_id,)
    )
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
# KINO QO'SHISH
# =========================================================

@dp.message(F.text == "➕ Kino qo'shish")
async def add_movie_start(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return

    await state.set_state(AddMovie.code)

    await message.answer(
        "➕ <b>Kino qo'shish — 1/4</b>\n\n"
        "Kino kodini yuboring.\n\n"
        "Masalan: <code>247</code>",
        reply_markup=cancel_kb(admin=True),
        parse_mode="HTML"
    )


@dp.message(StateFilter(AddMovie.code))
async def add_movie_code(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        await state.clear()
        return

    code = message.text.strip()

    if not code:
        await message.answer(
            "❌ Kod bo'sh bo'lishi mumkin emas.",
            reply_markup=cancel_kb(admin=True)
        )
        return

    exists = db.execute(
        "SELECT 1 FROM movies WHERE code=?",
        (code,)
    ).fetchone()

    if exists:
        await message.answer(
            "❌ Bu kod allaqachon mavjud.",
            reply_markup=cancel_kb(admin=True)
        )
        return

    await state.update_data(code=code)
    await state.set_state(AddMovie.title)

    await message.answer(
        "➕ <b>Kino qo'shish — 2/4</b>\n\n"
        "Kino nomini yuboring.",
        reply_markup=cancel_kb(admin=True),
        parse_mode="HTML"
    )


@dp.message(StateFilter(AddMovie.title))
async def add_movie_title(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        await state.clear()
        return

    title = message.text.strip()

    if not title:
        await message.answer(
            "❌ Kino nomi bo'sh.",
            reply_markup=cancel_kb(admin=True)
        )
        return

    await state.update_data(title=title)
    await state.set_state(AddMovie.video)

    await message.answer(
        "➕ <b>Kino qo'shish — 3/4</b>\n\n"
        "Kino videosini yuboring.",
        reply_markup=cancel_kb(admin=True),
        parse_mode="HTML"
    )


@dp.message(StateFilter(AddMovie.video), F.video)
async def add_movie_video(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        await state.clear()
        return

    await state.update_data(
        file_id=message.video.file_id
    )
    await state.set_state(AddMovie.prime)

    await message.answer(
        "➕ <b>Kino qo'shish — 4/4</b>\n\n"
        "Kino turini tanlang:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🆓 Oddiy kino",
                callback_data="movie_normal"
            )],
            [InlineKeyboardButton(
                text="⭐ Prime/VIP kino",
                callback_data="movie_prime"
            )],
            [InlineKeyboardButton(
                text="❌ Bekor qilish",
                callback_data="movie_add_cancel"
            )]
        ]),
        parse_mode="HTML"
    )


@dp.message(StateFilter(AddMovie.video))
async def add_movie_wrong(message: Message):
    await message.answer(
        "❌ Videoni video sifatida yuboring.",
        reply_markup=cancel_kb(admin=True)
    )


@dp.callback_query(F.data == "movie_add_cancel")
async def movie_add_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()

    await callback.message.answer(
        "❌ Kino qo'shish bekor qilindi.",
        reply_markup=admin_menu(callback.from_user.id)
    )
    await callback.answer()


@dp.callback_query(F.data.in_({"movie_normal", "movie_prime"}))
async def movie_add_finish(callback: CallbackQuery, state: FSMContext):
    if not is_admin_id(callback.from_user.id):
        return

    data = await state.get_data()

    if not data:
        await callback.answer(
            "Jarayon topilmadi.",
            show_alert=True
        )
        return

    prime = 1 if callback.data == "movie_prime" else 0

    try:
        db.execute("""
            INSERT INTO movies(
                code, title, file_id, prime, added_by
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            data["code"],
            data["title"],
            data["file_id"],
            prime,
            callback.from_user.id
        ))
        db.commit()
    except sqlite3.IntegrityError:
        await state.clear()

        await callback.message.answer(
            "❌ Bu kod allaqachon mavjud.",
            reply_markup=admin_menu(callback.from_user.id)
        )
        await callback.answer()
        return

    await state.clear()

    await callback.message.answer(
        "✅ <b>Kino qo'shildi!</b>\n\n"
        f"🎬 {data['title']}\n"
        f"🔢 Kod: <code>{data['code']}</code>\n"
        f"⭐ Prime/VIP: {'Ha' if prime else 'Yo‘q'}\n\n"
        "Endi foydalanuvchi kodni yozsa, kino chiqadi.",
        reply_markup=admin_menu(callback.from_user.id),
        parse_mode="HTML"
    )
    await callback.answer()


# =========================================================
# KINO O'CHIRISH
# =========================================================

@dp.message(F.text == "🗑 Kino o'chirish")
async def delete_movie_start(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        return

    await state.set_state(DeleteMovie.code)

    await message.answer(
        "🗑 <b>Kino o'chirish</b>\n\n"
        "Kino kodini yuboring.",
        reply_markup=cancel_kb(admin=True),
        parse_mode="HTML"
    )


@dp.message(StateFilter(DeleteMovie.code))
async def delete_movie_received(message: Message, state: FSMContext):
    if not is_admin_id(message.from_user.id):
        await state.clear()
        return

    code = message.text.strip()

    movie = db.execute(
        "SELECT * FROM movies WHERE code=?",
        (code,)
    ).fetchone()

    if not movie:
        await message.answer(
            "❌ Kino topilmadi.",
            reply_markup=cancel_kb(admin=True)
        )
        return

    db.execute(
        "DELETE FROM movies WHERE code=?",
        (code,)
    )
    db.commit()

    await state.clear()

    await message.answer(
        "🗑 <b>Kino o'chirildi.</b>\n\n"
        f"🎬 {movie['title']}\n"
        f"🔢 Kod: <code>{code}</code>",
        reply_markup=admin_menu(message.from_user.id),
        parse_mode="HTML"
    )


# =========================================================
# KINO BUYURTMA
# =========================================================

@dp.message(F.text == "🎬 Kino buyurtma qilish")
async def request_start(message: Message, state: FSMContext):
    if not await require_subscription(message):
        return

    await state.set_state(MovieRequest.text)

    await message.answer(
        "🎬 <b>Kino buyurtma qilish</b>\n\n"
        "Qaysi kinoni izlayotganingizni yozing:",
        reply_markup=cancel_kb(),
        parse_mode="HTML"
    )


@dp.message(StateFilter(MovieRequest.text))
async def request_received(message: Message, state: FSMContext):
    text = message.text.strip()
    ref_id = user_ref_admin_id(message.from_user.id) or owner_id()

    db.execute("""
        INSERT INTO requests(
            user_id, username, text, ref_admin_id, created_at
        )
        VALUES (?, ?, ?, ?, ?)
    """, (
        message.from_user.id,
        message.from_user.username or "",
        text,
        ref_id,
        now_iso()
    ))
    db.commit()

    await state.clear()

    try:
        await bot.send_message(
            ref_id,
            "🎬 <b>Yangi kino buyurtmasi!</b>\n\n"
            f"👤 @{message.from_user.username or '-'}\n"
            f"🆔 <code>{message.from_user.id}</code>\n\n"
            f"📝 {text}",
            parse_mode="HTML"
        )
    except Exception as e:
        print("REQUEST SEND:", e)

    await message.answer(
        "✅ <b>Buyurtmangiz adminga yuborildi.</b>",
        reply_markup=main_menu(message.from_user.id),
        parse_mode="HTML"
    )


# =========================================================
# INSTAGRAM / REKLAMA
# =========================================================

@dp.message(F.text == "📸 Instagramga qaytish")
async def instagram(message: Message):
    await message.answer(
        "📸 Instagram:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="📸 Instagram",
                url="https://www.instagram.com/oemovie/"
            )],
            [InlineKeyboardButton(
                text="❌ Yopish",
                callback_data="close_msg"
            )]
        ])
    )


@dp.message(F.text == "🤝 Reklama & Bot olish")
async def advertising(message: Message):
    await message.answer(
        "🤝 <b>Reklama & Bot olish</b>\n\n"
        "Admin bilan bog'lanish:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="👨‍💻 Admin",
                url=f"https://t.me/{OWNER_USERNAME}"
            )],
            [InlineKeyboardButton(
                text="❌ Yopish",
                callback_data="close_msg"
            )]
        ]),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "close_msg")
async def close_msg(callback: CallbackQuery):
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


# =========================================================
# CALLBACK SUBSCRIPTION GUARD
# =========================================================

# Any inline button that belongs to the user side must be blocked
# if the user has left a mandatory channel.
@dp.callback_query()
async def subscription_callback_guard(callback: CallbackQuery):
    if is_admin_id(callback.from_user.id):
        return

    # Allow only the subscription confirmation callback while unsubscribed.
    if callback.data == "check_sub":
        return

    if await check_subscription(callback.from_user.id):
        return

    await callback.answer(
        "🔐 Avval majburiy kanalga obuna bo'ling.",
        show_alert=True
    )


# =========================================================
# ADMIN PANEL CALLBACK
# =========================================================

@dp.callback_query(F.data == "admin_panel_cb")
async def admin_panel_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()

    if not is_admin_id(callback.from_user.id):
        await callback.answer(
            "Ruxsat yo'q.",
            show_alert=True
        )
        return

    await callback.message.answer(
        "👨‍💻 <b>Admin panel</b>",
        reply_markup=admin_menu(callback.from_user.id),
        parse_mode="HTML"
    )
    await callback.answer()


# =========================================================
# TO'G'RIDAN-TO'G'RI KOD/NOM BILAN KINO QIDIRISH
# =========================================================

@dp.message()
async def fallback_search(message: Message):
    text = (message.text or "").strip()

    if not text or text.startswith("/"):
        return

    known_buttons = {
        "🔎 Kino qidirish",
        "⭐ Prime status",
        "📚 Kinolar ro'yxati",
        "📸 Instagramga qaytish",
        "🎬 Kino buyurtma qilish",
        "🤝 Reklama & Bot olish",
        "👨‍💻 Admin panel",
        "➕ Kino qo'shish",
        "🗑 Kino o'chirish",
        "💳 Mening kartam",
        "📊 Mening statistikam",
        "🔗 Mening silkam",
        "👥 Adminlar",
        "📢 Kanal qo'shish",
        "🗑 Kanal o'chirish",
        "📋 Kanallar",
        "📊 Umumiy statistika",
        "🏠 Asosiy menyu",
        "⬅️ Admin panel",
        "⬅️ Asosiy menyu",
        "❌ Bekor qilish",
    }

    if text in known_buttons:
        return

    if not await require_subscription(message):
        return

    rows = db.execute("""
        SELECT * FROM movies
        WHERE code = ?
        OR title LIKE ?
        ORDER BY id DESC
        LIMIT 10
    """, (
        text,
        f"%{text}%"
    )).fetchall()

    if not rows:
        await message.answer(
            "❌ Buyruq yoki kino topilmadi.\n\n"
            "🔎 Kino qidirish tugmasini bosing yoki "
            "kino kodini yozing."
        )
        return

    for movie in rows:
        if movie["prime"] and not is_prime(message.from_user.id):
            await message.answer(
                f"🔒 <b>{movie['title']}</b>\n\n"
                "Bu kino faqat Prime/VIP uchun.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text="⭐ Prime olish",
                        callback_data="prime_back"
                    )]
                ]),
                parse_mode="HTML"
            )
            continue

        db.execute(
            "UPDATE movies SET views=views+1 WHERE id=?",
            (movie["id"],)
        )
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
# /MYID
# =========================================================

@dp.message(Command("myid"))
async def myid(message: Message):
    ensure_owner(message.from_user)

    await message.answer(
        f"🆔 Sizning Telegram ID: <code>{message.from_user.id}</code>",
        parse_mode="HTML"
    )


# =========================================================
# RENDER HEALTH SERVER
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

    print(f"Health server {port} portda ishlayapti.")


# =========================================================
# RUN
# =========================================================

async def main():
    print("🎬 KinoCinema bot ishga tushmoqda...")

    await start_web_server()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
