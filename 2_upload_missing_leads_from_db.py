# Скрипт выгружает из SQLite лиды без статуса в Google-таблицу клиента.
import logging
from logging.handlers import TimedRotatingFileHandler
import math
import os
import random
import re
import sqlite3
import time
from typing import Dict, List, NamedTuple, Optional, Tuple

from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


DB_PATH = "lr186.db"
STATUS_COLUMN_NAME = "статус отправки в таблицу клиента"
STATUS_VALUE = "sent_bot"
ADD_ROWS_COUNT = 500

# Нужен полный доступ к таблицам, потому что скрипт записывает данные.
GOOGLE_SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

MAX_API_RETRIES = 4
API_RETRY_BACKOFF_SECONDS = 1.0

LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "upload_missing_leads_from_db.log")

os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

file_handler = TimedRotatingFileHandler(
    LOG_FILE,
    when="midnight",
    backupCount=7,
    encoding="utf-8",
)
file_handler.setFormatter(
    logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
)

console_handler = logging.StreamHandler()
console_handler.setFormatter(
    logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
)

logger.handlers.clear()
logger.addHandler(file_handler)
logger.addHandler(console_handler)


class LeadRow(NamedTuple):
    source_id: str
    event_dt: str
    phone: str
    channel: str
    developer_project: str
    sheet_row: Optional[int]


def get_env_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"В переменной окружения {name} пусто или нет значения")
    return value


def validate_spreadsheet_id(spreadsheet_id: str, env_name: str) -> str:
    clean_id = spreadsheet_id.strip()
    if not re.fullmatch(r"[A-Za-z0-9-_]{20,}", clean_id):
        raise ValueError(f"{env_name} должен быть только ID таблицы без URL")
    return clean_id


def quote_sheet_name(sheet_name: str) -> str:
    escaped_name = sheet_name.replace("'", "''")
    return f"'{escaped_name}'"


def normalize_header(header: str) -> str:
    return re.sub(r"\s+", " ", header.strip().lower())


def column_index_to_letter(index: int) -> str:
    """Переводит номер столбца 1 -> A, 2 -> B, 27 -> AA."""
    if index < 1:
        raise ValueError("Номер столбца должен быть больше 0")

    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def create_sheets_service(credentials_file: str):
    if not os.path.exists(credentials_file):
        raise FileNotFoundError(f"Файл credentials не найден: {credentials_file}")
    credentials = service_account.Credentials.from_service_account_file(
        credentials_file,
        scopes=GOOGLE_SHEETS_SCOPES,
    )
    return build("sheets", "v4", credentials=credentials)


def execute_with_retries(request_factory, action_name: str):
    last_error = None
    for attempt in range(1, MAX_API_RETRIES + 1):
        try:
            return request_factory().execute()
        except HttpError as exc:
            status = exc.resp.status if exc.resp else None
            if status in (429, 500, 502, 503, 504):
                last_error = exc
            else:
                raise
        except OSError as exc:
            last_error = exc

        if attempt < MAX_API_RETRIES:
            base_delay = API_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
            sleep_time = random.uniform(0, base_delay)
            logger.warning(
                "Временная ошибка API (%s). Повтор через %.2f сек...",
                action_name,
                sleep_time,
            )
            time.sleep(sleep_time)

    raise RuntimeError(
        f"Не удалось выполнить запрос к API ({action_name}) "
        f"после {MAX_API_RETRIES} попыток: {last_error}"
    )


def get_sheet_info(service, spreadsheet_id: str, sheet_name: str) -> Tuple[int, int]:
    spreadsheet = execute_with_retries(
        lambda: service.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(sheetId,title,gridProperties(rowCount)))",
        ),
        "get_sheet_info",
    )

    for sheet in spreadsheet.get("sheets", []):
        properties = sheet.get("properties", {})
        if properties.get("title") == sheet_name:
            row_count = properties.get("gridProperties", {}).get("rowCount", 0)
            return properties["sheetId"], row_count

    raise ValueError(f"Лист не найден: {sheet_name}")


def read_column_b_values(service, spreadsheet_id: str, sheet_name: str) -> List[List[str]]:
    result = execute_with_retries(
        lambda: service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=f"{quote_sheet_name(sheet_name)}!B:B",
        ),
        "read_client_column_b",
    )
    return result.get("values", [])


def find_first_empty_row_in_column_b(values: List[List[str]]) -> int:
    for row_number, row in enumerate(values, start=1):
        if not row or not str(row[0]).strip():
            return row_number
    return len(values) + 1


def ensure_sheet_has_rows(
    service,
    spreadsheet_id: str,
    sheet_id: int,
    current_row_count: int,
    required_last_row: int,
) -> None:
    if required_last_row <= current_row_count:
        return

    rows_to_add = required_last_row - current_row_count
    rows_to_add = max(ADD_ROWS_COUNT, math.ceil(rows_to_add / ADD_ROWS_COUNT) * ADD_ROWS_COUNT)

    execute_with_retries(
        lambda: service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "appendDimension": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "length": rows_to_add,
                        }
                    }
                ]
            },
        ),
        "append_client_rows",
    )
    logger.info("В таблицу клиента добавлено строк: %s", rows_to_add)


def get_missing_leads(conn: sqlite3.Connection) -> List[LeadRow]:
    cursor = conn.execute(
        """
        SELECT
            source_id,
            event_dt,
            phone,
            COALESCE(channel, ''),
            COALESCE(developer_project, ''),
            sheet_row
        FROM leads
        WHERE skorozvon_info IS NULL OR TRIM(skorozvon_info) = ''
        ORDER BY event_dt, source_id
        """
    )
    return [LeadRow(*row) for row in cursor.fetchall()]


def write_leads_to_client_sheet(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    start_row: int,
    leads: List[LeadRow],
) -> int:
    values = [
        [
            lead.event_dt,
            lead.phone,
            lead.channel,
            lead.developer_project,
        ]
        for lead in leads
    ]
    end_row = start_row + len(values) - 1

    response = execute_with_retries(
        lambda: service.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=f"{quote_sheet_name(sheet_name)}!B{start_row}:E{end_row}",
            valueInputOption="RAW",
            body={"values": values},
        ),
        "write_client_values",
    )
    updated_rows = response.get("updatedRows", 0)
    if updated_rows != len(values):
        raise RuntimeError(
            "Google API подтвердил запись не всех строк: "
            f"{updated_rows} из {len(values)}"
        )
    return updated_rows


def mark_leads_sent_in_db(conn: sqlite3.Connection, leads: List[LeadRow]) -> int:
    if not leads:
        return 0

    before = conn.total_changes
    conn.executemany(
        """
        UPDATE leads
        SET skorozvon_info = ?
        WHERE source_id = ?
        """,
        [(STATUS_VALUE, lead.source_id) for lead in leads],
    )
    conn.commit()
    return conn.total_changes - before


def read_source_headers(service, spreadsheet_id: str, sheet_name: str) -> List[str]:
    result = execute_with_retries(
        lambda: service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=f"{quote_sheet_name(sheet_name)}!1:1",
        ),
        "read_source_headers",
    )
    rows = result.get("values", [])
    return rows[0] if rows else []


def find_status_column(headers: List[str]) -> int:
    for index, header in enumerate(headers, start=1):
        if normalize_header(header) == STATUS_COLUMN_NAME:
            return index
    raise ValueError(f"Не найден столбец: {STATUS_COLUMN_NAME}")


def mark_leads_sent_in_source_sheet(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    leads: List[LeadRow],
) -> int:
    headers = read_source_headers(service, spreadsheet_id, sheet_name)
    status_column = column_index_to_letter(find_status_column(headers))

    update_data = []
    skipped_without_row = 0
    for lead in leads:
        if not lead.sheet_row:
            skipped_without_row += 1
            continue
        update_data.append(
            {
                "range": (
                    f"{quote_sheet_name(sheet_name)}!"
                    f"{status_column}{lead.sheet_row}"
                ),
                "values": [[STATUS_VALUE]],
            }
        )

    if skipped_without_row:
        logger.warning(
            "Не смогли отметить в источнике строки без sheet_row: %s",
            skipped_without_row,
        )

    if not update_data:
        return 0

    execute_with_retries(
        lambda: service.spreadsheets()
        .values()
        .batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "valueInputOption": "RAW",
                "data": update_data,
            },
        ),
        "mark_source_values",
    )
    return len(update_data)


def main() -> None:
    load_dotenv()

    source_spreadsheet_id = validate_spreadsheet_id(
        get_env_required("GOOGLE_SHEET_ID"),
        "GOOGLE_SHEET_ID",
    )
    source_sheet_name = get_env_required("GOOGLE_SHEET_NAME")
    client_spreadsheet_id = validate_spreadsheet_id(
        get_env_required("GOOGLE_SHEET_ID_CLIENT"),
        "GOOGLE_SHEET_ID_CLIENT",
    )
    client_sheet_name = get_env_required("GOOGLE_SHEET_NAME_CLIENT")
    credentials_file = get_env_required("GOOGLE_CREDENTIALS_FILE")

    service = create_sheets_service(credentials_file)

    conn = sqlite3.connect(DB_PATH)
    try:
        leads = get_missing_leads(conn)
        if not leads:
            logger.info("В БД нет лидов с пустым skorozvon_info.")
            return

        logger.info("Найдено лидов для отправки клиенту: %s", len(leads))

        client_sheet_id, client_row_count = get_sheet_info(
            service,
            client_spreadsheet_id,
            client_sheet_name,
        )
        column_b_values = read_column_b_values(
            service,
            client_spreadsheet_id,
            client_sheet_name,
        )
        start_row = find_first_empty_row_in_column_b(column_b_values)
        end_row = start_row + len(leads) - 1

        ensure_sheet_has_rows(
            service,
            client_spreadsheet_id,
            client_sheet_id,
            client_row_count,
            end_row,
        )
        written_rows = write_leads_to_client_sheet(
            service,
            client_spreadsheet_id,
            client_sheet_name,
            start_row,
            leads,
        )
        logger.info(
            "Лиды записаны в таблицу клиента в диапазон B%s:E%s: %s",
            start_row,
            end_row,
            written_rows,
        )

        marked_in_db = mark_leads_sent_in_db(conn, leads)
        logger.info("Отмечено в локальной БД: %s", marked_in_db)

        try:
            marked_in_source = mark_leads_sent_in_source_sheet(
                service,
                source_spreadsheet_id,
                source_sheet_name,
                leads,
            )
            logger.info("Отмечено в таблице-источнике: %s", marked_in_source)
        except Exception:
            logger.exception(
                "Лиды уже записаны клиенту и отмечены в БД, "
                "но не удалось отметить источник."
            )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
