import os
import re
import logging
import asyncio
from datetime import datetime, timedelta
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

# Клиент DeepSeek
client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com"
)

# ======================================================================
# СУПЕР-ПРОМПТ: бот теперь настоящий эксперт под любой бюджет
# ======================================================================
SYSTEM_PROMPT = {
    "role": "system",
    "content": (
        "Ты — опытный консультант по сборке ПК, работаешь в российском магазине. "
        "Твоя задача — подбирать **максимально точные и реалистичные конфигурации** под любой бюджет и задачи, "
        "используя актуальные цены июля 2026 года.\n\n"

        "**ПРАВИЛА РАБОТЫ:**\n"
        "1. Всегда уточняй у пользователя: бюджет, цели (игры, работа, монтаж), "
        "разрешение монитора и нужно ли периферию. Если данных нет — задай наводящие вопросы.\n"
        "2. При бюджете **до 50 000 ₽** честно предупреждай, что собрать игровой ПК на новых деталях невозможно. "
        "Предлагай только б/у сборки с конкретными моделями, примерными ценами (ориентируйся на Авито) и пояснением, "
        "на что обратить внимание при покупке б/у.\n"
        "3. Для бюджета **50 000–80 000 ₽** рассматривай комбинацию новых и б/у комплектующих (например, видеокарта б/у).\n"
        "4. Для бюджета **выше 80 000 ₽** предлагай новые актуальные платформы (AM5, LGA1700), "
        "но не исключай б/у видеокарту, если это даст значительный прирост производительности.\n"
        "5. **Никогда** не предлагай устаревшие или несовместимые компоненты (DDR3 с современными процессорами, "
        "видеокарты GTX 16xx в новых сборках и т.п.).\n"
        "6. Для каждой конфигурации приводи **конкретные модели** (не «процессор Intel», а «Intel Core i5-12400F»), "
        "примерные цены в рублях, итоговую сумму. Разбивай на категории: процессор, видеокарта, материнская плата, "
        "ОЗУ, накопитель, блок питания, корпус. Указывай рекомендуемые магазины: Яндекс.Маркет, DNS, Ситилинк, а для б/у — Авито.\n"
        "7. В конце подведи итог: какие игры пойдут, на каких настройках, возможные апгрейды, слабые места.\n"
        "8. Отвечай на русском, дружелюбно, с эмодзи 📦💻🎮, но строго профессионально.\n"
    )
}

# Антифлуд: не более 1 сообщения в 6 секунд
user_last_request = {}

def rate_limit(user_id: int) -> bool:
    now = datetime.now()
    if user_id in user_last_request:
        if now - user_last_request[user_id] < timedelta(seconds=6):
            return False
    user_last_request[user_id] = now
    return True

# Извлечение ключевых комплектующих для кнопки "Проверить цены"
def extract_key_components(text: str) -> str:
    gpu_match = re.search(r'(RTX\s?\d{4}\s?(Super|Ti)?|RX\s?\d{4,5}\s?(XT|GRE)?|GeForce\s?\w+\s?\d{4})', text, re.IGNORECASE)
    cpu_match = re.search(r'(Ryzen\s?\d\s?\d{4}\w?|Core\s?i\d-\d{4,5}\w?)', text, re.IGNORECASE)
    parts = []
    if gpu_match:
        parts.append(gpu_match.group(1))
    if cpu_match:
        parts.append(cpu_match.group(1))
    if not parts:
        parts = [text[:100]]
    return ' '.join(parts)

def get_market_url(components: str) -> str:
    return f"https://market.yandex.ru/search?text={components.replace(' ', '+')}"

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
conversations = {}

async def ask_ai(messages: list) -> str:
    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=messages,
            temperature=0.4,
            max_tokens=2500
        )
        return response.choices[0].message.content
    except Exception as e:
        logging.error(f"DeepSeek error: {e}")
        return "Извините, произошла ошибка при обращении к ИИ. Попробуйте позже."

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    user_id = message.from_user.id
    conversations[user_id] = [SYSTEM_PROMPT]
    await message.answer(
        "👋 Привет! Я эксперт по сборке ПК. Подберу конфигурацию под любой бюджет и задачи.\n\n"
        "📌 Напиши, что нужно: бюджет, для чего компьютер (игры, работа), "
        "разрешение монитора и нужно ли периферия (клавиатура, мышь, монитор).\n"
        "Например: «Собери игровой ПК до 60 000 ₽ для Full HD» или «Нужен ПК для монтажа видео за 80 000 ₽»."
    )

@dp.message()
async def handle_message(message: types.Message):
    user_id = message.from_user.id
    user_text = message.text

    if not rate_limit(user_id):
        await message.answer("⏳ Пожалуйста, подожди несколько секунд перед следующим запросом.")
        return

    if user_id not in conversations:
        conversations[user_id] = [SYSTEM_PROMPT]

    conversations[user_id].append({"role": "user", "content": user_text})

    try:
        await bot.send_chat_action(message.chat.id, "typing")
    except:
        pass

    answer = await ask_ai(conversations[user_id])
    conversations[user_id].append({"role": "assistant", "content": answer})

    components = extract_key_components(answer)
    market_url = get_market_url(components)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Проверить цены на ключевые комплектующие", url=market_url)]
    ])

    await message.answer(answer, reply_markup=keyboard)

# Вебхуки
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
        asyncio.run(dp.start_polling(bot))
