import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import LabeledPrice

from openai import OpenAI


# ============================================================
# CONFIGURATION
# ============================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "ВашАПИ")
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))

# Telegram Stars
PRICE_15 = 250
PRICE_30 = 500

# AI
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")

DB_PATH = os.getenv("DB_PATH", "bot_users.db")

bot = Bot(token=TELEGRAM_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
client = OpenAI(api_key=OPENAI_API_KEY)


# ============================================================
# PSYCHOLOGIST PROMPT
# ============================================================
SYSTEM_PROMPT = """
Ты — ИИ-психолог-консультант в Telegram. Твоя задача — вести живой,
бережный и содержательный разговор с человеком, помогая ему лучше понять
себя, свои чувства, потребности, внутренние конфликты и возможные варианты
действий.

Твой стиль:
- человеческий, спокойный, тёплый, без канцелярита и шаблонов;
- не используй фразы вроде «всё будет хорошо», «просто отпустите ситуацию»,
  «вам нужно полюбить себя», если они не вытекают из контекста;
- не выдавай длинные универсальные списки советов;
- сначала понимай ситуацию, затем помогай её исследовать;
- задавай обычно ОДИН содержательный вопрос за раз;
- отражай смысл слов пользователя и аккуратно проверяй свои гипотезы;
- помогай человеку самому увидеть связи между событиями, чувствами,
  мыслями, потребностями и поведением;
- если пользователь просит конкретный совет, дай его, но объясни логику
  и предложи выбрать подходящий вариант;
- помни контекст всей текущей сессии и не заставляй человека повторять
  уже рассказанное;
- если человек просто хочет выговориться, не превращай каждый ответ
  в диагностику;
- не ставь диагнозы и не утверждай, что знаешь внутреннее состояние
  человека лучше него самого;
- не называй себя человеком или лицензированным психологом.

Структура хорошего ответа может быть такой:
1) коротко показать, что ты понял;
2) назвать важную деталь или противоречие, если оно действительно есть;
3) задать один вопрос или предложить небольшой следующий шаг.

Не используй эту структуру механически — естественный диалог важнее.

Безопасность:
- если человек сообщает о непосредственной угрозе жизни, намерении
  причинить себе вред или вред другому, не веди обычную терапевтическую
  беседу как будто ничего не произошло. Спокойно признай серьёзность
  ситуации и предложи немедленно обратиться в экстренные службы,
  к близкому человеку или другому доступному специалисту.
- не давай опасных медицинских или юридических указаний.

Главная цель: не «дать правильный совет», а помочь человеку яснее
увидеть свою ситуацию и сделать следующий осмысленный шаг.
""".strip()


class SessionStates(StatesGroup):
    chatting = State()


class BroadcastStates(StatesGroup):
    waiting_for_message = State()


# ============================================================
# DATABASE
# ============================================================
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                join_date TEXT NOT NULL,
                free_session_start TEXT,
                session_end TEXT,
                session_type TEXT DEFAULT 'none'
            )
        """)

        cur.execute("PRAGMA table_info(users)")
        existing_columns = {row[1] for row in cur.fetchall()}

        if "free_session_start" not in existing_columns:
            cur.execute("ALTER TABLE users ADD COLUMN free_session_start TEXT")
            if "free_session_date" in existing_columns:
                cur.execute("""
                    UPDATE users
                    SET free_session_start = free_session_date
                    WHERE free_session_start IS NULL
                """)

        if "session_end" not in existing_columns:
            cur.execute("ALTER TABLE users ADD COLUMN session_end TEXT")

        if "session_type" not in existing_columns:
            cur.execute(
                "ALTER TABLE users ADD COLUMN session_type TEXT DEFAULT 'none'"
            )

        cur.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                telegram_payment_charge_id TEXT,
                payload TEXT NOT NULL,
                stars INTEGER NOT NULL,
                minutes INTEGER NOT NULL,
                payment_date TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.commit()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_dt(value):
    if not value:
        return None
    return datetime.fromisoformat(value)


def save_user_start(user_id, username, first_name):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        now = now_iso()
        cur.execute("""
            INSERT INTO users (user_id, username, first_name, join_date)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name
        """, (user_id, username, first_name, now))
        conn.commit()


def get_user(user_id):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
    
    if not row:
        return None
    return {
        "user_id": row[0],
        "username": row[1],
        "first_name": row[2],
        "join_date": row[3],
        "free_session_start": row[4],
        "session_end": row[5],
        "session_type": row[6],
    }


def start_free_session_db(user_id):
    start = datetime.now(timezone.utc)
    end = start + timedelta(minutes=15)
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE users
            SET free_session_start=?, session_end=?, session_type='free'
            WHERE user_id=? AND free_session_start IS NULL
        """, (start.isoformat(), end.isoformat(), user_id))
        conn.commit()
    return start, end


def has_used_free_session(user_id):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT free_session_start FROM users WHERE user_id=?", (user_id,)
        )
        row = cur.fetchone()
    return bool(row and row[0])


def set_session(user_id, minutes, session_type):
    current = get_user(user_id)
    now = datetime.now(timezone.utc)

    current_end = parse_dt(current["session_end"]) if current else None
    if current_end and current_end > now:
        end = current_end + timedelta(minutes=minutes)
    else:
        end = now + timedelta(minutes=minutes)

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE users
            SET session_end=?, session_type=?
            WHERE user_id=?
        """, (end.isoformat(), session_type, user_id))
        conn.commit()
    return end


def log_payment(user_id, charge_id, payload, stars, minutes):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO payments
            (user_id, telegram_payment_charge_id, payload, stars, minutes, payment_date)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, charge_id, payload, stars, minutes, now_iso()))
        conn.commit()


def payment_exists(charge_id):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM payments WHERE telegram_payment_charge_id=?",
            (charge_id,)
        )
        result = cur.fetchone()
    return bool(result)


def save_message(user_id, role, content):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO messages (user_id, role, content, created_at)
            VALUES (?, ?, ?, ?)
        """, (user_id, role, content, now_iso()))
        conn.commit()


def load_history(user_id):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT role, content
            FROM messages
            WHERE user_id=?
            ORDER BY id ASC
        """, (user_id,))
        rows = cur.fetchall()
    return [{"role": r, "content": c} for r, c in rows]


def get_stats():
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM users")
        total_users = cur.fetchone()[0]

        cur.execute("""
            SELECT COUNT(*) FROM users
            WHERE free_session_start IS NOT NULL
        """)
        started_free = cur.fetchone()[0]

        cur.execute("""
            SELECT COUNT(*) FROM users
            WHERE session_end IS NOT NULL AND session_end > ?
        """, (now_iso(),))
        active = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM payments")
        payments = cur.fetchone()[0]

        cur.execute("SELECT COALESCE(SUM(stars),0) FROM payments")
        stars = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM payments WHERE minutes=15")
        paid15 = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM payments WHERE minutes=30")
        paid30 = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM messages")
        messages = cur.fetchone()[0]

        today = datetime.now(timezone.utc).date().isoformat()
        cur.execute("SELECT COUNT(*) FROM users WHERE substr(join_date,1,10)=?", (today,))
        new_today = cur.fetchone()[0]

    return {
        "total_users": total_users,
        "started_free": started_free,
        "active": active,
        "payments": payments,
        "stars": stars,
        "paid15": paid15,
        "paid30": paid30,
        "messages": messages,
        "new_today": new_today,
    }


# ============================================================
# KEYBOARDS
# ============================================================
def main_keyboard():
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(
                text="🧠 Начать разговор", callback_data="start_free"
            )
        ],
        [
            types.InlineKeyboardButton(
                text="⏱ Моя сессия", callback_data="session_info"
            ),
            types.InlineKeyboardButton(
                text="⭐ Продлить", callback_data="buy_menu"
            )
        ],
        [
            types.InlineKeyboardButton(
                text="ℹ️ Как это работает", callback_data="about"
            )
        ],
    ])


def payment_keyboard():
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(
                text="⭐ 15 минут — 250", callback_data="buy_15"
            )
        ],
        [
            types.InlineKeyboardButton(
                text="⭐ 30 минут — 500", callback_data="buy_30"
            )
        ],
        [
            types.InlineKeyboardButton(
                text="↩️ В меню", callback_data="menu"
            )
        ],
    ])


# ============================================================
# NOTIFICATIONS TO ADMIN (TELEGRAM)
# ============================================================
def build_transcript(user_id):
    user = get_user(user_id)
    history = load_history(user_id)

    lines = [
        f"Пользователь: {user['first_name'] if user else 'Неизвестен'}",
        f"Username: {user['username'] if user else '—'}",
        f"Telegram ID: {user_id}",
        "",
        "===== ДИАЛОГ =====",
    ]

    for item in history:
        role = "Пользователь" if item["role"] == "user" else "Психолог"
        lines.append(f"{role}:")
        lines.append(item["content"])
        lines.append("")

    return "\n".join(lines)


async def send_transcript_to_admin(user_id, subject_suffix="завершение сессии"):
    """Отправляет транскрипт диалога администратору в Telegram."""
    try:
        transcript_text = build_transcript(user_id)
        user = get_user(user_id)
        user_name = user['first_name'] if user else 'Неизвестен'
        
        header = f"📬 **Транскрипт сессии**\nПользователь: {user_name} (ID: `{user_id}`)\nСтатус: {subject_suffix}\n\n"
        full_message = header + transcript_text

        # Разбиваем сообщение, если оно превышает лимит Telegram (4096 символов)
        if len(full_message) <= 4096:
            await bot.send_message(ADMIN_ID, full_message, parse_mode="Markdown")
        else:
            for i in range(0, len(full_message), 4000):
                await bot.send_message(ADMIN_ID, full_message[i:i+4000])

        return True
    except Exception:
        logging.exception("Could not send transcript to admin Telegram")
        return False


# ============================================================
# HELPERS
# ============================================================
def session_remaining(user_id):
    user = get_user(user_id)
    if not user or not user["session_end"]:
        return None

    end = parse_dt(user["session_end"])
    remaining = end - datetime.now(timezone.utc)
    if remaining.total_seconds() <= 0:
        return None
    return remaining


def session_text(user_id):
    remaining = session_remaining(user_id)
    if not remaining:
        return (
            "⏰ Сейчас активной сессии нет.\n\n"
            "Можно начать бесплатную сессию, если вы ещё её не использовали, "
            "или приобрести продолжение."
        )

    total_seconds = int(remaining.total_seconds())
    minutes, seconds = divmod(total_seconds, 60)
    return f"⏱ До окончания сессии: **{minutes} мин. {seconds:02d} сек.**"


async def ensure_user(message):
    user_id = message.from_user.id
    username = (
        f"@{message.from_user.username}"
        if message.from_user.username else "нет юзернейма"
    )
    first_name = message.from_user.first_name or "Без имени"
    save_user_start(user_id, username, first_name)


# ============================================================
# ADMIN BROADCAST
# ============================================================
def get_all_user_ids():
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users")
        return [row[0] for row in cur.fetchall()]


async def broadcast_message(text):
    """Отправляет сообщение всем пользователям, сохранённым в базе."""
    user_ids = get_all_user_ids()
    sent = 0
    failed = 0

    for user_id in user_ids:
        try:
            await bot.send_message(user_id, text)
            sent += 1
        except Exception:
            failed += 1
            logging.exception("Broadcast failed for user %s", user_id)

        # Небольшая пауза, чтобы не упираться в лимиты Telegram.
        await asyncio.sleep(0.05)

    return len(user_ids), sent, failed


# ============================================================
# COMMANDS / MENU
# ============================================================
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await ensure_user(message)
    await state.clear()

    await message.answer(
        "Здравствуйте. Я ИИ-психолог для спокойного разговора о том, "
        "что вас действительно волнует.\n\n"
        "Вы можете рассказать о ситуации своими словами — без "
        "правильных формулировок и без необходимости сразу знать, "
        "что именно с вами происходит.\n\n"
        "Первая сессия — **15 минут бесплатно**.",
        reply_markup=main_keyboard(),
        parse_mode="Markdown",
    )


@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("У вас нет доступа к этой команде.")
        return

    s = get_stats()
    text = (
        "📊 **Статистика psyTrem**\n\n"
        f"👥 Всего пользователей: **{s['total_users']}**\n"
        f"🆕 Новых сегодня: **{s['new_today']}**\n"
        f"🟢 Активных сессий сейчас: **{s['active']}**\n"
        f"▶️ Запустили бесплатную сессию: **{s['started_free']}**\n\n"
        f"💳 Всего оплат: **{s['payments']}**\n"
        f"⭐ Получено Stars: **{s['stars']}**\n"
        f"⏱ Покупок по 15 минут: **{s['paid15']}**\n"
        f"⏱ Покупок по 30 минут: **{s['paid30']}**\n\n"
        f"💬 Сообщений в истории: **{s['messages']}**"
    )

    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(
            text="📢 Отправить сообщение пользователям",
            callback_data="admin_broadcast"
        )]
    ])

    await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")


@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    await state.set_state(BroadcastStates.waiting_for_message)
    await callback.message.answer(
        "📢 **Рассылка пользователям**\n\n"
        "Отправьте следующим сообщением текст, который нужно разослать "
        "всем пользователям, которые запускали бота.\n\n"
        "Для отмены используйте /cancel.",
        parse_mode="Markdown"
    )
    await callback.answer()


@dp.message(Command("cancel"))
async def admin_broadcast_cancel(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    current_state = await state.get_state()
    if current_state == BroadcastStates.waiting_for_message.state:
        await state.clear()
        await message.answer("❌ Рассылка отменена.")


@dp.message(BroadcastStates.waiting_for_message, F.text)
async def admin_broadcast_send(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await state.clear()
        return

    broadcast_text = message.text.strip()
    if not broadcast_text:
        await message.answer("Сообщение пустое. Отправьте текст ещё раз или /cancel.")
        return

    await state.clear()

    total, sent, failed = await broadcast_message(broadcast_text)

    await message.answer(
        "📢 **Рассылка завершена.**\n\n"
        f"👥 Получателей в базе: **{total}**\n"
        f"✅ Доставлено: **{sent}**\n"
        f"❌ Не доставлено: **{failed}**",
        parse_mode="Markdown"
    )


# ============================================================
# CALLBACKS
# ============================================================
@dp.callback_query(F.data == "menu")
async def menu(callback: types.CallbackQuery):
    await callback.message.answer(
        "Главное меню:", reply_markup=main_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data == "about")
async def about(callback: types.CallbackQuery):
    await callback.message.answer(
        "🧠 **Как это работает**\n\n"
        "Вы пишете о том, что вас волнует, а я помогаю разобраться "
        "в ситуации через диалог и вопросы.\n\n"
        "Первая сессия — 15 минут бесплатно.\n"
        "После неё можно продолжить:\n"
        "• 15 минут — 250 ⭐\n"
        "• 30 минут — 500 ⭐\n\n"
        "Это ИИ-консультант, а не замена очной помощи специалиста.",
        reply_markup=main_keyboard(),
        parse_mode="Markdown",
    )
    await callback.answer()


@dp.callback_query(F.data == "session_info")
async def session_info(callback: types.CallbackQuery):
    await callback.message.answer(
        session_text(callback.from_user.id),
        reply_markup=main_keyboard(),
        parse_mode="Markdown",
    )
    await callback.answer()


@dp.callback_query(F.data == "buy_menu")
async def buy_menu(callback: types.CallbackQuery):
    await callback.message.answer(
        "⭐ **Выберите продолжительность:**\n\n"
        "15 минут — 250 Stars\n"
        "30 минут — 500 Stars",
        reply_markup=payment_keyboard(),
        parse_mode="Markdown",
    )
    await callback.answer()


@dp.callback_query(F.data == "start_free")
async def start_free_session(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    save_user_start(
        user_id,
        f"@{callback.from_user.username}" if callback.from_user.username else "нет юзернейма",
        callback.from_user.first_name or "Без имени",
    )

    if has_used_free_session(user_id):
        remaining = session_remaining(user_id)
        if remaining:
            await callback.message.answer(
                "У вас уже есть активная сессия.\n\n" + session_text(user_id),
                reply_markup=main_keyboard(),
            )
        else:
            await callback.message.answer(
                "Бесплатная 15-минутная сессия уже была использована.\n\n"
                "Чтобы продолжить разговор, выберите время:",
                reply_markup=payment_keyboard(),
            )
        await callback.answer()
        return

    start_free_session_db(user_id)
    await state.set_state(SessionStates.chatting)

    await callback.message.answer(
        "🟢 **Сессия началась — 15 минут.**\n\n"
        "Расскажите, с чем вы пришли сегодня. Можно начать с того, "
        "что сейчас занимает ваши мысли сильнее всего.",
        parse_mode="Markdown",
    )
    await callback.answer()


# ============================================================
# CHAT
# ============================================================
@dp.message(SessionStates.chatting, F.text)
async def handle_chat(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    remaining = session_remaining(user_id)

    if not remaining:
        await state.clear()
        # Отправка транскрипта админу в Telegram
        await send_transcript_to_admin(
            user_id, "сессия завершена — транскрипт"
        )
        await message.answer(
            "⏰ **Время сессии закончилось.**\n\n"
            "Если хотите продолжить разговор, выберите время:",
            reply_markup=payment_keyboard(),
            parse_mode="Markdown",
        )
        return

    text = message.text.strip()
    if not text:
        return

    save_message(user_id, "user", text)

    recent_history = load_history(user_id)[-40:]
    while recent_history and recent_history[0]["role"] != "user":
        recent_history.pop(0)

    history = [{"role": "system", "content": SYSTEM_PROMPT}] + recent_history

    try:
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model=OPENAI_MODEL,
            messages=history,
            temperature=0.7,
        )
        ai_reply = response.choices[0].message.content.strip()

        save_message(user_id, "assistant", ai_reply)
        await message.answer(ai_reply)

    except Exception:
        logging.exception("OpenAI request failed")
        await message.answer(
            "Сейчас возникла техническая проблема с ответом. "
            "Пожалуйста, отправьте сообщение ещё раз."
        )


@dp.message(F.text)
async def handle_text_without_session(message: types.Message):
    user_id = message.from_user.id
    save_user_start(
        user_id,
        f"@{message.from_user.username}" if message.from_user.username else "нет юзернейма",
        message.from_user.first_name or "Без имени",
    )

    await message.answer(
        "Сейчас активной сессии нет. Выберите действие:",
        reply_markup=main_keyboard(),
    )


# ============================================================
# PAYMENTS
# ============================================================
def invoice_for(minutes):
    if minutes == 15:
        return {
            "title": "Продолжение разговора — 15 минут",
            "description": "Дополнительные 15 минут разговора с ИИ-психологом",
            "stars": PRICE_15,
            "payload": "psy_session_15",
        }
    return {
        "title": "Продолжение разговора — 30 минут",
        "description": "Дополнительные 30 минут разговора с ИИ-психологом",
        "stars": PRICE_30,
        "payload": "psy_session_30",
    }


async def send_invoice(callback: types.CallbackQuery, minutes):
    item = invoice_for(minutes)

    await callback.message.answer_invoice(
        title=item["title"],
        description=item["description"],
        prices=[LabeledPrice(label=f"{minutes} минут", amount=item["stars"])],
        provider_token="",
        payload=item["payload"],
        currency="XTR",
    )
    await callback.answer()


@dp.callback_query(F.data == "buy_15")
async def buy_15(callback: types.CallbackQuery):
    await send_invoice(callback, 15)


@dp.callback_query(F.data == "buy_30")
async def buy_30(callback: types.CallbackQuery):
    await send_invoice(callback, 30)


@dp.pre_checkout_query()
async def pre_checkout_query_handler(pre_checkout_query: types.PreCheckoutQuery):
    if pre_checkout_query.invoice_payload not in {
        "psy_session_15",
        "psy_session_30",
    }:
        await bot.answer_pre_checkout_query(
            pre_checkout_query.id,
            ok=False,
            error_message="Неизвестный тариф.",
        )
        return

    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@dp.message(F.successful_payment)
async def successful_payment_handler(message: types.Message, state: FSMContext):
    payment = message.successful_payment
    user_id = message.from_user.id

    if payment_exists(payment.telegram_payment_charge_id):
        await message.answer(
            "Этот платёж уже был обработан. Если доступ не появился, "
            "напишите в поддержку."
        )
        return

    payload = payment.invoice_payload

    if payload == "psy_session_15":
        minutes = 15
        stars = PRICE_15
    elif payload == "psy_session_30":
        minutes = 30
        stars = PRICE_30
    else:
        await message.answer(
            "Платёж получен, но тариф не удалось определить. "
            "Пожалуйста, обратитесь в поддержку."
        )
        return

    log_payment(
        user_id,
        payment.telegram_payment_charge_id,
        payload,
        stars,
        minutes,
    )

    end = set_session(user_id, minutes, "paid")
    await state.set_state(SessionStates.chatting)

    await message.answer(
        f"✅ **Оплата прошла!**\n\n"
        f"Доступ открыт ещё на **{minutes} минут**.\n"
        f"Можно продолжать разговор прямо сейчас.\n\n"
        f"⏱ Сессия закончится примерно в: "
        f"{end.astimezone().strftime('%H:%M')}",
        reply_markup=main_keyboard(),
        parse_mode="Markdown",
    )

    # Отправка транскрипта админу в Telegram при успешной оплате
    await send_transcript_to_admin(
        user_id, f"успешная оплата — {minutes} минут"
    )


# ============================================================
# START
# ============================================================
async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    init_db()
    logging.info("psyTrem bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
