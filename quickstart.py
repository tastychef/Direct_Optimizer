import os.path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Если вы изменяете эти области, удалите файл token.json.
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

# ID вашей Google таблицы
SPREADSHEET_ID = '1AijkONeKCIiJO6ztbV6AOpkrPk658uGpqGxon7JkTu8'
RANGE_NAME = 'OPTIMA!A:D'  # Диапазон для записи данных на вкладку OPTIMA


def get_credentials():
    creds = None
    if os.path.exists('token.json'):
        try:
            creds = Credentials.from_authorized_user_file('token.json', SCOPES)
        except ValueError:
            os.remove('token.json')
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token:
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
        range_to_update = f'OPTIMA!A{next_row}'

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