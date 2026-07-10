import os, re, logging, asyncio, hashlib, json, time
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

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")          # твой Telegram ID для алертов
CHANNEL_ID = os.getenv("CHANNEL_ID")                # ID или @username канала для автопостинга

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com"
)

# ========== Глобальные хранилища ==========
# Кэш ответов DeepSeek (для одинаковых запросов)
cache = {}  # ключ: хэш (промпт + сообщение), значение: ответ
# История диалогов (ограничим 5 сборками на пользователя)
history = defaultdict(list)  # user_id -> [{"role","content"}, ...]
# Рейтинг сборок: id сборки (из ответа бота) -> {"likes":0, "dislikes":0, "text":...}
build_ratings = {}
# Пользовательские сессии для /quiz
quiz_sessions = {}

# ========== Усиленный системный промпт с новыми правилами ==========
SYSTEM_PROMPT = {
    "role": "system",
    "content": (
        "Ты — элитный консультант по сборке ПК, работаешь в российском магазине (июль 2026). "
        "**Обязан:**\n"
        "- Предлагать конфигурации под любой бюджет, включая б/у рынок (Авито) для сумм до 50 000₽.\n"
        "- Указывать конкретные модели, примерные цены в рублях, итоговую сумму.\n"
        "- В конце добавлять блок «📊 Ожидаемый FPS» (для игровых сборок) с 3-4 популярными играми.\n"
        "- Рассчитывать энергопотребление (сумма TDP) и рекомендовать БП с запасом 20%.\n"
        "- Предупреждать о возможной необходимости обновления BIOS для материнских плат.\n"
        "- Если не указан бюджет или задачи — задать уточняющие вопросы.\n"
        "- Отвечать на русском (по умолчанию) или на языке запроса.\n"
        "Не предлагай устаревшие компоненты (GTX 16xx, RTX 20xx, Intel до 12-го поколения и т.п.).\n"
        "Для каждой сборки давай 2-3 альтернативных варианта с пояснениями."
    )
}

# ========== Инициализация бота ==========
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# ========== Вспомогательные функции ==========
def rate_limit(user_id: int) -> bool:
    """Ограничение 1 запрос в 6 секунд (10 в минуту)."""
    now = datetime.now()
    last = user_last_request.get(user_id)
    if last and (now - last) < timedelta(seconds=6):
        return False
    user_last_request[user_id] = now
    return True

user_last_request = {}

def extract_key_components(text: str) -> str:
    gpu = re.search(r'(RTX\s?\d{4}\s?(Super|Ti)?|RX\s?\d{4,5}\s?(XT|GRE)?|GeForce\s?\w+\s?\d{4})', text, re.I)
    cpu = re.search(r'(Ryzen\s?\d\s?\d{4}\w?|Core\s?i\d-\d{4,5}\w?)', text, re.I)
    parts = []
    if gpu: parts.append(gpu.group(1))
    if cpu: parts.append(cpu.group(1))
    return ' '.join(parts) if parts else text[:100]

def get_market_url(components: str) -> str:
    return f"https://market.yandex.ru/search?text={components.replace(' ', '+')}"

def get_cache_key(messages: list) -> str:
    """Хэш от склеенных сообщений для кэширования."""
    raw = json.dumps([m['content'] for m in messages], ensure_ascii=False)
    return hashlib.md5(raw.encode()).hexdigest()

async def ask_ai(messages: list) -> str:
    cache_key = get_cache_key(messages)
    if cache_key in cache:
        logger.info("Ответ взят из кэша")
        return cache[cache_key]
    try:
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=messages,
            temperature=0.4,
            max_tokens=2500
        )
        answer = resp.choices[0].message.content
        cache[cache_key] = answer
        return answer
    except Exception as e:
        logger.error(f"DeepSeek error: {e}")
        # Алерт админу
        if ADMIN_CHAT_ID:
            try:
                await bot.send_message(ADMIN_CHAT_ID, f"🚨 Ошибка DeepSeek: {e}")
            except:
                pass
        return "Извините, произошла ошибка при обращении к ИИ. Попробуйте позже."

async def send_long_message(chat_id: int, text: str, keyboard=None):
    """Разбивает длинное сообщение на части."""
    if len(text) <= 4096:
        return await bot.send_message(chat_id, text, reply_markup=keyboard)
    parts = []
    while len(text) > 4096:
        split_idx = text.rfind('\n', 0, 4096)
        if split_idx == -1:
            split_idx = 4096
        parts.append(text[:split_idx])
        text = text[split_idx:].lstrip()
    parts.append(text)
    for part in parts:
        await bot.send_message(chat_id, part, reply_markup=keyboard if part == parts[-1] else None)

# ========== Обработчики команд ==========
@dp.message(Command("start"))
async def start_cmd(message: Message):
    user_id = message.from_user.id
    history[user_id] = [SYSTEM_PROMPT]
    await message.answer(
        "👋 Я ИИ-консультант по сборке ПК. Вот что я умею:\n"
        "/budget <сумма> — быстрый подбор сборки под бюджет\n"
        "/compare <конфиг1> | <конфиг2> — сравнить две сборки\n"
        "/quiz — персональный подбор через опрос\n"
        "/top — топ сборок по рейтингу\n"
        "Просто напиши запрос, и я соберу идеальный ПК!"
    )

@dp.message(Command("budget"))
async def budget_cmd(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("Укажите сумму, например: /budget 120000")
        return
    try:
        amount = int(command.args.replace(" ", ""))
    except ValueError:
        await message.answer("Некорректная сумма. Введите число.")
        return
    user_text = f"Собери ПК с бюджетом {amount} рублей. Учти все задачи (по умолчанию игровой, Full HD)."
    # Перенаправляем в общий обработчик
    message.text = user_text
    await handle_message(message)

@dp.message(Command("compare"))
async def compare_cmd(message: Message, command: CommandObject):
    if not command.args or '|' not in command.args:
        await message.answer("Используйте: /compare <конфигурация1> | <конфигурация2>\n"
                             "Пример: /compare Ryzen 5 5600, RTX 3060 | i5-12400F, RX 6600")
        return
    parts = command.args.split('|')
    if len(parts) != 2:
        await message.answer("Укажите две конфигурации через вертикальную черту |")
        return
    conf1, conf2 = parts[0].strip(), parts[1].strip()
    prompt = (f"Сравни две конфигурации ПК:\n1) {conf1}\n2) {conf2}\n"
              "Укажи плюсы и минусы, примерную производительность в играх (FPS), "
              "цены, энергопотребление, рекомендации.")
    message.text = prompt
    await handle_message(message)

@dp.message(Command("quiz"))
async def quiz_start(message: Message):
    user_id = message.from_user.id
    quiz_sessions[user_id] = {"step": 1}
    await message.answer("📝 Давай подберём идеальный ПК. Ответь на несколько вопросов.\n"
                         "1️⃣ Какой бюджет (в рублях)?")

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
        likes, dislikes = data['likes'], data['dislikes']
        text += f"{i}. {short}...\n👍{likes} 👎{dislikes}\n\n"
    await message.answer(text)

# ========== Обработка текстовых сообщений ==========
@dp.message(F.text)
async def handle_message(message: Message):
    user_id = message.from_user.id
    user_text = message.text

    # Проверка на активную сессию квиза
    if user_id in quiz_sessions:
        await quiz_step(message)
        return

    if not rate_limit(user_id):
        await message.answer("⏳ Слишком много запросов. Подождите немного.")
        return

    # Инициализация истории
    if user_id not in history:
        history[user_id] = [SYSTEM_PROMPT]
    # Ограничиваем историю 10 последними сообщениями
    if len(history[user_id]) > 10:
        history[user_id] = [SYSTEM_PROMPT] + history[user_id][-9:]

    history[user_id].append({"role": "user", "content": user_text})

    await bot.send_chat_action(message.chat.id, "typing")

    answer = await ask_ai(history[user_id])
    history[user_id].append({"role": "assistant", "content": answer})

    # Сохраняем сборку в рейтинг (если это была сборка)
    if "бюджет" in user_text.lower() or "конфигурация" in user_text.lower() or "сборк" in user_text.lower():
        build_id = str(message.message_id)  # временный ID
        build_ratings[build_id] = {"likes": 0, "dislikes": 0, "text": answer}

    # Инлайн-клавиатура
    components = extract_key_components(answer)
    market_url = get_market_url(components)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 Альтернатива", callback_data=f"alt_{user_id}"),
            InlineKeyboardButton(text="💰 Где купить", url=market_url)
        ],
        [
            InlineKeyboardButton(text="📊 Бенчмарки", url="https://www.cpubenchmark.net/"),
            InlineKeyboardButton(text="👍 Лайк", callback_data=f"like_{message.message_id}"),
            InlineKeyboardButton(text="👎 Дизлайк", callback_data=f"dislike_{message.message_id}")
        ],
        [
            InlineKeyboardButton(text="🔔 Отслеживать цену", callback_data=f"track_{components}")
        ]
    ])
    await send_long_message(message.chat.id, answer, keyboard)

# ========== Квиз (персональные рекомендации) ==========
async def quiz_step(message: Message):
    user_id = message.from_user.id
    session = quiz_sessions[user_id]
    step = session["step"]

    if step == 1:
        try:
            session["budget"] = int(message.text.replace(" ", ""))
        except:
            await message.answer("Пожалуйста, введите бюджет числом.")
            return
        session["step"] = 2
        await message.answer("2️⃣ Для каких задач ПК? (игры, монтаж, офис, всё вместе)")

    elif step == 2:
        session["tasks"] = message.text.lower()
        session["step"] = 3
        await message.answer("3️⃣ Нужна ли периферия? (да/нет)")

    elif step == 3:
        session["periphery"] = "да" in message.text.lower()
        session["step"] = 4
        await message.answer("4️⃣ Предпочитаемый магазин? (Яндекс.Маркет, DNS, Ситилинк, любой)")

    elif step == 4:
        session["shop"] = message.text.strip()
        # Формируем итоговый запрос
        budget = session["budget"]
        tasks = session["tasks"]
        periphery = "нужна периферия" if session["periphery"] else "только системный блок"
        shop = session["shop"]
        prompt = (f"Подбери конфигурацию ПК: бюджет {budget}₽, задачи {tasks}, "
                  f"{periphery}, предпочтительный магазин {shop}.")
        await message.answer(f"Отлично! Собираю для вас конфигурацию:\n{prompt}")
        # Удаляем сессию
        del quiz_sessions[user_id]
        # Отправляем как обычный запрос
        message.text = prompt
        await handle_message(message)

# ========== Обработка инлайн-кнопок ==========
@dp.callback_query(F.data.startswith("alt_"))
async def alt_build(call: CallbackQuery):
    user_id = int(call.data.split("_")[1])
    # Запрашиваем у ИИ альтернативную сборку
    prompt = "Предложи альтернативную конфигурацию для предыдущей сборки с учётом того же бюджета и задач."
    history[user_id].append({"role": "user", "content": prompt})
    answer = await ask_ai(history[user_id])
    history[user_id].append({"role": "assistant", "content": answer})
    # Редактируем исходное сообщение с новым текстом (или отправляем новое)
    await call.message.edit_text(answer, reply_markup=call.message.reply_markup)
    await call.answer()

@dp.callback_query(F.data.startswith("like_"))
async def like_build(call: CallbackQuery):
    build_id = call.data.split("_")[1]
    if build_id in build_ratings:
        build_ratings[build_id]["likes"] += 1
    await call.answer("👍 Спасибо за оценку!")

@dp.callback_query(F.data.startswith("dislike_"))
async def dislike_build(call: CallbackQuery):
    build_id = call.data.split("_")[1]
    if build_id in build_ratings:
        build_ratings[build_id]["dislikes"] += 1
    await call.answer("👎 Оценка учтена.")

@dp.callback_query(F.data.startswith("track_"))
async def track_price(call: CallbackQuery):
    components = call.data[len("track_"):]
    await call.answer("🔔 Функция отслеживания цены пока в разработке. Но вот ссылка на Яндекс.Маркет:\n"
                      + get_market_url(components), show_alert=True)

# ========== Админ-команды ==========
@dp.message(Command("post"))
async def post_to_channel(message: Message):
    if str(message.from_user.id) != ADMIN_CHAT_ID:
        await message.answer("Недостаточно прав.")
        return
    if not message.reply_to_message:
        await message.answer("Ответьте на сообщение с конфигурацией, чтобы опубликовать в канал.")
        return
    target = message.reply_to_message.text
    if CHANNEL_ID:
        try:
            await bot.send_message(CHANNEL_ID, target)
            await message.answer("Опубликовано в канал ✅")
        except Exception as e:
            await message.answer(f"Ошибка: {e}")
    else:
        await message.answer("Не указан CHANNEL_ID в переменных окружения.")

# ========== Вебхуки ==========
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
