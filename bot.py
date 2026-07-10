import os, re, logging, asyncio, hashlib, json
from datetime import datetime, timedelta
from collections import defaultdict
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, Message
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from openai import OpenAI
from dotenv import load_dotenv
from duckduckgo_search import DDGS

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
CHANNEL_ID = os.getenv("CHANNEL_ID")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com"
)

# Обновлённый системный промпт (без белого списка)
SYSTEM_PROMPT = {
    "role": "system",
    "content": (
        "Ты — опытный консультант по сборке ПК. Сегодня 10 июля 2026 года. "
        "Используй только реальные, актуальные комплектующие. "
        "Если не уверен в существовании модели, скажи об этом. "
        "При подборе конфигурации соблюдай баланс цены и производительности.\n\n"
        "**ПРАВИЛА:**\n"
        "- Для бюджета до 50 000₽ предлагай только б/у сборки.\n"
        "- Для 2K-гейминга минимальная видеокарта — RTX 4060 Ti или RX 7700 XT.\n"
        "- Для Full HD — RTX 3060 / RX 6600.\n"
        "- Избегай процессоров Intel до 12-го поколения и AMD Ryzen 1000/2000.\n"
        "- Указывай конкретные модели, примерные цены и FPS в 3-4 играх.\n"
        "- Отвечай на русском языке."
    )
}

# Кэш проверенных моделей
verified_models = {}

def check_component_exists(component_name: str) -> bool:
    if component_name in verified_models:
        return verified_models[component_name]
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(
                f"{component_name} купить site:dns-shop.ru OR site:citilink.ru OR site:overclockers.ru",
                max_results=3
            ))
            exists = len(results) > 0
            verified_models[component_name] = exists
            return exists
    except Exception as e:
        logger.warning(f"DuckDuckGo search failed: {e}")
        return True  # разрешаем, если поиск не сработал

# Глобальные хранилища
cache = {}
history = defaultdict(list)
build_ratings = {}
quiz_sessions = {}
user_last_request = {}

def rate_limit(user_id: int) -> bool:
    now = datetime.now()
    last = user_last_request.get(user_id)
    if last and (now - last) < timedelta(seconds=6):
        return False
    user_last_request[user_id] = now
    return True

def extract_key_components(text: str) -> str:
    gpu = re.search(r'(RTX\s?\d{4}\s?(Super|Ti)?|RX\s?\d{4,5}\s?(XT|GRE)?|Arc\s?A\d{3})', text, re.I)
    cpu = re.search(r'(Ryzen\s?\d\s?\d{4}\w?|Core\s?i\d-\d{4,5}\w?)', text, re.I)
    parts = []
    if gpu: parts.append(gpu.group(1))
    if cpu: parts.append(cpu.group(1))
    return ' '.join(parts) if parts else text[:100]

def get_market_url(components: str) -> str:
    return f"https://market.yandex.ru/search?text={components.replace(' ', '+')}"

def get_cache_key(messages: list) -> str:
    raw = json.dumps([m['content'] for m in messages], ensure_ascii=False)
    return hashlib.md5(raw.encode()).hexdigest()

async def ask_ai(messages: list) -> str:
    max_attempts = 2
    for attempt in range(max_attempts):
        cache_key = get_cache_key(messages)
        if cache_key in cache:
            return cache[cache_key]
        try:
            resp = client.chat.completions.create(
                model="deepseek-chat",
                messages=messages,
                temperature=0.3,
                max_tokens=2500
            )
            answer = resp.choices[0].message.content

            gpu_match = re.search(r'(RTX\s?\d{4}\s?(Super|Ti)?|RX\s?\d{4,5}\s?(XT|GRE)?)', answer)
            cpu_match = re.search(r'(Ryzen\s?\d\s?\d{4}\w?|Core\s?i\d-\d{4,5}\w?)', answer)
            
            corrections = []
            if gpu_match:
                gpu = gpu_match.group(1)
                if not check_component_exists(gpu):
                    corrections.append(f"Видеокарта {gpu} не найдена или устарела.")
            if cpu_match:
                cpu = cpu_match.group(1)
                if not check_component_exists(cpu):
                    corrections.append(f"Процессор {cpu} не найден или устарел.")

            if corrections:
                logger.warning(f"Некорректные компоненты: {corrections}. Попытка {attempt+1}")
                messages.append({
                    "role": "user",
                    "content": f"Твой ответ содержит ошибки: {'; '.join(corrections)}. Найди актуальные замены для этих компонентов и перестрой сборку."
                })
                continue
            
            cache[cache_key] = answer
            return answer

        except Exception as e:
            logger.error(f"DeepSeek error: {e}")
            return "Извините, ошибка ИИ."
    
    return "Не удалось подобрать актуальную конфигурацию. Попробуйте переформулировать запрос."

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# Команды бота (все те же: /start, /budget, /compare, /quiz, /top)
@dp.message(Command("start"))
async def start_cmd(message: Message):
    user_id = message.from_user.id
    history[user_id] = [SYSTEM_PROMPT]
    await message.answer(
        "👋 Привет! Я ИИ-консультант по сборке ПК.\n"
        "/budget <сумма> — быстрый подбор\n"
        "/compare <конф1> | <конф2> — сравнение\n"
        "/quiz — персональный подбор\n"
        "/top — топ сборок\n"
        "Просто напиши запрос, и я подберу актуальную конфигурацию!"
    )

@dp.message(Command("budget"))
async def budget_cmd(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("Укажите сумму, например: /budget 120000")
        return
    try:
        amount = int(command.args.replace(" ", ""))
    except ValueError:
        await message.answer("Некорректная сумма.")
        return
    message.text = f"Собери ПК с бюджетом {amount} рублей. Задачи: игры, 2K."
    await handle_message(message)

@dp.message(Command("compare"))
async def compare_cmd(message: Message, command: CommandObject):
    if not command.args or '|' not in command.args:
        await message.answer("Пример: /compare Ryzen 5 7500F, RTX 4070 | i5-12400F, RX 7800 XT")
        return
    parts = command.args.split('|')
    if len(parts) != 2:
        await message.answer("Укажите две конфигурации через |")
        return
    conf1, conf2 = parts[0].strip(), parts[1].strip()
    message.text = f"Сравни две конфигурации:\n1) {conf1}\n2) {conf2}\nУкажи плюсы/минусы, FPS, цены."
    await handle_message(message)

@dp.message(Command("quiz"))
async def quiz_start(message: Message):
    user_id = message.from_user.id
    quiz_sessions[user_id] = {"step": 1}
    await message.answer("📝 Подберём ПК. 1️⃣ Какой бюджет?")

@dp.message(Command("top"))
async def top_builds(message: Message):
    if not build_ratings:
        await message.answer("Пока нет оценённых сборок.")
        return
    sorted_builds = sorted(build_ratings.items(),
                           key=lambda x: x[1]['likes'] - x[1]['dislikes'],
                           reverse=True)[:5]
    text = "🏆 Топ сборок:\n\n"
    for i, (bid, data) in enumerate(sorted_builds, 1):
        short = data['text'][:100].replace('\n', ' ')
        text += f"{i}. {short}...\n👍{data['likes']} 👎{data['dislikes']}\n\n"
    await message.answer(text)

# Основной обработчик сообщений
@dp.message(F.text)
async def handle_message(message: Message):
    user_id = message.from_user.id
    user_text = message.text

    if user_id in quiz_sessions:
        await quiz_step(message)
        return

    if not rate_limit(user_id):
        await message.answer("⏳ Слишком часто. Подождите 6 секунд.")
        return

    if user_id not in history:
        history[user_id] = [SYSTEM_PROMPT]
    if len(history[user_id]) > 10:
        history[user_id] = [SYSTEM_PROMPT] + history[user_id][-9:]

    history[user_id].append({"role": "user", "content": user_text})
    await bot.send_chat_action(message.chat.id, "typing")
    answer = await ask_ai(history[user_id])
    history[user_id].append({"role": "assistant", "content": answer})

    if any(w in user_text.lower() for w in ["бюджет", "конфигурация", "сборк"]):
        build_ratings[str(message.message_id)] = {"likes": 0, "dislikes": 0, "text": answer}

    components = extract_key_components(answer)
    market_url = get_market_url(components)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Альтернатива", callback_data=f"alt_{user_id}"),
         InlineKeyboardButton(text="💰 Где купить", url=market_url)],
        [InlineKeyboardButton(text="📊 Бенчмарки", url="https://www.cpubenchmark.net/"),
         InlineKeyboardButton(text="👍 Лайк", callback_data=f"like_{message.message_id}"),
         InlineKeyboardButton(text="👎 Дизлайк", callback_data=f"dislike_{message.message_id}")],
        [InlineKeyboardButton(text="🔔 Отслеживать цену", callback_data=f"track_{components}")]
    ])
    await message.answer(answer, reply_markup=keyboard)

# Квиз
async def quiz_step(message: Message):
    user_id = message.from_user.id
    session = quiz_sessions[user_id]
    step = session["step"]
    if step == 1:
        try:
            session["budget"] = int(message.text.replace(" ", ""))
        except:
            await message.answer("Введите бюджет числом.")
            return
        session["step"] = 2
        await message.answer("2️⃣ Для каких задач? (игры, монтаж, офис)")
    elif step == 2:
        session["tasks"] = message.text.lower()
        session["step"] = 3
        await message.answer("3️⃣ Нужна периферия? (да/нет)")
    elif step == 3:
        session["periphery"] = "да" in message.text.lower()
        session["step"] = 4
        await message.answer("4️⃣ Предпочтительный магазин?")
    elif step == 4:
        session["shop"] = message.text.strip()
        prompt = (f"Подбери ПК: бюджет {session['budget']}₽, задачи {session['tasks']}, "
                  f"{'с периферией' if session['periphery'] else 'без периферии'}, магазин {session['shop']}.")
        del quiz_sessions[user_id]
        message.text = prompt
        await handle_message(message)

# Callback-обработчики
@dp.callback_query(F.data.startswith("alt_"))
async def alt_build(call: CallbackQuery):
    user_id = int(call.data.split("_")[1])
    history[user_id].append({"role": "user", "content": "Предложи альтернативную сборку."})
    answer = await ask_ai(history[user_id])
    history[user_id].append({"role": "assistant", "content": answer})
    await call.message.edit_text(answer, reply_markup=call.message.reply_markup)
    await call.answer()

@dp.callback_query(F.data.startswith("like_"))
async def like_build(call: CallbackQuery):
    build_id = call.data.split("_")[1]
    if build_id in build_ratings:
        build_ratings[build_id]["likes"] += 1
    await call.answer("👍")

@dp.callback_query(F.data.startswith("dislike_"))
async def dislike_build(call: CallbackQuery):
    build_id = call.data.split("_")[1]
    if build_id in build_ratings:
        build_ratings[build_id]["dislikes"] += 1
    await call.answer("👎")

@dp.callback_query(F.data.startswith("track_"))
async def track_price(call: CallbackQuery):
    await call.answer("🔔 Функция отслеживания цен в разработке.", show_alert=True)

# Админ-команда /post
@dp.message(Command("post"))
async def post_to_channel(message: Message):
    if str(message.from_user.id) != ADMIN_CHAT_ID:
        await message.answer("Нет прав.")
        return
    if not message.reply_to_message:
        await message.answer("Ответьте на сообщение со сборкой.")
        return
    if CHANNEL_ID:
        try:
            await bot.send_message(CHANNEL_ID, message.reply_to_message.text)
            await message.answer("✅ Опубликовано.")
        except Exception as e:
            await message.answer(f"Ошибка: {e}")
    else:
        await message.answer("Не указан CHANNEL_ID.")

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
    logger.info(f"Webhook set to {WEBHOOK_URL}")

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
