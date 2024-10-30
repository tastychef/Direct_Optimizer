import logging
import json
import os
import telegram
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, ConversationHandler, CallbackQueryHandler
import sqlite3
from datetime import datetime, timedelta
from dotenv import load_dotenv
import warnings
import quickstart

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
    c.execute("DROP TABLE IF EXISTS users")

    c.execute('''CREATE TABLE tasks
                 (id INTEGER PRIMARY KEY, project TEXT, task TEXT, interval INTEGER, next_reminder TEXT)''')
    c.execute('''CREATE TABLE sent_reminders
                 (task_id INTEGER PRIMARY KEY, sent_at TEXT, responded BOOLEAN)''')
    c.execute('''CREATE TABLE users
                 (id INTEGER PRIMARY KEY, surname TEXT, status TEXT, last_update TEXT)''')

    c.execute("CREATE INDEX idx_tasks_next_reminder ON tasks(next_reminder)")
    c.execute("CREATE INDEX idx_sent_reminders_task_id ON sent_reminders(task_id)")
    c.execute("CREATE INDEX idx_users_status ON users(status)")

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


# ОБНОВЛЕНИЕ СТАТУСА ПОЛЬЗОВАТЕЛЯ
def update_user_status(user_id, surname, status):
    conn = sqlite3.connect('tasks.db')
    c = conn.cursor()
    now = datetime.now()

    c.execute("SELECT status FROM users WHERE id = ?", (user_id,))
    old_status = c.fetchone()

    if old_status is None or old_status[0] != status:
        c.execute("INSERT OR REPLACE INTO users (id, surname, status, last_update) VALUES (?, ?, ?, ?)",
                  (user_id, surname, status, now.isoformat()))
        conn.commit()

        if status == "Отключен":
            quickstart.write_to_sheet(surname, status, None, now)
        else:
            quickstart.write_to_sheet(surname, status, now, None)

        logger.info(f"Статус пользователя {surname} обновлен: {status}")

    conn.close()


# ОБРАБОТЧИКИ КОМАНД
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    welcome_message = (
        "Привет! 😊\nТебе на помощь спешит бот, который будет напоминать выполнять рутину по контексту, "
        "без которой никак. 💪✨\n\nСписок задач с периодом приложу позже. 🗓️ Если нужно что-то изменить или добавить, дай знать! 🌟"
    )
    await update.message.reply_text(welcome_message)

    specialists = load_specialists()
    keyboard = [
        [telegram.InlineKeyboardButton(spec['surname'], callback_data=f"specialist:{spec['surname']}")]
        for spec in specialists
    ]
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
            interval=5,
            first=1,
            data={'projects': specialist['projects'], 'chat_id': query.message.chat_id,
                  'surname': specialist['surname']},
            name=str(query.message.chat_id)
        )

        update_user_status(query.from_user.id, specialist['surname'], "Подключен")

        return ConversationHandler.END
    else:
        await query.edit_message_text('Произошла ошибка. Пожалуйста, напишите @LEX_126.')
        return ConversationHandler.END


async def send_reminder(context: ContextTypes.DEFAULT_TYPE, chat_id: int, task: str, projects: list) -> None:
    message = f"*📋 НАПОМИНАНИЕ:*\n\n*{task.upper()}*\n"
    for project in sorted(projects):
        message += f"- {project}\n"

    try:
        await context.bot.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')
    except telegram.error.Forbidden:
        logger.warning(f"Пользователь {chat_id} заблокировал бота")
        block_time = datetime.now()
        surname = context.job.data.get('surname', 'Неизвестный')
        quickstart.write_to_sheet(surname, "Отключен", None, block_time)
        stop_reminders(context, chat_id)


def stop_reminders(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    current_jobs = context.job_queue.get_jobs_by_name(str(chat_id))
    for job in current_jobs:
        job.schedule_removal()
    logger.info(f"Проверка напоминаний остановлена для пользователя {chat_id}")


async def check_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now()
    logger.info(f"Проверка напоминаний в {now}")
    conn = sqlite3.connect('tasks.db')
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

        next_reminder = now + timedelta(seconds=interval)
        c.execute("UPDATE tasks SET next_reminder = ? WHERE id = ?", (next_reminder.isoformat(), task_id))

    for task, data in reminders.items():
        await send_reminder(context, context.job.data['chat_id'], task, list(data["projects"]))

    conn.commit()
    conn.close()


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception while handling an update: {context.error}")
    if isinstance(context.error, telegram.error.Forbidden):
        if update.effective_chat:
            logger.warning(f"Пользователь {update.effective_chat.id} заблокировал бота")
            block_time = datetime.now()
            surname = context.user_data.get('surname', 'Неизвестный')
            quickstart.write_to_sheet(surname, "Отключен", None, block_time)
            stop_reminders(context, update.effective_chat.id)
    elif isinstance(context.error, telegram.error.BadRequest) and "Query is too old" in str(context.error):
        if update and update.callback_query:
            await update.callback_query.answer()
            await update.effective_message.reply_text("Это сообщение устарело. Пожалуйста, дождитесь следующего напоминания.")
    elif update:
        await update.message.reply_text("Произошла ошибка при обработке запроса. Пожалуйста, попробуйте еще раз.")

def main() -> None:
    init_db()

    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING_SPECIALIST: [CallbackQueryHandler(specialist_choice, pattern=r'^specialist:')],
        },
        fallbacks=[],
    )

    application.add_handler(conv_handler)
    application.add_error_handler(error_handler)

    application.run_polling()


if __name__ == '__main__':
    main()
