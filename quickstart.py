import os.path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Если вы изменяете эти области, удалите файл token.json.
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

# Получение конфиденциальных данных из .env
SPREADSHEET_ID = os.getenv('SPREADSHEET_ID')
RANGE_NAME = os.getenv('RANGE_NAME')
CREDENTIALS_FILE = os.getenv('CREDENTIALS_FILE')
TOKEN_FILE = os.getenv('TOKEN_FILE')

def get_credentials():
    creds = None
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        except ValueError:
            os.remove(TOKEN_FILE)
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, 'w') as token:
            token.write(creds.to_json())
    return creds

def read_sheet():
    """
    Читает данные из Google таблицы.
    """
    try:
        creds = get_credentials()
        service = build('sheets', 'v4', credentials=creds)

        sheet = service.spreadsheets()
        result = sheet.values().get(spreadsheetId=SPREADSHEET_ID, range=RANGE_NAME).execute()
        values = result.get('values', [])

        if not values:
            print('Данные не найдены.')
            return []

        return values

    except HttpError as err:
        print(f"Произошла ошибка: {err}")
        return None

def write_to_sheet(values):
    """
    Записывает данные в Google таблицу на вкладку OPTIMA.
    :param values: Список списков с данными для записи
    """
    try:
        creds = get_credentials()
        service = build('sheets', 'v4', credentials=creds)

        # Получаем текущие данные
        result = service.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range=RANGE_NAME).execute()
        current_values = result.get('values', [])

        # Определяем, куда добавлять новые данные
        next_row = len(current_values) + 1
        range_to_update = f'{RANGE_NAME.split("!")[0]}!A{next_row}'

        body = {
            'values': values
        }
        result = service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range=range_to_update,
            valueInputOption='USER_ENTERED',
            body=body).execute()

        print(f"Данные добавлены на вкладку OPTIMA. Обновлено ячеек: {result.get('updates').get('updatedCells')}")
        return result
    except HttpError as error:
        print(f"Произошла ошибка при записи в таблицу: {error}")
        return error

def main():
    """
    Основная функция для тестирования.
    """
    # Пример чтения данных
    data = read_sheet()
    if data:
        for row in data:
            print(row)

    # Пример записи данных
    new_data = [
        ['1 декабря', 'Test Project', 'Test Task', 'Test User']
    ]
    write_to_sheet(new_data)

if __name__ == '__main__':
    main()