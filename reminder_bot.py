import logging
import json
import os
import telegram
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes, ConversationHandler,
    CallbackQueryHandler, RetryAfter
)
import sqlite3
from datetime import datetime, timedelta, time
from dotenv import load_dotenv
import warnings
from quickstart import update_sheet_row
import pytz
import asyncio

MAX_RETRIES = 5
RETRY_DELAY = 5

warnings.filterwarnings("ignore", category=telegram.warnings.PTBUserWarning)
load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

CHOOSING_SPECIALIST = range(1)
BOT_TOKEN = os.getenv('BOT_TOKEN')
SPECIALISTS_FILE = os.getenv('SPECIALISTS_FILE', 'specialists.json')
TASKS_FILE = os.getenv('TASKS_FILE', 'tasks.json')
START_TIME = time(4, 0)
END_TIME = time(19, 0)
TIMEZONE = pytz.timezone('Europe/Moscow')

MONTHS = {
    1: 'января', 2: 'февраля', 3: 'марта', 4: 'апреля',
    5: 'мая', 6: 'июня', 7: 'июля', 8: 'августа',
    9: 'сентября', 10: 'октября', 11: 'ноября', 12: 'декабря'
}


async def send_message_with_retry(context, chat_id, text, parse_mode=None):
    for attempt in range(MAX_RETRIES):
        try:
            return await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=parse_mode
            )
        except RetryAfter as e:
            if attempt == MAX_RETRIES - 1:
                raise
            await asyncio.sleep(e.retry_after)
        except Exception as e:
            if attempt == MAX_RETRIES - 1:
                raise
            await asyncio.sleep(RETRY_DELAY)


def load_json_file(file_path):
    try:
        with open(file_path, 'r', encoding='utf-8') as file:
            return json.load(file)
    except FileNotFoundError:
        logger.error(f"Файл {file_path} не найден.")
        return None
    except json.JSONDecodeError:
        logger.error(f"Ошибка при разборе JSON в файле {file_path}.")
        return None


def load_specialists():
    specialists_data = load_json_file(SPECIALISTS_FILE)
    return sorted(specialists_data['specialists'], key=lambda x: x['surname']) if specialists_data else []


def load_tasks():
    tasks_data = load_json_file(TASKS_FILE)
    return tasks_data['tasks'] if tasks_data else []


def init_db():
    with sqlite3.connect('tasks.db') as conn:
        c = conn.cursor()
        c.executescript('''
            DROP TABLE IF EXISTS tasks;
            DROP TABLE IF EXISTS sent_reminders;
            DROP TABLE IF EXISTS users;

            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY,
                project TEXT,
                task TEXT,
                interval INTEGER,
                next_reminder TEXT,
                last_attempt TEXT
            );

            CREATE TABLE sent_reminders (
                task_id INTEGER PRIMARY KEY,
                sent_at TEXT,
                responded BOOLEAN
            );

            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                surname TEXT,
                status TEXT,
                last_update TEXT,
                retry_count INTEGER DEFAULT 0
            );

            CREATE INDEX idx_tasks_next_reminder ON tasks(next_reminder);
            CREATE INDEX idx_sent_reminders_task_id ON sent_reminders(task_id);
            CREATE INDEX idx_users_status ON users(status);
        ''')
        logger.info("База данных инициализирована")


def init_tasks_for_specialist(specialist):
    tasks = load_tasks()
    now = datetime.now(TIMEZONE)
    with sqlite3.connect('tasks.db') as conn:
        c = conn.cursor()
        for project in specialist['projects']:
            for task in tasks:
                next_reminder = now + timedelta(days=task['interval_days'])
                next_reminder = get_next_workday(next_reminder)
                c.execute(
                    "INSERT INTO tasks (project, task, interval, next_reminder, last_attempt) VALUES (?, ?, ?, ?, ?)",
                    (project, task['task'], task['interval_days'], next_reminder.isoformat(), now.isoformat())
                )
        logger.info(f"Задачи загружены для специалиста {specialist['surname']}")


def update_user_status(user_id, surname, status):
    now = datetime.now(TIMEZONE)
    with sqlite3.connect('tasks.db') as conn:
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO users (id, surname, status, last_update, retry_count) VALUES (?, ?, ?, ?, 0)",
            (user_id, surname, status, now.isoformat())
        )
        try:
            date_on = now if status == "Подключен" else None
            date_off = now if status == "Отключен" else None
            update_sheet_row(surname, status, date_on=date_on, date_off=date_off)
            logger.info(f"Статус пользователя {surname} обновлен в Google Sheets: {status}")
        except Exception as e:
            logger.error(f"Ошибка при обновлении статуса в Google Sheets: {e}")


def get_interval_string(interval: int) -> str:
    if interval == 1:
        return "**1 день**"
    elif 2 <= interval <= 4:
        return f"**{interval} дня**"
    else:
        return f"**{interval} дней**"


def is_workday(date):
    return date.weekday() < 5


def get_next_workday(date):
    while not is_workday(date):
        date += timedelta(days=1)
    return date


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    welcome_message = (
        "ПРИВЕТ!😊\nТебе на помощь спешит бот, который будет напоминать выполнять "
        "рутину по контексту💪✨\n\n🗓️ Если нужно что-то изменить или добавить, "
        "в конце месяца соберу ОС! 🌟"
    )
    await send_message_with_retry(context, update.message.chat_id, welcome_message)

    specialists = load_specialists()
    keyboard = [
        [telegram.InlineKeyboardButton(spec['surname'], callback_data=f"specialist:{spec['surname']}")]
        for spec in specialists
    ]
    reply_markup = telegram.InlineKeyboardMarkup(keyboard)
    await send_message_with_retry(
        context,
        update.message.chat_id,
        'Теперь выбери свою фамилию',
        reply_markup=reply_markup
    )
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
        await send_message_with_retry(
            context,
            query.message.chat_id,
            f"*ТВОИ ПРОЕКТЫ:*\n{project_list}",
            parse_mode='Markdown'
        )

        init_tasks_for_specialist(specialist)

        context.job_queue.run_once(
            send_reminder_list,
            10,
            data={'projects': specialist['projects'], 'chat_id': query.message.chat_id}
        )

        context.job_queue.run_once(
            send_nearest_task,
            20,
            data={'projects': specialist['projects'], 'chat_id': query.message.chat_id}
        )

        context.job_queue.run_repeating(
            check_reminders,
            interval=120,
            first=5,
            data={'projects': specialist['projects'], 'chat_id': query.message.chat_id},
            name=str(query.message.chat_id)
        )

        update_user_status(query.from_user.id, specialist['surname'], "Подключен")
        return ConversationHandler.END


async def send_reminder_list(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data['chat_id']
    projects = context.job.data['projects']

    with sqlite3.connect('tasks.db') as conn:
        c = conn.cursor()
        placeholders = ','.join('?' for _ in projects)
        c.execute(f"""
            SELECT DISTINCT t.task, t.interval
            FROM tasks t
            WHERE t.project IN ({placeholders})
        """, projects)
        tasks = c.fetchall()

        if tasks:
            message_lines = ["*СПИСОК ТВОИХ НАПОМИНАНИЙ и ГРАФИК ПРОВЕРКИ*\n\n"]
            unique_tasks = {task[0].lower(): (task[0], task[1]) for task in tasks}

            for task_name, (original_name, interval) in unique_tasks.items():
                task_name_upper = original_name.capitalize()
                interval_string = get_interval_string(interval)
                message_lines.append(f"• {task_name_upper} - {interval_string}\n")

            message = "".join(message_lines)
            await send_message_with_retry(context, chat_id, message, parse_mode='Markdown')


async def check_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now(TIMEZONE)
    if START_TIME <= now.time() <= END_TIME and is_workday(now):
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
            for task_id, project, task_name, interval in tasks:
                if task_name not in reminders:
                    reminders[task_name] = {
                        "projects": set(),
                        "ids": [],
                        "interval": interval
                    }
                reminders[task_name]["projects"].add(project)
                reminders[task_name]["ids"].append(task_id)

            for task_name, reminder_data in reminders.items():
                try:
                    await send_reminder(
                        context,
                        context.job.data['chat_id'],
                        task_name,
                        list(reminder_data["projects"]),
                        reminder_data["interval"]
                    )

                    next_reminder_time = now + timedelta(days=reminder_data["interval"])
                    next_reminder_time = get_next_workday(next_reminder_time)

                    for task_id in reminder_data["ids"]:
                        c.execute(
                            "UPDATE tasks SET next_reminder = ?, last_attempt = ? WHERE id = ?",
                            (next_reminder_time.isoformat(), now.isoformat(), task_id)
                        )
                    conn.commit()

                except Exception as e:
                    logger.error(f"Ошибка при отправке напоминания: {e}")
    else:
        logger.info(
            f"Текущее время {now.time()} не соответствует времени отправки "
            f"напоминаний ({START_TIME}-{END_TIME}) или сегодня выходной"
        )


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception while handling an update: {context.error}")

    try:
        if isinstance(context.error, RetryAfter):
            await asyncio.sleep(context.error.retry_after)
            return

        if isinstance(context.error, telegram.error.NetworkError):
            await asyncio.sleep(RETRY_DELAY)
            return

        if isinstance(context.error, telegram.error.Forbidden):
            user_id = update.effective_user.id if update and update.effective_user else "Unknown"
            logger.warning(f"Bot was blocked by user {user_id}")

            with sqlite3.connect('tasks.db') as conn:
                c = conn.cursor()
                c.execute("UPDATE users SET status = 'Blocked' WHERE id = ?", (user_id,))
            return

    except Exception as e:
        logger.error(f"Error in error handler: {e}")


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_surname = context.user_data.get('surname', 'Неизвестный пользователь')
    update_user_status(update.message.from_user.id, user_surname, "Отключен")
    await send_message_with_retry(
        context,
        update.message.chat_id,
        "Вы отключены от бота. Если захотите снова подключиться, просто напишите /start."
    )


def ping_server(context: ContextTypes.DEFAULT_TYPE):
    # Здесь можно выполнить любое действие, чтобы поддерживать активность
    logger.info("Ping server to keep it alive")


def main() -> None:
    init_db()
    logger.info(f"Бот запущен. Текущее время: {datetime.now(TIMEZONE)}")

    application = Application.builder().token(BOT_TOKEN).build()
    application.job_queue.run_repeating(ping_server, interval=timedelta(minutes=10))

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING_SPECIALIST: [CallbackQueryHandler(specialist_choice)],
        },
        fallbacks=[],
        persistent=True
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("stop", stop))
    application.add_error_handler(error_handler)

    if os.environ.get('RENDER'):
        port = int(os.environ.get('PORT', 10000))
        webhook_url = os.environ.get("WEBHOOK_URL")

        application.run_webhook(
            listen="0.0.0.0",
            port=port,
            webhook_url=webhook_url,
            secret_token=os.environ.get("SECRET_TOKEN"),
            drop_pending_updates=True,
            webhook_max_connections=40,
            allowed_updates=["message", "callback_query"]
        )
    else:
        application.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
