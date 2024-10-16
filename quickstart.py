import os
import json
import logging
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from dotenv import load_dotenv

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
load_dotenv()

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

SPREADSHEET_ID = os.getenv('SPREADSHEET_ID')
RANGE_NAME = os.getenv('RANGE_NAME')

def get_credentials():
    creds = None
    token_json = os.getenv('GOOGLE_TOKEN')
    if token_json:
        try:
            token_data = json.loads(token_json)
            creds = Credentials.from_authorized_user_info(token_data, SCOPES)
        except json.JSONDecodeError:
            logger.error("Ошибка при разборе JSON из GOOGLE_TOKEN")
            raise ValueError("Invalid JSON in GOOGLE_TOKEN environment variable")

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise ValueError("Invalid credentials. Please update GOOGLE_TOKEN in .env file.")
    return creds

def write_to_sheet(values):
    try:
        creds = get_credentials()
        logger.info("Credentials получены успешно")
        service = build('sheets', 'v4', credentials=creds)
        logger.info("Сервис Google Sheets создан")

        body = {
            'values': values
        }
        result = service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range=RANGE_NAME,
            valueInputOption='USER_ENTERED',
            body=body).execute()

        logger.info(f"Запрос к API выполнен. Результат: {result}")
        return result
    except HttpError as error:
        logger.error(f"Произошла ошибка при записи в таблицу: {error}")
        return error

if __name__ == '__main__':
    # Пример использования
    test_data = [['Тестовая запись', 'Проект', 'Задача', '01.01.2023']]
    result = write_to_sheet(test_data)
    if isinstance(result, dict):
        print("Данные успешно записаны в таблицу")
    else:
        print("Произошла ошибка при записи данных")