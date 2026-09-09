import os
import asyncio
import sqlite3

from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder


TOKEN = os.getenv("BOT_TOKEN")
CHANNEL = "@uz_kinocinema"

bot = Bot(TOKEN)
dp = Dispatcher()


# =========================
# DATABASE
# =========================

db = sqlite3.connect("movies.db")
cursor = db.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS movies (
    code TEXT PRIMARY KEY,
    file_id TEXT NOT NULL
)
""")

db.commit()


# =========================
# SUBSCRIPTION
# =========================

async def check_subscription(user_id):

    try:
        member = await bot.get_chat_member(
            CHANNEL,
            user_id
        )

        return member.status in [
            "member",
            "administrator",
            "creator"
        ]

    except Exception:
        return False


def subscription_keyboard():

    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text="📢 Kanalga obuna bo‘lish",
            url="https://t.me/uz_kinocinema"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="✅ Obunani tekshirish",
            callback_data="check_sub"
        )
    )

    return builder.as_markup()


# =========================
# START
# =========================

@dp.message(Command("start"))
async def start(message: types.Message):

    subscribed = await check_subscription(
        message.from_user.id
    )

    if not subscribed:

        await message.answer(
            "🎬 KinoCinema botiga xush kelibsiz!\n\n"
            "Botdan foydalanish uchun kanalimizga "
            "obuna bo‘ling 👇",
            reply_markup=subscription_keyboard()
        )

        return

    await message.answer(
        "✅ Obuna tasdiqlandi!\n\n"
        "🎥 Kino kodini yuboring.\n"
        "Masalan: 247"
    )


# =========================
# CHECK SUBSCRIPTION
# =========================

@dp.callback_query(lambda c: c.data == "check_sub")
async def check_sub(callback: types.CallbackQuery):

    subscribed = await check_subscription(
        callback.from_user.id
    )

    if subscribed:

        await callback.message.edit_text(
            "✅ Obuna tasdiqlandi!\n\n"
            "🎥 Kino kodini yuboring.\n"
            "Masalan: 247"
        )

        await callback.answer()

    else:

        await callback.answer(
            "❌ Avval kanalga obuna bo‘ling!",
            show_alert=True
        )


# =========================
# GET TELEGRAM ID
# =========================

@dp.message(Command("myid"))
async def my_id(message: types.Message):

    await message.answer(
        f"🆔 Sizning Telegram ID:\n"
        f"`{message.from_user.id}`",
        parse_mode="Markdown"
    )


# =========================
# MOVIE CODE
# =========================

@dp.message()
async def get_movie(message: types.Message):

    subscribed = await check_subscription(
        message.from_user.id
    )

    if not subscribed:

        await message.answer(
            "❌ Avval kanalga obuna bo‘ling.",
            reply_markup=subscription_keyboard()
        )

        return

    if not message.text:
        return

    code = message.text.strip()

    cursor.execute(
        "SELECT file_id FROM movies WHERE code = ?",
        (code,)
    )

    movie = cursor.fetchone()

    if movie:

        await message.answer_video(
            movie[0],
            caption="🎬 KinoCinema\n\n"
                    "🍿 Yoqimli tomosha!"
        )

    else:

        await message.answer(
            "❌ Bunday kino kodi topilmadi."
        )


# =========================
# RENDER HTTP SERVER
# =========================

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


# =========================
# MAIN
# =========================

async def main():

    print("🎬 KinoCinema bot ishga tushmoqda...")

    runner = await start_web_server()

    try:

        await dp.start_polling(bot)

    finally:

        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
