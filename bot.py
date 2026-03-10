import asyncio
import logging
import os
import json
import re
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, \
    CallbackQuery, FSInputFile, BufferedInputFile
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import aiohttp
import sqlite3
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Настройки
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
API_URL = "http://api.onlysq.ru/ai/v2"

# Логирование
logging.basicConfig(level=logging.INFO)

# Инициализация бота
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Список доступных моделей
AVAILABLE_MODELS = [
    "gpt-5.2-chat", "deepseek-v3", "deepseek-r1",
    "gemini-3-pro", "gemini-3-pro-preview", "gemini-3-flash",
    "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro",
    "gemini-2.0-flash", "gemini-2.0-flash-lite"
]


# Состояния FSM
class CodeModificationStates(StatesGroup):
    waiting_for_code = State()
    waiting_for_request = State()


# База данных
def init_db():
    conn = sqlite3.connect('code_bot.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY, 
                  current_model TEXT DEFAULT 'gemini-3-flash',
                  last_code TEXT,
                  last_filename TEXT)''')
    conn.commit()
    conn.close()


def get_user_model(user_id):
    conn = sqlite3.connect('code_bot.db')
    c = conn.cursor()
    c.execute("SELECT current_model FROM users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else "gemini-3-flash"


def set_user_model(user_id, model):
    conn = sqlite3.connect('code_bot.db')
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO users (user_id, current_model) VALUES (?, ?)", (user_id, model))
    conn.commit()
    conn.close()


def save_user_code(user_id, code, filename):
    conn = sqlite3.connect('code_bot.db')
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO users (user_id, last_code, last_filename) VALUES (?, ?, ?)",
              (user_id, code, filename))
    conn.commit()
    conn.close()


def get_user_code(user_id):
    conn = sqlite3.connect('code_bot.db')
    c = conn.cursor()
    c.execute("SELECT last_code, last_filename FROM users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result if result else (None, None)


# Клавиатуры
def main_keyboard(is_admin=False):
    buttons = [
        [KeyboardButton(text="📝 Изменить код")],
        [KeyboardButton(text="⚙️ Сменить модель"), KeyboardButton(text="ℹ️ Информация")],
        [KeyboardButton(text="🆘 Поддержка")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def models_keyboard(current_model):
    buttons = []
    for i in range(0, len(AVAILABLE_MODELS), 2):
        row = []
        for j in range(i, min(i + 2, len(AVAILABLE_MODELS))):
            model = AVAILABLE_MODELS[j]
            checkmark = "✅ " if model == current_model else ""
            row.append(InlineKeyboardButton(
                text=f"{checkmark}{model}",
                callback_data=f"model_{model}"
            ))
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# AI функции
async def send_ai_request(user_id, code, request_text, model):
    """Отправка запроса к AI для модификации кода"""

    system_prompt = """Ты AI ассистент для модификации кода. 
Пользователь отправит тебе код и запрос на изменение.

ТВОЯ ЗАДАЧА:
1. Проанализировать код
2. Понять что нужно изменить, добавить или удалить
3. Вернуть ТОЛЬКО JSON в следующем формате:

{
  "summary": "Краткое описание что ты изменил/добавил/удалил",
  "changes": [
    {
      "action": "replace",
      "old_code": "точный код который нужно заменить",
      "new_code": "новый код на замену"
    },
    {
      "action": "add_after",
      "marker": "код после которого добавить",
      "new_code": "код для добавления"
    },
    {
      "action": "delete",
      "code_to_delete": "код который нужно удалить"
    }
  ]
}

ВАЖНО:
- Возвращай ТОЛЬКО JSON без комментариев и markdown
- В "old_code" и "marker" указывай ТОЧНУЮ строку из кода
- Будь максимально точным в указании фрагментов
- Действия: "replace", "add_after", "add_before", "delete"
"""

    user_prompt = f"""КОД:
```
{code}
```

ЗАПРОС:
{request_text}

Верни JSON с изменениями."""

    # Формируем историю сообщений
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]

    # Структура запроса как в оригинальном файле
    send = {
        "model": model,
        "request": {
            "messages": messages
        }
    }

    headers = {
        "Authorization": "Bearer openai"
    }

    try:
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            logging.info(f"Отправка запроса к AI API с моделью {model}")

            async with session.post(API_URL, json=send, headers=headers) as response:
                response_text = await response.text()
                logging.info(f"Ответ API (статус {response.status}): {response_text[:300]}")

                if response.status == 200:
                    data = await response.json()

                    # Извлекаем ответ по структуре из оригинального файла
                    ai_response = data['choices'][0]['message']['content']

                    if ai_response:
                        logging.info(f"AI ответ получен: {len(ai_response)} символов")
                        return ai_response
                    else:
                        logging.error(f"Не удалось извлечь ответ из данных: {data}")
                        return None
                else:
                    logging.error(f"API вернул статус {response.status}: {response_text}")
                    return None

    except aiohttp.ClientError as ce:
        logging.error(f"Ошибка соединения с API: {ce}")
        return None
    except Exception as e:
        logging.error(f"Общая ошибка AI запроса: {e}")
        return None


def apply_changes(code, changes_json):
    """Применение изменений к коду"""
    try:
        # Убираем markdown блоки кода если есть
        cleaned_json = changes_json

        # Убираем ```json и ``` если есть
        if "```json" in cleaned_json:
            cleaned_json = re.sub(r'```json\s*', '', cleaned_json)
            cleaned_json = re.sub(r'```\s*$', '', cleaned_json)
        elif "```" in cleaned_json:
            cleaned_json = re.sub(r'```\s*', '', cleaned_json)

        # Пытаемся найти JSON в тексте
        json_match = re.search(r'\{.*\}', cleaned_json, re.DOTALL)
        if json_match:
            cleaned_json = json_match.group()

        logging.info(f"Попытка парсинга JSON: {cleaned_json[:200]}")

        changes = json.loads(cleaned_json)
        modified_code = code
        summary = changes.get("summary", "Изменения применены")

        changes_applied = 0

        for change in changes.get("changes", []):
            action = change.get("action")

            if action == "replace":
                old_code = change.get("old_code", "")
                new_code = change.get("new_code", "")
                if old_code in modified_code:
                    modified_code = modified_code.replace(old_code, new_code, 1)
                    changes_applied += 1
                    logging.info(f"Заменен фрагмент: {old_code[:50]}")
                else:
                    logging.warning(f"Фрагмент для замены не найден: {old_code[:50]}")

            elif action == "add_after":
                marker = change.get("marker", "")
                new_code = change.get("new_code", "")
                if marker in modified_code:
                    parts = modified_code.split(marker, 1)
                    modified_code = parts[0] + marker + "\n" + new_code + parts[1]
                    changes_applied += 1
                    logging.info(f"Добавлен код после: {marker[:50]}")
                else:
                    logging.warning(f"Маркер не найден: {marker[:50]}")

            elif action == "add_before":
                marker = change.get("marker", "")
                new_code = change.get("new_code", "")
                if marker in modified_code:
                    parts = modified_code.split(marker, 1)
                    modified_code = parts[0] + new_code + "\n" + marker + parts[1]
                    changes_applied += 1
                    logging.info(f"Добавлен код перед: {marker[:50]}")
                else:
                    logging.warning(f"Маркер не найден: {marker[:50]}")

            elif action == "delete":
                code_to_delete = change.get("code_to_delete", "")
                if code_to_delete in modified_code:
                    modified_code = modified_code.replace(code_to_delete, "", 1)
                    changes_applied += 1
                    logging.info(f"Удален фрагмент: {code_to_delete[:50]}")
                else:
                    logging.warning(f"Фрагмент для удаления не найден: {code_to_delete[:50]}")

        logging.info(f"Применено изменений: {changes_applied}")

        if changes_applied == 0:
            return False, None, "Не удалось применить ни одного изменения. AI указал несуществующие фрагменты кода."

        return True, modified_code, f"{summary} (применено {changes_applied} изменений)"

    except json.JSONDecodeError as je:
        logging.error(f"Ошибка JSON: {je}")
        return False, None, f"Ошибка: AI вернул некорректный JSON формат. {str(je)}"
    except Exception as e:
        logging.error(f"Ошибка применения изменений: {e}")
        return False, None, f"Ошибка применения изменений: {str(e)}"


# Обработчики команд
@dp.message(CommandStart())
async def cmd_start(message: Message):
    is_admin = message.from_user.id == ADMIN_ID

    await message.answer(
        "<blockquote>👋 Добро пожаловать в AI Code Editor!</blockquote>\n\n"
        "⚙️ <b>Возможности бота:</b>\n\n"
        "<blockquote>📝 <b>Я помогу изменить ваш код с помощью AI</b></blockquote>\n\n"
        "🔹 Отправьте файл с кодом\n"
        "🔹 Опишите что нужно изменить\n"
        "🔹 Получите готовый файл\n\n"
        "<blockquote>👇 Выберите действие:</blockquote>",
        reply_markup=main_keyboard(is_admin),
        parse_mode="HTML"
    )


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    conn = sqlite3.connect('code_bot.db')
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    conn.close()

    await message.answer(
        "👨‍💼 <b>Админ панель</b>\n\n"
        f"<blockquote>👥 Пользователей: {total_users}</blockquote>",
        parse_mode="HTML"
    )


# Обработчик кнопки "Изменить код"
@dp.message(F.text == "📝 Изменить код")
async def start_code_modification(message: Message, state: FSMContext):
    await message.answer(
        "📂 <b>Отправьте файл с кодом</b>\n\n"
        "<blockquote>📎 Прикрепите файл (.py, .js, .html, .txt и т.д.)</blockquote>\n\n"
        "❌ Для отмены введите /cancel",
        parse_mode="HTML"
    )
    await state.set_state(CodeModificationStates.waiting_for_code)


# Отмена операции
@dp.message(Command("cancel"))
async def cancel_operation(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state:
        await state.clear()
        await message.answer(
            "❌ <b>Операция отменена</b>\n\n"
            "<blockquote>Возвращаемся в главное меню</blockquote>",
            parse_mode="HTML"
        )
    else:
        await message.answer("Нет активных операций для отмены")


# Получение файла
@dp.message(CodeModificationStates.waiting_for_code, F.document)
async def receive_code_file(message: Message, state: FSMContext):
    document = message.document

    try:
        # Скачиваем файл
        file = await bot.get_file(document.file_id)
        file_path = file.file_path

        # Читаем содержимое
        downloaded_file = await bot.download_file(file_path)
        code_content = downloaded_file.read().decode('utf-8')

        # Сохраняем в БД
        save_user_code(message.from_user.id, code_content, document.file_name)

        await message.answer(
            f"✅ <b>Файл получен!</b>\n\n"
            f"<blockquote>📄 Имя: {document.file_name}\n"
            f"📦 Размер: {len(code_content)} символов</blockquote>\n\n"
            "💬 <b>Теперь опишите что нужно изменить:</b>\n\n"
            "<blockquote>Например:\n"
            "• Добавь функцию для...\n"
            "• Измени переменную X на Y\n"
            "• Удали функцию Z\n"
            "• Исправь ошибку в...</blockquote>\n\n"
            "❌ Для отмены введите /cancel",
            parse_mode="HTML"
        )
        await state.set_state(CodeModificationStates.waiting_for_request)
    except UnicodeDecodeError:
        await message.answer(
            "❌ <b>Ошибка чтения файла</b>\n\n"
            "<blockquote>Файл не является текстовым или имеет неподдерживаемую кодировку</blockquote>\n\n"
            "Попробуйте другой файл",
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"Ошибка обработки файла: {e}")
        await message.answer(
            f"❌ <b>Ошибка при обработке файла</b>\n\n"
            f"<blockquote>{str(e)}</blockquote>",
            parse_mode="HTML"
        )


# Обработка неверного типа данных (не файл)
@dp.message(CodeModificationStates.waiting_for_code)
async def wrong_data_type_code(message: Message):
    await message.answer(
        "⚠️ <b>Ожидается файл</b>\n\n"
        "<blockquote>Пожалуйста, отправьте файл с кодом, а не текст</blockquote>\n\n"
        "❌ Для отмены введите /cancel",
        parse_mode="HTML"
    )


# Получение запроса на изменение
@dp.message(CodeModificationStates.waiting_for_request, F.text)
async def receive_modification_request(message: Message, state: FSMContext):
    user_request = message.text

    # Проверка на команду отмены
    if user_request.startswith('/'):
        return

    # Получаем код из БД
    code, filename = get_user_code(message.from_user.id)

    if not code:
        await message.answer("❌ Код не найден. Отправьте файл заново.")
        await state.clear()
        return

    # Получаем модель
    model = get_user_model(message.from_user.id)

    # Отправляем запрос к AI
    status_msg = await message.answer(
        "🤖 <b>AI анализирует код...</b>\n\n"
        f"<blockquote>📊 Модель: <code>{model}</code>\n"
        f"📝 Размер кода: {len(code)} символов</blockquote>",
        parse_mode="HTML"
    )

    ai_response = await send_ai_request(message.from_user.id, code, user_request, model)

    if not ai_response:
        await status_msg.edit_text(
            "❌ <b>Ошибка связи с AI</b>\n\n"
            "<blockquote>🔧 Возможные причины:\n"
            "• Проблемы с интернетом\n"
            "• API временно недоступен\n"
            "• Неверная модель</blockquote>\n\n"
            f"🤖 Модель: <code>{model}</code>\n\n"
            "💡 Попробуйте:\n"
            "• Сменить модель (/start → ⚙️ Сменить модель)\n"
            "• Повторить запрос через минуту\n"
            "• Использовать команду /test для диагностики",
            parse_mode="HTML"
        )
        await state.clear()
        return

    # Применяем изменения
    await status_msg.edit_text(
        "⚙️ <b>Применяю изменения...</b>\n\n"
        "<blockquote>🔄 Обработка кода...</blockquote>",
        parse_mode="HTML"
    )

    success, modified_code, summary = apply_changes(code, ai_response)

    if not success:
        # Если не удалось распарсить как JSON, возможно AI вернул текстовое объяснение
        await status_msg.edit_text(
            f"⚠️ <b>AI не смог автоматически изменить код</b>\n\n"
            f"<blockquote>💬 Ответ AI:\n{ai_response[:500]}...</blockquote>\n\n"
            f"<blockquote>❌ {summary}</blockquote>\n\n"
            "💡 <b>Попробуйте:</b>\n"
            "• Переформулировать запрос более конкретно\n"
            "• Указать точные имена функций/переменных\n"
            "• Сменить модель AI (gemini-3-pro рекомендуется)\n"
            "• Разбить задачу на несколько простых запросов",
            parse_mode="HTML"
        )
        await state.clear()
        return

    # Сохраняем измененный код
    new_filename = f"modified_{filename}"
    modified_file_path = f"/tmp/{new_filename}"

    try:
        with open(modified_file_path, 'w', encoding='utf-8') as f:
            f.write(modified_code)

        # Отправляем файл
        await status_msg.delete()

        file_to_send = FSInputFile(modified_file_path)
        await message.answer_document(
            document=file_to_send,
            caption=f"✅ <b>Готово!</b>\n\n"
                    f"<blockquote>🔧 {summary}</blockquote>\n\n"
                    f"🤖 <b>Модель:</b> <code>{model}</code>\n"
                    f"📄 <b>Файл:</b> <code>{new_filename}</code>",
            parse_mode="HTML"
        )

        # Удаляем временный файл
        os.remove(modified_file_path)

    except Exception as e:
        logging.error(f"Ошибка при сохранении/отправке файла: {e}")
        await status_msg.edit_text(
            f"❌ <b>Ошибка при создании файла</b>\n\n"
            f"<blockquote>{str(e)}</blockquote>",
            parse_mode="HTML"
        )

    await state.clear()


# Обработка неверного типа данных (не текст) при ожидании запроса
@dp.message(CodeModificationStates.waiting_for_request)
async def wrong_data_type_request(message: Message):
    await message.answer(
        "⚠️ <b>Ожидается текстовый запрос</b>\n\n"
        "<blockquote>Пожалуйста, опишите текстом что нужно изменить в коде</blockquote>\n\n"
        "❌ Для отмены введите /cancel",
        parse_mode="HTML"
    )


# Команда для тестирования API
@dp.message(Command("test"))
async def test_api(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    model = get_user_model(message.from_user.id)
    status_msg = await message.answer(
        f"🧪 <b>Тестирование API</b>\n\n"
        f"<blockquote>📡 URL: <code>{API_URL}</code>\n"
        f"🤖 Модель: <code>{model}</code></blockquote>\n\n"
        "⏳ Отправка тестового запроса...",
        parse_mode="HTML"
    )

    test_code = "print('Hello, World!')"
    test_request = "Добавь комментарий к этой строке"

    ai_response = await send_ai_request(message.from_user.id, test_code, test_request, model)

    if ai_response:
        await status_msg.edit_text(
            "✅ <b>API работает!</b>\n\n"
            f"<blockquote>📡 Соединение установлено\n"
            f"🔑 Авторизация пройдена\n"
            f"🤖 Модель: <code>{model}</code>\n"
            f"📊 Получен ответ: {len(ai_response)} символов</blockquote>\n\n"
            f"💬 <b>Фрагмент ответа:</b>\n"
            f"<blockquote>{ai_response[:300]}...</blockquote>",
            parse_mode="HTML"
        )
    else:
        await status_msg.edit_text(
            "❌ <b>API не отвечает</b>\n\n"
            f"<blockquote>📡 URL: <code>{API_URL}</code>\n"
            f"🤖 Модель: <code>{model}</code></blockquote>\n\n"
            "🔧 <b>Возможные причины:</b>\n"
            "• API недоступен\n"
            "• Проблемы с интернетом\n"
            "• Модель не поддерживается\n\n"
            "📋 Проверьте логи бота для деталей",
            parse_mode="HTML"
        )


# Обработчик кнопки "Сменить модель"
@dp.message(F.text == "⚙️ Сменить модель")
async def show_models(message: Message):
    current_model = get_user_model(message.from_user.id)

    await message.answer(
        "⚙️ <b>Выбор AI модели</b>\n\n"
        f"<blockquote>Текущая: <code>{current_model}</code></blockquote>\n\n"
        "<blockquote>👇 Выберите модель:</blockquote>",
        reply_markup=models_keyboard(current_model),
        parse_mode="HTML"
    )


# Выбор модели
@dp.callback_query(F.data.startswith("model_"))
async def select_model(callback: CallbackQuery):
    model = callback.data.split("model_")[1]
    set_user_model(callback.from_user.id, model)

    await callback.message.edit_text(
        "✅ <b>Модель изменена!</b>\n\n"
        f"<blockquote>🤖 Новая модель: <code>{model}</code></blockquote>",
        parse_mode="HTML"
    )
    await callback.answer(f"✅ Модель {model} установлена!")


@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery):
    await callback.message.delete()
    await callback.answer()


# Обработчик кнопки "Информация"
@dp.message(F.text == "ℹ️ Информация")
async def show_info(message: Message):
    current_model = get_user_model(message.from_user.id)
    is_admin = message.from_user.id == ADMIN_ID

    commands_text = "📋 <b>Команды:</b>\n<blockquote>/start - главное меню\n/cancel - отмена операции"
    if is_admin:
        commands_text += "\n/admin - админ панель\n/test - тест API"
    commands_text += "</blockquote>\n\n"

    await message.answer(
        "ℹ️ <b>Информация о боте</b>\n\n"
        "<blockquote>🤖 <b>AI Code Editor</b>\n\n"
        "Бот для автоматической модификации кода с помощью AI</blockquote>\n\n"
        "🔹 <b>Как использовать:</b>\n\n"
        "<blockquote>1️⃣ Нажмите «Изменить код»\n"
        "2️⃣ Отправьте файл с кодом\n"
        "3️⃣ Опишите нужные изменения\n"
        "4️⃣ Получите готовый файл</blockquote>\n\n"
        f"{commands_text}"
        f"🤖 <b>Текущая модель:</b> <code>{current_model}</code>\n\n"
        f"📊 <b>Доступно моделей:</b> {len(AVAILABLE_MODELS)}",
        parse_mode="HTML"
    )


# Обработчик кнопки "Поддержка"
@dp.message(F.text == "🆘 Поддержка")
async def show_support(message: Message):
    await message.answer(
        "🆘 <b>Поддержка</b>\n\n"
        "<blockquote>По всем вопросам и предложениям обращайтесь к администратору:</blockquote>\n\n"
        "👤 <b>Контакт:</b> @fuck_zaza",
        parse_mode="HTML"
    )


# Обработчик кнопки "Админ панель"
@dp.message(F.text == "👨‍💼 Админ панель")
async def admin_panel(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    conn = sqlite3.connect('code_bot.db')
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    conn.close()

    await message.answer(
        "👨‍💼 <b>Админ панель</b>\n\n"
        f"<blockquote>👥 <b>Всего пользователей:</b> {total_users}\n"
        f"🤖 <b>Доступно моделей:</b> {len(AVAILABLE_MODELS)}</blockquote>",
        parse_mode="HTML"
    )


# Главная функция
async def main():
    init_db()
    logging.info("🚀 AI Code Editor бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
