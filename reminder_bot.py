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
from quickstart import update_sheet_row  # Убедитесь, что эта функция корректно реализована
import pytz

warnings.filterwarnings("ignore", category=telegram.warnings.PTBUserWarning)
load_dotenv()

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
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
        c.execute("DROP TABLE IF EXISTS tasks")
        c.execute("DROP TABLE IF EXISTS sent_reminders")
        c.execute("DROP TABLE IF EXISTS users")
        c.execute(
            ''' CREATE TABLE tasks ( id INTEGER PRIMARY KEY, project TEXT, task TEXT, interval INTEGER, next_reminder TEXT ) ''')
        c.execute(''' CREATE TABLE sent_reminders ( task_id INTEGER PRIMARY KEY, sent_at TEXT, responded BOOLEAN ) ''')
        c.execute(''' CREATE TABLE users ( id INTEGER PRIMARY KEY, surname TEXT, status TEXT, last_update TEXT ) ''')
        c.execute("CREATE INDEX idx_tasks_next_reminder ON tasks(next_reminder)")
        c.execute("CREATE INDEX idx_sent_reminders_task_id ON sent_reminders(task_id)")
        c.execute("CREATE INDEX idx_users_status ON users(status)")
        logger.info("База данных инициализирована")


def init_tasks_for_specialist(specialist):
    tasks = load_tasks()
    now = datetime.now(TIMEZONE)
    with sqlite3.connect('tasks.db') as conn:
        c = conn.cursor()
        for project in specialist['projects']:
            for task in tasks:
                next_reminder = now + timedelta(days=task['interval_days'])
                c.execute(
                    "INSERT INTO tasks (project, task, interval, next_reminder) VALUES (?, ?, ?, ?)",
                    (project, task['task'], task['interval_days'], next_reminder.isoformat())
                )
    logger.info(f"Задачи загружены для специалиста {specialist['surname']}")


def update_user_status(user_id, surname, status):
    now = datetime.now(TIMEZONE)
    with sqlite3.connect('tasks.db') as conn:
        c = conn.cursor()
        c.execute("SELECT status FROM users WHERE id = ?", (user_id,))
        old_status = c.fetchone()
        if old_status is None or old_status[0] != status:
            c.execute(
                "INSERT OR REPLACE INTO users (id, surname, status, last_update) VALUES (?, ?, ?, ?)",
                (user_id, surname, status, now.isoformat())
            )
            date_on = now if status == "Подключен" else None
            date_off = now if status == "Отключен" else None
            try:
                update_sheet_row(surname, status, date_on=date_on,
                                 date_off=date_off)  # Убедитесь в правильности этой функции
                logger.info(f"Статус пользователя {surname} обновлен в Google Sheets: {status}")
            except Exception as e:
                logger.error(f"Ошибка при обновлении статуса в Google Sheets: {e}")
        logger.info(f"Статус пользователя {surname} обновлен: {status}")


def get_interval_string(interval: int) -> str:
    if interval == 1:
        return "**1 день**"
    elif 2 <= interval <= 4:
        return f"**{interval} дня**"
    else:
        return f"**{interval} дней**"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    welcome_message = (
        "*ПРИВЕТ*! 😊\nНа помощь спешит бот, который будет напоминать выполнять рутину по контексту,\n"
        "💪✨\n\n🗓️ Если нужно что-то изменить или добавить, в конце месяца соберу ОС! 🌟"
    )
    await update.message.reply_text(welcome_message)

    specialists = load_specialists()
    keyboard = [[telegram.InlineKeyboardButton(spec['surname'], callback_data=f"specialist:{spec['surname']}")] for spec
                in specialists]
    reply_markup = telegram.InlineKeyboardMarkup(keyboard)

    await update.message.reply_text('Теперь выбери свою фамилию', reply_markup=reply_markup)
    return CHOOSING_SPECIALIST


async def send_reminder_list(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data['chat_id']
    projects = context.job.data['projects']

    with sqlite3.connect('tasks.db') as conn:
        c = conn.cursor()
        placeholders = ','.join('?' for _ in projects)
        c.execute(f""" SELECT t.task, t.interval FROM tasks t WHERE t.project IN ({placeholders}) """, projects)

        tasks = c.fetchall()

        if tasks:
            message_lines = []
            message_lines.append("*ВАШИ НАПОМИНАНИЯ*\n\n")

            unique_tasks = {task[0].lower(): (task[0], task[1]) for task in
                            tasks}  # Уникальные задачи по нижнему регистру

            for task_name, (original_name, interval) in unique_tasks.items():
                task_name_upper = original_name.capitalize()  # Используем оригинальное имя
                interval_string = get_interval_string(interval)  # Получаем правильную форму интервала

                # Форматируем строку для вывода
                message_lines.append(f"• {task_name_upper} - {interval_string}\n")  # Жирный текст для интервала

            message = "".join(message_lines)
            await context.bot.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')


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

        init_tasks_for_specialist(specialist)  # Инициализация задач для выбранного специалиста

        # Отправка списка напоминаний через 5 секунд
        context.job_queue.run_once(send_reminder_list, 5,
                                   data={'projects': specialist['projects'], 'chat_id': query.message.chat.id})

        # Запуск регулярных проверок каждые 48 часов
        context.job_queue.run_repeating(check_reminders,
                                        interval=48,
                                        first=5,
                                        data={'projects': specialist['projects'], 'chat_id': query.message.chat.id},
                                        name=str(query.message.chat.id))

        update_user_status(query.from_user.id, specialist['surname'], "Подключен")

    return ConversationHandler.END


async def send_reminder(context: ContextTypes.DEFAULT_TYPE,
                        chat_id: int,
                        task: str,
                        projects: list,
                        interval: int) -> None:
    projects_list = "\n".join(f"- {project}" for project in sorted(projects))

    message = f"*📋ПОРА {task.upper()}*\n\n{projects_list}"

    try:
        await context.bot.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')
    except telegram.error.Forbidden:
        logger.warning(f"Пользователь {chat_id} заблокировал бота")


async def check_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now(TIMEZONE)

    if START_TIME <= now.time() <= END_TIME:
        logger.info(f"Проверка напоминаний в {now}")

        with sqlite3.connect('tasks.db') as conn:
            c = conn.cursor()
            projects = context.job.data['projects']
            placeholders = ','.join('?' for _ in projects)

            c.execute(
                f""" SELECT t.id, t.project, t.task, t.interval FROM tasks t WHERE t.next_reminder <= ? AND t.project IN ({placeholders}) """,
                (now.isoformat(), *projects))

            tasks = c.fetchall()
            logger.info(f"Найдено задач для напоминания: {len(tasks)}")

            reminders = {}

            for task_id, project, task_name, interval in tasks:
                if task_name not in reminders:
                    reminders[task_name] = {"projects": set(), "ids": [], "interval": interval}

                reminders[task_name]["projects"].add(project)
                reminders[task_name]["ids"].append(task_id)

            for task_name, reminder_data in reminders.items():
                await send_reminder(context,
                                    context.job.data['chat_id'],
                                    task_name,
                                    list(reminder_data["projects"]),
                                    reminder_data["interval"])

                next_reminder_time = now + timedelta(days=reminder_data["interval"])

                with sqlite3.connect('tasks.db') as conn:
                    c = conn.cursor()
                    for task_id in reminder_data["ids"]:
                        c.execute("UPDATE tasks SET next_reminder = ? WHERE id = ?",
                                  (next_reminder_time.isoformat(), task_id))
                    conn.commit()

    else:
        logger.info(
            f"Текущее время {now.time()} не соответствует времени отправки напоминаний ({START_TIME}-{END_TIME})")


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception while handling an update: {context.error}")


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_surname = context.user_data.get('surname', 'Неизвестный пользователь')

    # Обновляем статус пользователя на "Отключен"
    await update_user_status(update.message.from_user.id, user_surname, "Отключен")
    await update.message.reply_text("Вы отключены от бота. Если захотите снова подключиться , просто напишите /start.")


def main() -> None:
    init_db()
    logger.info(f"Бот запущен. Текущее время : {datetime.now(TIMEZONE)}")

    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING_SPECIALIST: [CallbackQueryHandler(specialist_choice)],
        },
        fallbacks=[],
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("stop", stop))
    application.add_error_handler(error_handler)

    if os.environ.get('RENDER'):
        port = int(os.environ.get('PORT', 10000))
        webhook_url = os.environ.get("WEBHOOK_URL")
        application.run_webhook(listen="0.0.0.0", port=port, webhook_url=webhook_url,
                                secret_token=os.environ.get("SECRET_TOKEN"))
    else:
        application.run_polling()


if __name__ == '__main__':
    main()