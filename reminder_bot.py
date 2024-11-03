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

warnings.filterwarnings("ignore", category=telegram.warnings.PTBUserWarning)

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Состояния разговора
CHOOSING_SPECIALIST = range(1)

# Получение данных из переменных окружения
BOT_TOKEN = os.getenv('BOT_TOKEN')
SPECIALISTS_FILE = os.getenv('SPECIALISTS_FILE', 'specialists.json')
TASKS_FILE = os.getenv('TASKS_FILE', 'tasks.json')

# Временные ограничения для напоминаний
START_TIME = time(4, 0)  # 4:00 AM
END_TIME = time(21, 0)  # 7:00 PM


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


def init_db():
    with sqlite3.connect('tasks.db') as conn:
        c = conn.cursor()
        c.execute("DROP TABLE IF EXISTS tasks")
        c.execute("DROP TABLE IF EXISTS sent_reminders")
        c.execute("DROP TABLE IF EXISTS users")
        c.execute('''CREATE TABLE tasks (id INTEGER PRIMARY KEY, project TEXT, task TEXT, interval INTEGER)''')
        c.execute('''CREATE TABLE sent_reminders (task_id INTEGER PRIMARY KEY, sent_at TEXT, responded BOOLEAN)''')
        c.execute('''CREATE TABLE users (id INTEGER PRIMARY KEY, surname TEXT, status TEXT, last_update TEXT)''')
        logger.info("База данных инициализирована")


def init_tasks_for_specialist(specialist):
    tasks = load_tasks()
    with sqlite3.connect('tasks.db') as conn:
        c = conn.cursor()
        for project in specialist['projects']:
            for task in tasks:
                # Используем interval_minutes напрямую без перевода
                next_reminder = datetime.now() + timedelta(
                    minutes=task['interval_minutes'])  # Используем минуты напрямую
                c.execute("INSERT INTO tasks (project, task, interval) VALUES (?, ?, ?)",
                          (project, task['task'], task['interval_minutes']))
                logger.info(f"Задачи загружены для специалиста {specialist['surname']}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    welcome_message = "Привет! 😊\nТебе на помощь спешит бот..."
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

        # Запуск повторяющейся задачи проверки напоминаний каждую минуту
        context.job_queue.run_repeating(check_reminders, interval=1, first=1,
                                        data={'projects': specialist['projects'], 'chat_id': query.message.chat_id},
                                        name=str(query.message.chat_id))

        return ConversationHandler.END
    else:
        await query.edit_message_text('Произошла ошибка. Пожалуйста, напишите @LEX_126.')
        return ConversationHandler.END


async def send_reminder(context: ContextTypes.DEFAULT_TYPE, chat_id: int, task: str, projects: list) -> None:
    message = f"*ПОРА {task.upper()}!*\n" + "\n".join([f"- {project}" for project in sorted(projects)])

    try:
        await context.bot.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')
    except telegram.error.Forbidden:
        logger.warning(f"Пользователь {chat_id} заблокировал бота")


async def check_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now()

    # Проверка на выходные (суббота и воскресенье)
    if now.weekday() >= 5:  # 5 - суббота; 6 - воскресенье
        logger.info("Сегодня выходной. Напоминания не отправляются.")
        return

    current_time = now.time()

    if START_TIME <= current_time <= END_TIME:
        logger.info(f"Проверка напоминаний в {now}")

        projects = context.job.data['projects']

        with sqlite3.connect('tasks.db') as conn:
            c = conn.cursor()
            placeholders = ','.join('?' for _ in projects)
            c.execute(f"""SELECT t.project, t.task FROM tasks t WHERE t.project IN ({placeholders})""", (*projects,))
            tasks = c.fetchall()
            logger.info(f"Найдено задач для напоминания: {len(tasks)}")

            reminders = {}
            for project_name, task_name in tasks:
                if task_name not in reminders:
                    reminders[task_name] = {"projects": set()}
                reminders[task_name]["projects"].add(project_name)

            # Отправка напоминаний по очереди
            for task_name, data in reminders.items():
                await send_reminder(context, context.job.data['chat_id'], task_name, list(data["projects"]))

    else:
        logger.info(f"Текущее время {current_time} вне диапазона отправки напоминаний")


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception while handling an update: {context.error}")


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