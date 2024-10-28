import logging
import json
import os
import time
import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, ConversationHandler, CallbackQueryHandler
import sqlite3
from datetime import datetime, timedelta
import quickstart
from dotenv import load_dotenv
import warnings
from telegram.warnings import PTBUserWarning
import asyncio

warnings.filterwarnings("ignore", category=PTBUserWarning)

# ЗАГРУЗКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ
load_dotenv()

# НАСТРОЙКА ЛОГИРОВАНИЯ
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# СОСТОЯНИЯ ДЛЯ CONVERSATIONHANDLER
CHOOSING_SPECIALIST = range(1)

# ПОЛУЧЕНИЕ КОНФИДЕНЦИАЛЬНЫХ ДАННЫХ ИЗ .ENV
BOT_TOKEN = os.getenv('BOT_TOKEN')
SPECIALISTS_FILE = os.getenv('SPECIALISTS_FILE', 'specialists.json')
TASKS_FILE = os.getenv('TASKS_FILE', 'tasks.json')

# ЗАГРУЗКА СПЕЦИАЛИСТОВ И ИХ ПРОЕКТОВ
def load_specialists():
    try:
        with open(SPECIALISTS_FILE, 'r', encoding='utf-8') as file:
            specialists = json.load(file)['specialists']
        return sorted(specialists, key=lambda x: x['surname'])
    except FileNotFoundError:
        logger.error(f"Файл {SPECIALISTS_FILE} не найден.")
        return []
    except json.JSONDecodeError:
        logger.error(f"Ошибка при разборе JSON в файле {SPECIALISTS_FILE}.")
        return []

# ЗАГРУЗКА ЗАДАЧ
def load_tasks():
    try:
        with open(TASKS_FILE, 'r', encoding='utf-8') as file:
            return json.load(file)['tasks']
    except FileNotFoundError:
        logger.error(f"Файл {TASKS_FILE} не найден.")
        return []
    except json.JSONDecodeError:
        logger.error(f"Ошибка при разборе JSON в файле {TASKS_FILE}.")
        return []

# ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ
def init_db():
    conn = sqlite3.connect('tasks.db')
    c = conn.cursor()

    c.execute("DROP TABLE IF EXISTS tasks")
    c.execute("DROP TABLE IF EXISTS sent_reminders")
    c.execute("DROP TABLE IF EXISTS button_data")

    c.execute('''CREATE TABLE tasks
                 (id INTEGER PRIMARY KEY, project TEXT, task TEXT, interval INTEGER, next_reminder TEXT)''')
    c.execute('''CREATE TABLE sent_reminders
                 (task_id INTEGER PRIMARY KEY, sent_at TEXT, responded BOOLEAN)''')
    c.execute('''CREATE TABLE button_data
                 (button_id TEXT PRIMARY KEY, task_id INTEGER, project TEXT, task TEXT, created_at INTEGER)''')

    # Добавление индексов для оптимизации запросов
    c.execute("CREATE INDEX idx_tasks_next_reminder ON tasks(next_reminder)")
    c.execute("CREATE INDEX idx_sent_reminders_task_id ON sent_reminders(task_id)")
    c.execute("CREATE INDEX idx_button_data_created_at ON button_data(created_at)")

    conn.commit()
    conn.close()
    logger.info("База данных инициализирована")

# ИНИЦИАЛИЗАЦИЯ ЗАДАЧ ДЛЯ КОНКРЕТНОГО СПЕЦИАЛИСТА
def init_tasks_for_specialist(specialist):
    conn = sqlite3.connect('tasks.db')
    c = conn.cursor()

    tasks = load_tasks()

    for project in specialist['projects']:
        for task in tasks:
            next_reminder = datetime.now() + timedelta(seconds=task['interval_seconds'])
            c.execute("INSERT INTO tasks (project, task, interval, next_reminder) VALUES (?, ?, ?, ?)",
                      (project, task['task'], task['interval_seconds'], next_reminder.isoformat()))

    conn.commit()
    conn.close()
    logger.info(f"Задачи загружены для специалиста {specialist['surname']}")

# ОБРАБОТЧИКИ КОМАНД
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    welcome_message = (
        "Привет! 😊\nТебе на помощь спешит бот, который будет напоминать выполнять рутину по контексту, "
        "без которой никак. 💪✨\n\nСписок задач с периодом приложу позже. 🗓️ Если нужно что-то изменить или добавить, дай знать! 🌟"
    )
    await update.message.reply_text(welcome_message)

    specialists = load_specialists()
    keyboard = [
        [InlineKeyboardButton(spec['surname'], callback_data=f"specialist:{spec['surname']}")]
        for spec in specialists
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text('Пожалуйста, выберите вашу фамилию:', reply_markup=reply_markup)
    return CHOOSING_SPECIALIST

async def specialist_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    _, surname = query.data.split(':')
    specialists = load_specialists()
    specialist = next((s for s in specialists if s['surname'] == surname), None)

    if specialist:
        context.user_data['surname'] = specialist['surname']
        context.user_data['projects'] = specialist['projects']

        project_list = "\n".join([f"{i + 1}. {project}" for i, project in enumerate(specialist['projects'])])
        await query.edit_message_text(f"*ВАШИ ПРОЕКТЫ:*\n{project_list}", parse_mode='Markdown')

        # ИНИЦИАЛИЗАЦИЯ ЗАДАЧ ДЛЯ ВЫБРАННОГО СПЕЦИАЛИСТА
        init_tasks_for_specialist(specialist)

        # ЗАПУСК ПРОВЕРКИ НАПОМИНАНИЙ
        context.job_queue.run_repeating(check_reminders, interval=10, first=1,
                                        data={'projects': specialist['projects'], 'chat_id': query.message.chat_id,
                                              'surname': specialist['surname']})

        return ConversationHandler.END
    else:
        await query.edit_message_text('Произошла ошибка. Пожалуйста, напишите @LEX_126.')
        return ConversationHandler.END

async def send_reminder_with_buttons(context: ContextTypes.DEFAULT_TYPE, chat_id: int, project: str, task: str,
                                     task_id: int) -> None:
    conn = sqlite3.connect('tasks.db')
    c = conn.cursor()

    button_id = f"{task_id}:{int(time.time())}"
    c.execute("INSERT INTO button_data (button_id, task_id, project, task, created_at) VALUES (?, ?, ?, ?, ?)",
              (button_id, task_id, project, task, int(time.time())))
    conn.commit()

    keyboard = [
        [
            InlineKeyboardButton("✅ Сегодня сделаю!", callback_data=f"work:{button_id}"),
            InlineKeyboardButton("⏰ Напомни завтра", callback_data=f"later:{button_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await context.bot.send_message(chat_id=chat_id, text=f"Проект: {project}\n*{task}*", reply_markup=reply_markup,
                                   parse_mode='Markdown')

    conn.close()

async def check_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now()
    logger.info(f"Проверка напоминаний в {now}")
    conn = sqlite3.connect('tasks.db')
    c = conn.cursor()

    projects = context.job.data['projects']
    placeholders = ','.join('?' for _ in projects)

    c.execute(f"""
        SELECT t.id, t.project, t.task 
        FROM tasks t
        LEFT JOIN sent_reminders sr ON t.id = sr.task_id
        WHERE t.next_reminder <= ? AND (sr.sent_at IS NULL OR (sr.sent_at < t.next_reminder AND sr.responded = 0))
        AND t.project IN ({placeholders})
    """, (now.isoformat(), *projects))

    tasks = c.fetchall()

    logger.info(f"Найдено задач для напоминания: {len(tasks)}")

    reminders = []
    for task_id, project, task in tasks:
        c.execute("SELECT sent_at FROM sent_reminders WHERE task_id = ?", (task_id,))
        last_sent = c.fetchone()

        if last_sent is None or (now - datetime.fromisoformat(last_sent[0])).total_seconds() > 3600:
            reminders.append((context.job.data['chat_id'], project, task, task_id))
            c.execute("INSERT OR REPLACE INTO sent_reminders (task_id, sent_at, responded) VALUES (?, ?, ?)",
                      (task_id, now.isoformat(), 0))
        else:
            logger.info(f"Пропущено напоминание для проекта {project}, задача: {task} (отправлено менее часа назад)")

    conn.commit()
    conn.close()

    # Асинхронная отправка напоминаний
    await asyncio.gather(*[send_reminder_with_buttons(context, *reminder) for reminder in reminders])

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data_parts = query.data.split(':')
    action = data_parts[0]

    if action == "specialist":
        return await specialist_choice(update, context)

    task_id = int(data_parts[1])
    button_id = ':'.join(data_parts[1:]) if len(data_parts) > 2 else None

    conn = sqlite3.connect('tasks.db')
    c = conn.cursor()

    if action == "work":
        c.execute("SELECT interval, project, task FROM tasks WHERE id = ?", (task_id,))
        interval, project, task = c.fetchone()
        next_reminder = datetime.now() + timedelta(seconds=interval)
        c.execute("UPDATE tasks SET next_reminder = ? WHERE id = ?", (next_reminder.isoformat(), task_id))
        c.execute("UPDATE sent_reminders SET responded = 1 WHERE task_id = ?", (task_id,))
        await query.edit_message_text(text=f"✅ Отлично! Закройте задачу сегодня.")

        # ЗАПИСЬ В GOOGLE SHEETS
        try:
            surname = context.user_data.get('surname', 'Неизвестный')
            quickstart.write_to_sheet([[surname, project, task, datetime.now().strftime('%d.%m')]])
            logger.info(f"Данные успешно записаны в Google Sheets: {surname}, {project}, {task}")
        except Exception as e:
            logger.error(f"Ошибка при записи в Google Sheets: {e}")

    elif action == "later":
        next_reminder = datetime.now() + timedelta(days=1)
        c.execute("UPDATE tasks SET next_reminder = ? WHERE id = ?", (next_reminder.isoformat(), task_id))
        c.execute("UPDATE sent_reminders SET responded = 1 WHERE task_id = ?", (task_id,))
        await query.edit_message_text(text=f"⏳ Хорошо, я напомню завтра.")

    conn.commit()
    conn.close()

async def clean_old_button_data(context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = sqlite3.connect('tasks.db')
    c = conn.cursor()

    # Удаляем данные кнопок старше 48 часов
    c.execute("DELETE FROM button_data WHERE created_at < ?", (int(time.time()) - 48 * 3600,))

    conn.commit()
    conn.close()

def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception while handling an update: {context.error}")
    if isinstance(context.error, telegram.error.BadRequest) and "Query is too old" in str(context.error):
        if update and update.callback_query:
            update.callback_query.answer()
            update.effective_message.reply_text("Это сообщение устарело. Пожалуйста, дождитесь следующего напоминания.")
    elif update:
        update.message.reply_text("Произошла ошибка при обработке запроса. Пожалуйста, попробуйте еще раз.")

def main() -> None:
    init_db()

    application = Application.builder().token(BOT_TOKEN).build()

    # ИНИЦИАЛИЗАЦИЯ ОБРАБОТЧИКОВ
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING_SPECIALIST: [CallbackQueryHandler(specialist_choice, pattern=r'^specialist:')],
        },
        fallbacks=[],
    )

    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_error_handler(error_handler)

    # Добавляем периодическую очистку данных кнопок
    application.job_queue.run_repeating(clean_old_button_data, interval=timedelta(hours=24))

    # ЗАПУСК БОТА
    application.run_polling()

if __name__ == '__main__':
    main()