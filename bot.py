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
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

logging.basicConfig(level=logging.INFO)

# Клиент DeepSeek (совместим с OpenAI API)
client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com"
)

SYSTEM_PROMPT = {
    "role": "system",
    "content": (
        "Ты — опытный консультант по сборке и оптимизации ПК. "
        "Отвечай на русском, дружелюбно, понятно даже новичкам. "
        "Если просят сборку — уточни бюджет и цели. "
        "Анализируй совместимость, баланс, плюсы и минусы, предлагай альтернативы."
    )
}

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
conversations = {}

async def ask_ai(messages: list) -> str:
    try:
        response = client.chat.completions.create(
            model="deepseek-chat",  # модель DeepSeek
            messages=messages,
            temperature=0.7,
            max_tokens=2000
        )
        return response.choices[0].message.content
    except Exception as e:
        logging.error(f"DeepSeek error: {e}")
        return "Извините, произошла ошибка при обращении к ИИ. Попробуйте позже."

def get_market_url(text: str) -> str:
    return f"https://market.yandex.ru/search?text={text.replace(' ', '+')}"

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    user_id = message.from_user.id
    conversations[user_id] = [SYSTEM_PROMPT]
    await message.answer(
        "👋 Привет! Я ИИ-консультант по сборке ПК.\n"
        "Ты можешь:\n"
        "• попросить собрать ПК под бюджет и задачи (например: «Собери игровой ПК до 100 000 ₽»)\n"
        "• прислать готовый список комплектующих — я оценю и дам советы\n"
        "• спросить о совместимости или характеристиках\n"
        "Для проверки цен просто нажми кнопку под ответом."
    )

@dp.message()
async def handle_message(message: types.Message):
    user_id = message.from_user.id
    user_text = message.text

    if user_id not in conversations:
        conversations[user_id] = [SYSTEM_PROMPT]

    conversations[user_id].append({"role": "user", "content": user_text})

    try:
        await bot.send_chat_action(message.chat.id, "typing")
    except:
        pass

    answer = await ask_ai(conversations[user_id])
    conversations[user_id].append({"role": "assistant", "content": answer})

    search_query = answer[:150].replace('\n', ' ')
    market_url = get_market_url(search_query)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Проверить цены на эти комплектующие", url=market_url)]
    ])

    await message.answer(answer, reply_markup=keyboard)

# Настройка вебхуков для Render
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
