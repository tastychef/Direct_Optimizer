import logging
import json
import os
import telegram
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, ConversationHandler, CallbackQueryHandler
import sqlite3
from datetime import datetime, timedelta, time
from dotenv import load_dotenv
import warnings
from quickstart import update_sheet_row

warnings.filterwarnings("ignore", category=telegram.warnings.PTBUserWarning)

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

# ВРЕМЕННЫЕ ОГРАНИЧЕНИЯ ДЛЯ ОТПРАВКИ НАПОМИНАНИЙ
START_TIME = time(4, 0)  # 4:00
END_TIME = time(21, 0)  # 21:00


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
    with sqlite3.connect('tasks.db') as conn:
        c = conn.cursor()
        c.execute("DROP TABLE IF EXISTS tasks")
        c.execute("DROP TABLE IF EXISTS sent_reminders")
        c.execute("DROP TABLE IF EXISTS users")
        c.execute(
            '''CREATE TABLE tasks (id INTEGER PRIMARY KEY, project TEXT, task TEXT, interval INTEGER, next_reminder TEXT)''')
        c.execute('''CREATE TABLE sent_reminders (task_id INTEGER PRIMARY KEY, sent_at TEXT, responded BOOLEAN)''')
        c.execute('''CREATE TABLE users (id INTEGER PRIMARY KEY, surname TEXT, status TEXT, last_update TEXT)''')
        c.execute("CREATE INDEX idx_tasks_next_reminder ON tasks(next_reminder)")
        c.execute("CREATE INDEX idx_sent_reminders_task_id ON sent_reminders(task_id)")
        c.execute("CREATE INDEX idx_users_status ON users(status)")
    logger.info("База данных инициализирована")


# ИНИЦИАЛИЗАЦИЯ ЗАДАЧ ДЛЯ КОНКРЕТНОГО СПЕЦИАЛИСТА
def init_tasks_for_specialist(specialist):
    tasks = load_tasks()
    with sqlite3.connect('tasks.db') as conn:
        c = conn.cursor()
        for project in specialist['projects']:
            for task in tasks:
                next_reminder = datetime.now() + timedelta(minutes=task['interval_minutes'])
                c.execute("INSERT INTO tasks (project, task, interval, next_reminder) VALUES (?, ?, ?, ?)",
                          (project, task['task'], task['interval_minutes'], next_reminder.isoformat()))
    logger.info(f"Задачи загружены для специалиста {specialist['surname']}")


# ОБНОВЛЕНИЕ СТАТУСА ПОЛЬЗОВАТЕЛЯ
def update_user_status(user_id, surname, status):
    now = datetime.now()
    with sqlite3.connect('tasks.db') as conn:
        c = conn.cursor()
        c.execute("SELECT status FROM users WHERE id = ?", (user_id,))
        old_status = c.fetchone()
        if old_status is None or old_status[0] != status:
            c.execute("INSERT OR REPLACE INTO users (id, surname, status, last_update) VALUES (?, ?, ?, ?)",
                      (user_id, surname, status, now.isoformat()))

            # Обновление в Google Sheets
            date_on = now if status == "Подключен" else None
            date_off = now if status == "Отключен" else None
            try:
                update_sheet_row(surname, status, date_on, date_off)
                logger.info(f"Статус пользователя {surname} обновлен в Google Sheets: {status}")
            except Exception as e:
                logger.error(f"Ошибка при обновлении статуса в Google Sheets: {e}")

    logger.info(f"Статус пользователя {surname} обновлен: {status}")


# ОБРАБОТЧИКИ КОМАНД
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    welcome_message = (
        "Привет! 😊\nТебе на помощь спешит бот..."
    )
    await update.message.reply_text(welcome_message)
    specialists = load_specialists()
    keyboard = [[telegram.InlineKeyboardButton(spec['surname'], callback_data=f"specialist:{spec['surname']}")] for spec
                in specialists]
    reply_markup = telegram.InlineKeyboardMarkup(keyboard)
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
        init_tasks_for_specialist(specialist)
        context.job_queue.run_repeating(
            check_reminders,
            interval=30.01,
            first=1,
            data={'projects': specialist['projects'], 'chat_id': query.message.chat_id},
            name=str(query.message.chat_id)
        )
        update_user_status(query.from_user.id, specialist['surname'], "Подключен")
        return ConversationHandler.END
    else:
        await query.edit_message_text('Произошла ошибка. Пожалуйста, напишите @LEX_126.')
        return ConversationHandler.END


async def send_reminder(context: ContextTypes.DEFAULT_TYPE, chat_id: int, task: str, projects: list) -> None:
    message = f"*📋{task.upper()}*\n"
    for project in sorted(projects):
        message += f"- {project}\n"
    try:
        await context.bot.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')
    except telegram.error.Forbidden:
        logger.warning(f"Пользователь {chat_id} заблокировал бота")
        await handle_bot_blocked(context, chat_id)


async def handle_bot_blocked(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    with sqlite3.connect('tasks.db') as conn:
        c = conn.cursor()
        c.execute("SELECT surname FROM users WHERE id = ?", (chat_id,))
        result = c.fetchone()
        if result:
            surname = result[0]
            update_user_status(chat_id, surname, "Отключен")
        else:
            logger.error(f"Пользователь с chat_id {chat_id} не найден в базе данных")


# ПРОВЕРКА НАПОМИНАНИЙ
async def check_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now()
    current_time = now.time()
    if START_TIME <= current_time <= END_TIME:
        logger.info(f"Проверка напоминаний в {now}")
        with sqlite3.connect('tasks.db') as conn:
            c = conn.cursor()
            projects = context.job.data['projects']
            placeholders = ','.join('?' for _ in projects)
            c.execute(f"""
                SELECT t.id, t.project, t.task, t.interval
                FROM tasks t
                WHERE t.next_reminder <= ? AND t.project IN ({placeholders})
            """, (now.isoformat(), *projects))
            tasks = c.fetchall()
        logger.info(f"Найдено задач для напоминания: {len(tasks)}")
        reminders = {}
        for task_id, project, task, interval in tasks:
            if task not in reminders:
                reminders[task] = {"projects": set(), "interval": interval}
            reminders[task]["projects"].add(project)
            next_reminder = now + timedelta(minutes=interval)
            with sqlite3.connect('tasks.db') as conn:
                c = conn.cursor()
                c.execute("UPDATE tasks SET next_reminder = ? WHERE id = ?", (next_reminder.isoformat(), task_id))
        for task_name, data in reminders.items():
            await send_reminder(context, context.job.data['chat_id'], task_name, list(data["projects"]))
    else:
        logger.info(f"Текущее время {current_time} вне диапазона отправки напоминаний")


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception while handling an update: {context.error}")


# ОСНОВНАЯ ФУНКЦИЯ
def main() -> None:
    init_db()

    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING_SPECIALIST: [CallbackQueryHandler(specialist_choice)],
        },
        fallbacks=[],
    )

    application.add_handler(conv_handler)
    application.add_error_handler(error_handler)

    if os.environ.get('RENDER'):
        port = int(os.environ.get('PORT', 10000))
        webhook_url = os.environ.get("WEBHOOK_URL")
        secret_token = os.environ.get("SECRET_TOKEN")

        application.run_webhook(listen="0.0.0.0", port=port,
                                webhook_url=webhook_url,
                                secret_token=secret_token)
    else:
        application.run_polling()


if __name__ == '__main__':
    main()