import logging
import json
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, ConversationHandler, CallbackQueryHandler
import psycopg2
from psycopg2 import sql
from datetime import datetime, timedelta
import quickstart
from dotenv import load_dotenv

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
DB_NAME = os.getenv('DB_NAME')
DB_USER = os.getenv('DB_USER')
DB_PASSWORD = os.getenv('DB_PASSWORD')
DB_HOST = os.getenv('DB_HOST')
DB_PORT = os.getenv('DB_PORT')

def get_db_connection():
    return psycopg2.connect(
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT
    )

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
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("DROP TABLE IF EXISTS tasks")
    cur.execute("DROP TABLE IF EXISTS sent_reminders")

    cur.execute('''CREATE TABLE tasks
                 (id SERIAL PRIMARY KEY, project TEXT, task TEXT, interval INTEGER, next_reminder TIMESTAMP)''')
    cur.execute('''CREATE TABLE sent_reminders
                 (task_id INTEGER PRIMARY KEY, sent_at TIMESTAMP)''')

    conn.commit()
    cur.close()
    conn.close()
    logger.info("База данных инициализирована")

# ИНИЦИАЛИЗАЦИЯ ЗАДАЧ ДЛЯ КОНКРЕТНОГО СПЕЦИАЛИСТА
def init_tasks_for_specialist(specialist):
    conn = get_db_connection()
    cur = conn.cursor()

    tasks = load_tasks()

    for project in specialist['projects']:
        for task in tasks:
            next_reminder = datetime.now() + timedelta(seconds=task['interval_seconds'])
            cur.execute("INSERT INTO tasks (project, task, interval, next_reminder) VALUES (%s, %s, %s, %s)",
                        (project, task['task'], task['interval_seconds'], next_reminder))

    conn.commit()
    cur.close()
    conn.close()
    logger.info(f"Задачи загружены для специалиста {specialist['surname']}")

# ОБРАБОТЧИКИ КОМАНД
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
        context.job_queue.run_repeating(check_reminders, interval=1, first=1,
                                        data={'projects': specialist['projects'], 'chat_id': query.message.chat_id, 'surname': specialist['surname']})

        return ConversationHandler.END
    else:
        await query.edit_message_text('Произошла ошибка. Пожалуйста, напишите @LEX_126.')
        return ConversationHandler.END

async def send_reminder_with_buttons(context: ContextTypes.DEFAULT_TYPE, chat_id: int, project: str, task: str, task_id: int) -> None:
    keyboard = [
        [
            InlineKeyboardButton("✅ Взял(а) в работу", callback_data=f"work:{task_id}"),
            InlineKeyboardButton("⏰ Напомни через 2 часа", callback_data=f"later:{task_id}")
        ],
        [
            InlineKeyboardButton("📅 Напомни завтра", callback_data=f"tomorrow:{task_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await context.bot.send_message(chat_id=chat_id, text=f"Проект: {project}\n*{task}*", reply_markup=reply_markup, parse_mode='Markdown')

async def check_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now()
    logger.info(f"Проверка напоминаний в {now}")
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT t.id, t.project, t.task 
        FROM tasks t
        LEFT JOIN sent_reminders sr ON t.id = sr.task_id
        WHERE t.next_reminder <= %s AND (sr.sent_at IS NULL OR sr.sent_at < t.next_reminder)
    """, (now,))
    tasks = cur.fetchall()

    logger.info(f"Найдено задач для напоминания: {len(tasks)}")

    for task_id, project, task in tasks:
        if project in context.job.data['projects']:
            try:
                await send_reminder_with_buttons(context, context.job.data['chat_id'], project, task, task_id)
                logger.info(f"Отправлено напоминание для проекта {project}, задача: {task}")

                cur.execute("INSERT INTO sent_reminders (task_id, sent_at) VALUES (%s, %s) ON CONFLICT (task_id) DO UPDATE SET sent_at = EXCLUDED.sent_at",
                            (task_id, now))
            except Exception as e:
                logger.error(f"Ошибка при отправке напоминания: {e}")

    conn.commit()
    cur.close()
    conn.close()

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data.startswith("specialist:"):
        return await specialist_choice(update, context)

    action, task_id = query.data.split(':')
    task_id = int(task_id)

    conn = get_db_connection()
    cur = conn.cursor()

    if action == "work":
        cur.execute("SELECT interval, project, task FROM tasks WHERE id = %s", (task_id,))
        interval, project, task = cur.fetchone()
        next_reminder = datetime.now() + timedelta(seconds=interval)
        cur.execute("UPDATE tasks SET next_reminder = %s WHERE id = %s", (next_reminder, task_id))
        cur.execute("DELETE FROM sent_reminders WHERE task_id = %s", (task_id,))
        await query.edit_message_text(text=f"✅ Отлично! Вы взяли задачу в работу.")

        # ЗАПИСЬ В GOOGLE SHEETS
        try:
            surname = context.user_data.get('surname', 'Неизвестный')
            quickstart.write_to_sheet([[surname, project, task, datetime.now().strftime('%d.%m')]])
            logger.info(f"Данные успешно записаны в Google Sheets: {surname}, {project}, {task}")
        except Exception as e:
            logger.error(f"Ошибка при записи в Google Sheets: {e}")

    elif action == "later":
        next_reminder = datetime.now() + timedelta(hours=2)
        cur.execute("UPDATE tasks SET next_reminder = %s WHERE id = %s", (next_reminder, task_id))
        cur.execute("DELETE FROM sent_reminders WHERE task_id = %s", (task_id,))
        await query.edit_message_text(text=f"⏳ Хорошо, я напомню вам об этой задаче через 2 часа.")
    elif action == "tomorrow":
        next_reminder = datetime.now() + timedelta(days=1)
        cur.execute("UPDATE tasks SET next_reminder = %s WHERE id = %s", (next_reminder, task_id))
        cur.execute("DELETE FROM sent_reminders WHERE task_id = %s", (task_id,))
        await query.edit_message_text(text=f"📅 Понял, напомню вам об этой задаче завтра.")

    conn.commit()
    cur.close()
    conn.close()

def main() -> None:
    init_db()  # Теперь только инициализируем базу данных, без загрузки задач

    application = Application.builder().token(BOT_TOKEN).build()

    # ИНИЦИАЛИЗАЦИЯ ОБРАБОТЧИКОВ
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING_SPECIALIST: [CallbackQueryHandler(specialist_choice, pattern=r'^specialist:')],
        },
        fallbacks=[],
        per_message=True
    )

    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(button_callback))

    # ЗАПУСК БОТА
    if os.environ.get('ENVIRONMENT') == 'PRODUCTION':
        port = int(os.environ.get('PORT', 10000))
        application.run_webhook(
            listen="0.0.0.0",
            port=port,
            webhook_url=os.environ.get("WEBHOOK_URL"),
            secret_token=os.environ.get("SECRET_TOKEN")
        )
    else:
        application.run_polling()

if __name__ == '__main__':
    main()