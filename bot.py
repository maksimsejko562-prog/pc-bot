import os, re, logging, asyncio, hashlib, json
from datetime import datetime, timedelta
from collections import defaultdict
import aiohttp
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
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
CHANNEL_ID = os.getenv("CHANNEL_ID")

# --- Бесплатная LLM через OpenRouter (без DeepSeek, без оплаты за токены) ---
# Регистрация на openrouter.ai, ключ бесплатный, карта не нужна.
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")

# --- Бесплатный поиск для RAG (реальные данные вместо галлюцинаций) ---
# Регистрация на tavily.com, 1000 бесплатных запросов/месяц, карта не нужна.
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
TAVILY_SEARCH_URL = "https://api.tavily.com/search"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

client = OpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1",
)

SYSTEM_PROMPT = {
    "role": "system",
    "content": (
        "Ты — консультант по сборке ПК. Отвечай СТРОГО на основе блока "
        "'АКТУАЛЬНЫЕ ДАННЫЕ ИЗ ПОИСКА' ниже в сообщении пользователя — это реальные "
        "результаты поиска цен и наличия на сегодня. НЕ придумывай модели и цены от себя, "
        "если их нет в этих данных — так и скажи, что не удалось найти актуальную информацию, "
        "вместо того чтобы гадать.\n\n"
        "ПРАВИЛА:\n"
        "- Для бюджета до 50 000₽ предлагай только б/у сборки.\n"
        "- Для 2K-гейминга минимальная видеокарта — RTX 4060 Ti или RX 7700 XT (если такие "
        "модели фигурируют в данных поиска).\n"
        "- Для Full HD — RTX 3060 / RX 6600.\n"
        "- Указывай конкретные модели и цены ТОЛЬКО если они явно присутствуют в данных поиска.\n"
        "- В конце явно укажи источник (домен), откуда взята цена.\n"
        "- Отвечай на русском языке."
    )
}

cache = {}
history = defaultdict(list)
build_ratings = {}
quiz_sessions = {}
user_last_request = {}
search_cache = {}  # кэш результатов Tavily, чтобы не жечь лимит на одинаковые запросы


def rate_limit(user_id: int) -> bool:
    now = datetime.now()
    last = user_last_request.get(user_id)
    if last and (now - last) < timedelta(seconds=6):
        return False
    user_last_request[user_id] = now
    return True


async def tavily_search(query: str, max_results: int = 6) -> list[dict]:
    """Реальный поиск цен/наличия комплектующих. Возвращает список {title, url, content}."""
    if not TAVILY_API_KEY:
        logger.warning("TAVILY_API_KEY не задан — поиск отключён, ответы будут менее точными.")
        return []

    cache_key = hashlib.md5(query.encode()).hexdigest()
    cached = search_cache.get(cache_key)
    if cached and (datetime.now() - cached["ts"]) < timedelta(hours=6):
        return cached["results"]

    payload = {
        "api_key": TAVILY_API_KEY,
        "query": query,
        "search_depth": "basic",
        "max_results": max_results,
        "include_domains": [
            "dns-shop.ru", "citilink.ru", "regard.ru",
            "market.yandex.ru", "overclockers.ru",
        ],
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(TAVILY_SEARCH_URL, json=payload, timeout=15) as resp:
                data = await resp.json()
                results = [
                    {
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "content": r.get("content", "")[:500],
                    }
                    for r in data.get("results", [])
                ]
                search_cache[cache_key] = {"ts": datetime.now(), "results": results}
                return results
    except Exception as e:
        logger.warning(f"Tavily search failed: {e}")
        return []


def format_search_context(results: list[dict]) -> str:
    if not results:
        return (
            "АКТУАЛЬНЫЕ ДАННЫЕ ИЗ ПОИСКА: поиск не дал результатов или недоступен. "
            "Явно предупреди пользователя, что не можешь подтвердить актуальные цены/модели."
        )
    lines = ["АКТУАЛЬНЫЕ ДАННЫЕ ИЗ ПОИСКА (используй только это, не выдумывай сверх этого):"]
    for r in results:
        lines.append(f"- [{r['url']}] {r['title']}: {r['content']}")
    return "\n".join(lines)


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


async def ask_ai(messages: list, search_query: str) -> str:
    cache_key = get_cache_key(messages) + search_query
    if cache_key in cache:
        return cache[cache_key]

    # 1. Реальный поиск ДО генерации — модель строит ответ на фактах, а не наоборот
    results = await tavily_search(search_query)
    context_block = format_search_context(results)

    grounded_messages = messages[:-1] + [
        {"role": "user", "content": f"{context_block}\n\nЗапрос пользователя: {messages[-1]['content']}"}
    ]

    try:
        # OpenAI SDK синхронный — уводим в отдельный поток, чтобы не блокировать event loop
        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model=OPENROUTER_MODEL,
            messages=grounded_messages,
            temperature=0.3,
            max_tokens=2500,
        )
        answer = resp.choices[0].message.content
        cache[cache_key] = answer
        return answer
    except Exception as e:
        logger.error(f"OpenRouter error: {e}")
        return "Извините, ошибка ИИ. Попробуйте ещё раз через минуту."


bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()


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
    # Поисковый запрос для RAG строим из текста пользователя напрямую
    search_query = f"{user_text} купить цена 2026"
    answer = await ask_ai(history[user_id], search_query)
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


@dp.callback_query(F.data.startswith("alt_"))
async def alt_build(call: CallbackQuery):
    user_id = int(call.data.split("_")[1])
    history[user_id].append({"role": "user", "content": "Предложи альтернативную сборку."})
    answer = await ask_ai(history[user_id], "альтернативная сборка ПК цена 2026")
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
