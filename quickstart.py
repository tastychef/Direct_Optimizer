import os
import json
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Определение области доступа
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

# Получение ID таблицы и диапазона из переменных окружения
SPREADSHEET_ID = os.getenv('SPREADSHEET_ID')
RANGE_NAME = os.getenv('RANGE_NAME')

def get_credentials():
    creds = None
    token_path = os.getenv('GOOGLE_TOKEN')
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise ValueError("Invalid credentials. Please update GOOGLE_TOKEN in .env file.")
    return creds

def write_to_sheet(values):
    try:
        creds = get_credentials()
        service = build('sheets', 'v4', credentials=creds)

        body = {
            'values': values
        }
        result = service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range=RANGE_NAME,
            valueInputOption='USER_ENTERED',
            body=body).execute()

        print(f"Данные добавлены на вкладку {RANGE_NAME}. Обновлено ячеек: {result.get('updates').get('updatedCells')}")
        return result
    except HttpError as error:
        print(f"Произошла ошибка при записи в таблицу: {error}")
        return error

if __name__ == '__main__':
    # Пример использования
    test_data = [['Тестовая запись', 'Проект', 'Задача', '01.01.2023']]
    write_to_sheet(test_data)