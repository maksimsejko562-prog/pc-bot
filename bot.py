import os
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

logging.basicConfig(level=logging.INFO)

# Клиент Groq
ai_client = OpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1"
)

# Системный промпт
SYSTEM_PROMPT = {
    "role": "system",
    "content": (
        "Ты — опытный консультант по сборке и оптимизации ПК. "
        "Отвечай на русском, дружелюбно, понятно. "
        "При запросе сборки уточни бюджет и цели. "
        "Анализируй совместимость, баланс, плюсы и минусы, предлагай альтернативы."
    )
}

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
conversations = {}

async def ask_ai(messages):
    try:
        response = ai_client.chat.completions.create(
            model="llama-3.1-70b-versatile",
            messages=messages,
            temperature=0.7,
            max_tokens=2000
        )
        return response.choices[0].message.content
    except Exception as e:
        logging.error(f"AI error: {e}")
        return f"Ошибка ИИ: {e}"

def get_market_url(text):
    return f"https://market.yandex.ru/search?text={text.replace(' ', '+')}"

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    conversations[message.from_user.id] = [SYSTEM_PROMPT]
    await message.answer("Привет! Я ИИ-консультант по ПК. Задай вопрос или попроси собрать конфигурацию.")

@dp.message()
async def handle_message(message: types.Message):
    logging.info(f"Получено сообщение от {message.from_user.id}: {message.text}")
    uid = message.from_user.id
    if uid not in conversations:
        conversations[uid] = [SYSTEM_PROMPT]
    conversations[uid].append({"role": "user", "content": message.text})

    # Убираем send_chat_action, который вызывал ошибку
    answer = await ask_ai(conversations[uid])
    conversations[uid].append({"role": "assistant", "content": answer})

    search_query = answer[:150].replace('\n', ' ')
    market_url = get_market_url(search_query)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Проверить цены на эти комплектующие", url=market_url)]
    ])
    await message.answer(answer, reply_markup=keyboard)

# Вебхук
WEBHOOK_PATH = f"/bot/{TELEGRAM_TOKEN}"
WEBHOOK_URL = f"https://{os.environ.get('RENDER_EXTERNAL_HOSTNAME', 'localhost')}" + WEBHOOK_PATH
app = web.Application()

webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
webhook_requests_handler.register(app, path=WEBHOOK_PATH)
setup_application(app, dp, bot=bot)

async def healthcheck(request):
    return web.Response(text="OK")
app.router.add_get("/", healthcheck)

async def on_startup(bot: Bot):
    await bot.set_webhook(WEBHOOK_URL)
    logging.info(f"Webhook set to {WEBHOOK_URL}")

async def on_shutdown(bot: Bot):
    await bot.delete_webhook()

if __name__ == "__main__":
    port = os.environ.get("PORT")
    if port:
        app.on_startup.append(lambda app: on_startup(bot))
        app.on_shutdown.append(lambda app: on_shutdown(bot))
        web.run_app(app, host="0.0.0.0", port=int(port))
    else:
        import asyncio
        asyncio.run(dp.start_polling(bot))
