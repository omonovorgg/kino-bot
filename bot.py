import os
import sqlite3
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
import asyncio

TOKEN = os.getenv("BOT_TOKEN")
CHANNEL = "@uz_kinocinema"

bot = Bot(TOKEN)
dp = Dispatcher()

db = sqlite3.connect("movies.db")
cursor = db.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS movies (
    code TEXT PRIMARY KEY,
    file_id TEXT NOT NULL
)
""")
db.commit()


async def check_subscription(user_id):
    try:
        member = await bot.get_chat_member(CHANNEL, user_id)

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


@dp.message(Command("start"))
async def start(message: types.Message):

    subscribed = await check_subscription(message.from_user.id)

    if not subscribed:
        await message.answer(
            "🎬 KinoCinema botiga xush kelibsiz!\n\n"
            "Botdan foydalanish uchun kanalimizga obuna bo‘ling 👇",
            reply_markup=subscription_keyboard()
        )
        return

    await message.answer(
        "✅ Obuna tasdiqlandi!\n\n"
        "🎥 Kino kodini yuboring.\n"
        "Masalan: 247"
    )


@dp.callback_query(lambda c: c.data == "check_sub")
async def check_sub(callback: types.CallbackQuery):

    subscribed = await check_subscription(callback.from_user.id)

    if subscribed:
        await callback.message.edit_text(
            "✅ Obuna tasdiqlandi!\n\n"
            "🎥 Kino kodini yuboring.\n"
            "Masalan: 247"
        )
    else:
        await callback.answer(
            "❌ Avval kanalga obuna bo‘ling!",
            show_alert=True
        )


@dp.message()
async def get_movie(message: types.Message):

    subscribed = await check_subscription(message.from_user.id)

    if not subscribed:
        await message.answer(
            "❌ Avval kanalga obuna bo‘ling.",
            reply_markup=subscription_keyboard()
        )
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
            caption="🎬 KinoCinema"
        )
    else:
        await message.answer(
            "❌ Bunday kino kodi topilmadi."
        )


async def main():
    print("Bot ishga tushdi...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
