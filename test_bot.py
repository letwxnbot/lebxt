import os
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
print("BOT_TOKEN:", BOT_TOKEN)  # Debug

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN not found in .env file!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def start_handler(msg: types.Message):
    await msg.answer("✅ Bot is working! Hello, " + msg.from_user.first_name)

async def main():
    print("🚀 Bot started polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
