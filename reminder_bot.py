import logging
import json
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, ConversationHandler, CallbackQueryHandler
import sqlite3
from datetime import datetime, timedelta
import quickstart  # Импортируем функции из файла quickstart.py

# Настройка логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Состояния для ConversationHandler
CHOOSING_SPECIALIST = range(1)

# Загрузка специалистов и их проектов
def load_specialists():
    with open('specialists.json', 'r', encoding='utf-8') as file:
        specialists = json.load(file)['specialists']
    return sorted(specialists, key=lambda x: x['surname'])

# Загрузка задач
def load_tasks():
    with open('tasks.json', 'r', encoding='utf-8') as file:
        return json.load(file)['tasks']

# Инициализация базы данных и загрузка задач
def init_db_and_load_tasks():
    conn = sqlite3.connect('tasks.db')
    c = conn.cursor()

    c.execute("DROP TABLE IF EXISTS tasks")
    c.execute("DROP TABLE IF EXISTS sent_reminders")

    c.execute('''CREATE TABLE tasks
                 (id INTEGER PRIMARY KEY, project TEXT, task TEXT, interval INTEGER, next_reminder TEXT)''')
    c.execute('''CREATE TABLE sent_reminders
                 (task_id INTEGER, sent_at TEXT, PRIMARY KEY (task_id))''')

    tasks = load_tasks()
    specialists = load_specialists()

    for specialist in specialists:
        for project in specialist['projects']:
            for task in tasks:
                next_reminder = datetime.now() + timedelta(seconds=task['interval_seconds'])
                c.execute("INSERT INTO tasks (project, task, interval, next_reminder) VALUES (?, ?, ?, ?)",
                          (project, task['task'], task['interval_seconds'], next_reminder.isoformat()))

    conn.commit()
    conn.close()
    logger.info("База данных инициализирована и задачи загружены")

# Обработчики команд
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
    conn = sqlite3.connect('tasks.db')
    c = conn.cursor()

    c.execute("""
        SELECT t.id, t.project, t.task 
        FROM tasks t
        LEFT JOIN sent_reminders sr ON t.id = sr.task_id
        WHERE t.next_reminder <= ? AND (sr.sent_at IS NULL OR sr.sent_at < t.next_reminder)
    """, (now.isoformat(),))
    tasks = c.fetchall()

    logger.info(f"Найдено задач для напоминания: {len(tasks)}")

    for task_id, project, task in tasks:
        if project in context.job.data['projects']:
            try:
                await send_reminder_with_buttons(context, context.job.data['chat_id'], project, task, task_id)
                logger.info(f"Отправлено напоминание для проекта {project}, задача: {task}")

                c.execute("INSERT OR REPLACE INTO sent_reminders (task_id, sent_at) VALUES (?, ?)",
                          (task_id, now.isoformat()))
            except Exception as e:
                logger.error(f"Ошибка при отправке напоминания: {e}")

    conn.commit()
    conn.close()

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data.startswith("specialist:"):
        return await specialist_choice(update, context)

    action, task_id = query.data.split(':')
    task_id = int(task_id)

    conn = sqlite3.connect('tasks.db')
    c = conn.cursor()

    if action == "work":
        c.execute("SELECT interval, project, task FROM tasks WHERE id = ?", (task_id,))
        interval, project, task = c.fetchone()
        next_reminder = datetime.now() + timedelta(seconds=interval)
        c.execute("UPDATE tasks SET next_reminder = ? WHERE id = ?", (next_reminder.isoformat(), task_id))
        c.execute("DELETE FROM sent_reminders WHERE task_id = ?", (task_id,))
        await query.edit_message_text(text=f"✅ Отлично! Вы взяли задачу в работу.")

        # Запись в Google Sheets
        try:
            surname = context.user_data.get('surname', 'Неизвестный')
            quickstart.write_to_sheet([[surname, project, task, datetime.now().strftime('%d.%m')]])
            logger.info(f"Данные успешно записаны в Google Sheets: {surname}, {project}, {task}")
        except Exception as e:
            logger.error(f"Ошибка при записи в Google Sheets: {e}")

    elif action == "later":
        next_reminder = datetime.now() + timedelta(hours=2)
        c.execute("UPDATE tasks SET next_reminder = ? WHERE id = ?", (next_reminder.isoformat(), task_id))
        c.execute("DELETE FROM sent_reminders WHERE task_id = ?", (task_id,))
        await query.edit_message_text(text=f"⏳ Хорошо, я напомню вам об этой задаче через 2 часа.")
    elif action == "tomorrow":
        next_reminder = datetime.now() + timedelta(days=1)
        c.execute("UPDATE tasks SET next_reminder = ? WHERE id = ?", (next_reminder.isoformat(), task_id))
        c.execute("DELETE FROM sent_reminders WHERE task_id = ?", (task_id,))
        await query.edit_message_text(text=f"📅 Понял, напомню вам об этой задаче завтра.")

    conn.commit()
    conn.close()

def main() -> None:
    init_db_and_load_tasks()

    application = Application.builder().token("7669733890:AAE4ATVkJvPP4yido5CUXauLOnw6ECTZdYY").build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING_SPECIALIST: [CallbackQueryHandler(specialist_choice, pattern=r'^specialist:')],
        },
        fallbacks=[],
    )

    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(button_callback))

    application.run_polling()

if __name__ == '__main__':
    main()