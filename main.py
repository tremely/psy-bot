import asyncio
from datetime import datetime, timedelta
import logging
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import requests

# ВАШИ ТОКЕНЫ И ID АДМИНИСТРАТОРА (сюда приходят отчеты)
TELEGRAM_TOKEN = "ВАШ_ТОКЕН_БОТА"
OPENAI_API_KEY = "ВАШ_КЛЮЧ_OPENAI"
ADMIN_ID = 543884011  # Ваш Telegram ID

# Инициализация
bot = Bot(token=TELEGRAM_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Системный промпт для психолога
SYSTEM_PROMPT = (
    "Ты — профессиональный, эмпатичный и этичный психолог. Ты практикуешь глубокий терапевтический подход. "
    "Никогда не давай поверхностных советов. Вместо этого используй открытые вопросы, отражай чувства клиента, "
    "помогай ему замечать когнитивные искажения. Веди диалог последовательно, по одному вопросу за раз, "
    "не перегружай клиента текстом. Держи фокус на проблеме клиента."
)

# Хранилище сессий пользователей
users_sessions = {}


class SessionStates(StatesGroup):
  chatting = State()


@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
  user_id = message.from_user.id
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
      "Здравствуйте. Я ваш виртуальный психолог. Я помогаю разобраться в "
      "тревогах, личных границах и сложных жизненных ситуациях.\n\nПервая "
      "ознакомительная сессия (30 минут) — **абсолютно бесплатная**. За это "
      "время мы наметим корень проблемы.\n\nНажмите кнопку ниже, когда будете "
      "готовы начать.",
      reply_markup=keyboard,
      parse_mode="Markdown",
  )


@dp.callback_query(F.data == "start_free")
async def start_free_session(callback: types.CallbackQuery, state: FSMContext):
  user_id = callback.from_user.id
  now = datetime.now()

  users_sessions[user_id]["status"] = "free"
  users_sessions[user_id]["start_time"] = now
  users_sessions[user_id]["end_time"] = now + timedelta(
      minutes=30
  )  # Таймер на 30 минут

  await state.set_state(SessionStates.chatting)
  await callback.message.answer(
      "Сессия началась. Таймер запущен на 30 минут.\n\nРасскажите, с каким"
      " запросом вы сегодня пришли? Что вас беспокоит?"
  )
  await callback.answer()


@dp.message(SessionStates.chatting, F.text)
async def handle_chat(message: types.Message, state: FSMContext):
  user_id = message.from_user.id
  username = (
      f"@{message.from_user.username}"
      if message.from_user.username
      else f"ID: {user_id}"
  )
  user_data = users_sessions.get(
      user_id,
      {
          "status": "none",
          "history": [{"role": "system", "content": SYSTEM_PROMPT}],
      },
  )

  # Проверяем тайминг (30 минут)
  if user_data.get("status") == "free":
    if datetime.now() > user_data["end_time"]:
      user_data["status"] = "expired"

      pay_keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[
          types.InlineKeyboardButton(
              text="💳 Оплатить продление сессии (30 мин)",
              callback_data="buy_session",
          )
      ]])

      await message.answer(
          "⏰ **Время нашей бесплатной сессии подошло к концу.**\nМы успели"
          " наметить важные зоны для работы. Чтобы продолжить глубокую"
          " проработку проблемы еще на 30 минут, пожалуйста, оформите"
          " продолжение сессии.",
          reply_markup=pay_keyboard,
          parse_mode="Markdown",
      )
      return

  if user_data.get("status") == "expired":
    await message.answer(
        "Пожалуйста, оплатите продолжение сессии, чтобы мы могли продолжить"
        " разговор."
    )
    return

  # Добавляем сообщение пользователя в историю
  user_data["history"].append({"role": "user", "content": message.text})

  # 🔔 ПЕРЕСЫЛКА СООБЩЕНИЯ КЛИЕНТА АДМИНУ
  try:
    await bot.send_message(
        ADMIN_ID,
        f"📩 **Сообщение от пользователя {username}:**\n{message.text}",
        parse_mode="Markdown",
    )
  except Exception:
    pass

  try:
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "gpt-4o-mini",
        "messages": user_data["history"],
        "temperature": 0.7,
    }
    response = requests.post(
        "https://api.openai.com/v1/chat/completions",
        json=payload,
        headers=headers,
        timeout=30,
    )
    res_json = response.json()
    ai_reply = res_json["choices"][0]["message"]["content"]

    # Добавляем ответ бота в историю
    user_data["history"].append({"role": "assistant", "content": ai_reply})

    # 🤖 ПЕРЕСЫЛКА ОТВЕТА БОТА АДМИНУ
    try:
      await bot.send_message(
          ADMIN_ID,
          f"🧠 **Ответ психолога пользователю {username}:**\n{ai_reply}",
          parse_mode="Markdown",
      )
    except Exception:
      pass

    await message.answer(ai_reply)
  except Exception as e:
    await message.answer(
        "Произошла небольшая техническая ошибка при обращении к нейросети."
        " Попробуйте отправить сообщение еще раз."
    )


@dp.callback_query(F.data == "buy_session")
async def process_payment(callback: types.CallbackQuery):
  user_id = callback.from_user.id
  users_sessions[user_id]["status"] = "paid"
  users_sessions[user_id]["end_time"] = datetime.now() + timedelta(minutes=30)

  await callback.message.answer(
      "✅ Оплата успешно симулирована! Продленная сессия активирована еще на 30"
      " минут. Продолжайте общение."
  )
  await callback.answer()


async def main():
  logging.basicConfig(level=logging.INFO)
  await dp.start_polling(bot)


if __name__ == "__main__":
  asyncio.run(main())
