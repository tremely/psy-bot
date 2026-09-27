import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import LabeledPrice
from openai import OpenAI

# ЗАМЕНИТЕ ЭТИ СТРОКИ НА СВОИ ДАННЫЕ:
TELEGRAM_TOKEN = "PUT_YOUR_KEY_HERE"
OPENAI_API_KEY = "PUT_YOUR_KEY_HERE"
ADMIN_ID = PUT_YOUR_KEY_HERE  # ВПИШИТЕ СЮДА СВОЙ ЧИСЛОВОЙ TELEGRAM ID ДЛЯ АДМИНКИ

# Стоимость продления сессии на 30 минут в Telegram Stars
SESSION_STARS_PRICE = 550

# Инициализация
bot = Bot(token=TELEGRAM_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
client = OpenAI(api_key=OPENAI_API_KEY)

# Психологический системный промпт
SYSTEM_PROMPT = (
    "Ты — профессиональный, эмпатичный и этичный психолог. Ты практикуешь глубокий терапевтический подход. "
    "Никогда не давай поверхностных советов. Вместо этого используй открытые вопросы, отражай чувства клиента, "
    "помогай ему замечать когнитивные искажения. Веди диалог последовательно, по одному вопросу за раз, "
    "не перегружай клиента текстом. Держи фокус на проблеме клиента."
)

# Оперативная память для сессий и истории диалогов
users_sessions = {}


# --- РАБОТА С БАЗОЙ ДАННЫХ (SQLite) ---
def init_db():
  conn = sqlite3.connect("bot_users.db")
  cursor = conn.cursor()
  # Таблица пользователей
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            join_date TEXT,
            free_session_date TEXT
        )
    """)
  # Таблица оплаченных сессий (история)
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS paid_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            payment_date TEXT
        )
    """)
  conn.commit()
  conn.close()


def save_user_start(user_id: int, username: str, first_name: str):
  conn = sqlite3.connect("bot_users.db")
  cursor = conn.cursor()
  now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
  # Сохраняем пользователя, если его еще нет, или обновляем юзернейм
  cursor.execute(
      """
        INSERT INTO users (user_id, username, first_name, join_date) 
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name
    """,
      (user_id, username, first_name, now),
  )
  conn.commit()
  conn.close()


def save_free_session_time(user_id: int):
  conn = sqlite3.connect("bot_users.db")
  cursor = conn.cursor()
  now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
  cursor.execute(
      """
        UPDATE users SET free_session_date = ? WHERE user_id = ? AND free_session_date IS NULL
    """,
      (now, user_id),
  )
  conn.commit()
  conn.close()


def log_paid_session(user_id: int):
  conn = sqlite3.connect("bot_users.db")
  cursor = conn.cursor()
  now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
  cursor.execute(
      "INSERT INTO paid_sessions (user_id, payment_date) VALUES (?, ?)",
      (user_id, now),
  )
  conn.commit()
  conn.close()


def get_all_users_report():
  conn = sqlite3.connect("bot_users.db")
  cursor = conn.cursor()
  cursor.execute(
      "SELECT user_id, username, first_name, join_date, free_session_date FROM"
      " users"
  )
  users = cursor.fetchall()
  report_data = []
  for u in users:
    u_id, uname, fname, j_date, f_date = u
    # Получаем все платные сессии пользователя
    cursor.execute(
        "SELECT payment_date FROM paid_sessions WHERE user_id = ?", (u_id,)
    )
    payments = [p[0] for p in cursor.fetchall()]
    report_data.append({
        "user_id": u_id,
        "username": uname,
        "first_name": fname,
        "join_date": j_date or "Не зафиксировано",
        "free_date": f_date or "Еще не начинал",
        "payments": payments,
    })
  conn.close()
  return report_data


class SessionStates(StatesGroup):
  chatting = State()


@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
  user_id = message.from_user.id
  username = (
      f"@{message.from_user.username}"
      if message.from_user.username
      else "нет юзернейма"
  )
  first_name = message.from_user.first_name or "Без имени"

  # Сохраняем факт первого запуска в базу
  save_user_start(user_id, username, first_name)

  users_sessions[user_id] = {
      "status": "none",
      "history": [{"role": "system", "content": SYSTEM_PROMPT}],
  }

  keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[
      types.InlineKeyboardButton(
          text="🧠 Начать бесплатную сессию (30 минут)",
          callback_data="start_free",
      )
  ]])

  await message.answer(
      "Здравствуйте. Я ваш персональный ИИ-психолог. Я помогаю бережно"
      " разобраться в личных границах, тревогах и тупиковых ситуациях.\n\nПервая"
      " ознакомительная сессия (30 минут) — **абсолютно бесплатная**. За это"
      " время мы наметим корень проблемы.\n\nНажмите кнопку ниже, когда будете"
      " готовы начать.",
      reply_markup=keyboard,
      parse_mode="Markdown",
  )


# --- АДМИН-ПАНЕЛЬ ---
@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
  if message.from_user.id != ADMIN_ID:
    await message.answer("У вас нет доступа к этой команде.")
    return

  users_report = get_all_users_report()
  if not users_report:
    await message.answer("В базе пока нет зарегистрированных пользователей.")
    return

  text = (
      f"📊 **Админ-панель: Всего пользователей в базе: {len(users_report)}**\n\n"
  )
  for u in users_report:
    text += f"👤 **Имя:** {u['first_name']}\n"
    text += f"🔗 **Юзернейм:** {u['username']}\n"
    text += f"🆔 **ID:** `{u['user_id']}`\n"
    text += f"📥 **Запуск бота:** {u['join_date']}\n"
    text += f"⏱ **Старт бесплатной сессии:** {u['free_date']}\n"

    if u["payments"]:
      text += "💳 **Оплаченные сессии:**\n"
      for p_date in u["payments"]:
        text += f"   • {p_date}\n"
    else:
      text += "💳 **Оплаченные сессии:** нет\n"
    text += "-------------------\n"

  if len(text) > 4000:
    for x in range(0, len(text), 4000):
      await message.answer(text[x : x + 4000], parse_mode="Markdown")
  else:
    await message.answer(text, parse_mode="Markdown")


@dp.callback_query(F.data == "start_free")
async def start_free_session(callback: types.CallbackQuery, state: FSMContext):
  user_id = callback.from_user.id
  now = datetime.now()

  # Фиксируем время старта бесплатной сессии в базе
  save_free_session_time(user_id)

  if user_id not in users_sessions:
    users_sessions[user_id] = {
        "history": [{"role": "system", "content": SYSTEM_PROMPT}]
    }

  users_sessions[user_id]["status"] = "free"
  users_sessions[user_id]["start_time"] = now
  users_sessions[user_id]["end_time"] = now + timedelta(minutes=30)

  await state.set_state(SessionStates.chatting)
  await callback.message.answer(
      "Сессия началась. Таймер запущен на 30 минут.\n\nРасскажите, с каким"
      " запросом вы сегодня пришли? Что вас беспокоит?"
  )
  await callback.answer()


@dp.message(SessionStates.chatting, F.text)
async def handle_chat(message: types.Message, state: FSMContext):
  user_id = message.from_user.id
  user_data = users_sessions.get(
      user_id,
      {
          "status": "none",
          "history": [{"role": "system", "content": SYSTEM_PROMPT}],
      },
  )

  # Проверяем тайминг для бесплатных пользователей
  if user_data.get("status") == "free":
    if datetime.now() > user_data["end_time"]:
      user_data["status"] = "expired"

      pay_keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[
          types.InlineKeyboardButton(
              text="⭐️ Оплатить 30 минут (150 звезд)", callback_data="buy_session"
          )
      ]])

      await message.answer(
          "⏰ **Время нашей бесплатной сессии подошло к концу.**\nМы успели"
          " наметить важные зоны для работы. Чтобы продолжить глубокую"
          " проработку проблемы еще на 30 минут, пожалуйста, оплатите"
          " продолжение сессии.",
          reply_markup=pay_keyboard,
          parse_mode="Markdown",
      )
      return

  if user_data.get("status") == "expired":
    await message.answer(
        "Пожалуйста, оплатите продолжение сессии через кнопку выше, чтобы мы"
        " могли продолжить разговор."
    )
    return

  # Добавляем сообщение пользователя в историю (сохраняем контекст)
  user_data["history"].append({"role": "user", "content": message.text})

  try:
    response = client.chat.completions.create(
        model="gpt-4o", messages=user_data["history"], temperature=0.7
    )
    ai_reply = response.choices[0].message.content
    user_data["history"].append({"role": "assistant", "content": ai_reply})
    await message.answer(ai_reply)
  except Exception as e:
    await message.answer(
        "Произошла небольшая техническая ошибка при обращении к нейросети."
        " Попробуйте отправить сообщение еще раз."
    )


# --- ЛОГИКА ОПЛАТЫ ЧЕРЕЗ TELEGRAM STARS ---
@dp.callback_query(F.data == "buy_session")
async def process_buy_session(callback: types.CallbackQuery):
  prices = [LabeledPrice(label="Психологическая сессия 30 мин", amount=SESSION_STARS_PRICE)]
  await callback.message.answer_invoice(
      title="Продление сессии",
      description="Продолжение глубокой психологической проработки на 30 минут",
      prices=prices,
      provider_token="",  # Для Telegram Stars provider_token всегда пустая строка!
      payload="session_30_min",
      currency="XTR",  # Валюта Telegram Stars
  )
  await callback.answer()


# Предварительная проверка счета
@dp.pre_checkout_query()
async def pre_checkout_query_handler(pre_checkout_query: types.PreCheckoutQuery):
  await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


# Успешная оплата
@dp.message(F.successful_payment)
async def successful_payment_handler(message: types.Message):
  user_id = message.from_user.id
  
  # Логируем платную сессию в базу данных
  log_paid_session(user_id)

  if user_id not in users_sessions:
    users_sessions[user_id] = {
        "history": [{"role": "system", "content": SYSTEM_PROMPT}]
    }

  # Продлеваем сессию на 30 минут, не удаляя историю диалога (контекст сохраняется)
  users_sessions[user_id]["status"] = "paid"
  users_sessions[user_id]["end_time"] = datetime.now() + timedelta(minutes=30)

  await message.answer(
      "✅ **Оплата прошла успешно!** Сессия продлена еще на 30 минут. Мы"
      " продолжаем нашу работу с того же места. О чем вы бы хотели рассказать"
      " дальше?",
      parse_mode="Markdown",
  )


async def main():
  init_db()
  logging.basicConfig(level=logging.INFO)
  await dp.start_polling(bot)


if __name__ == "__main__":
  asyncio.run(main())