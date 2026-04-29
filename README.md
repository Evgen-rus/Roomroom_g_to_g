# Roomroom_g_to_g

Небольшой проект для переноса лидов из Google Sheets в локальную SQLite-базу и дальнейшей отправки новых лидов в Google-таблицу клиента.

## Что делают скрипты

`1_save_gsheet_to_sqlite.py` читает исходную Google-таблицу, берёт лиды за последние несколько дней и сохраняет их в локальную базу `lr186.db`.

`2_upload_missing_leads_from_db.py` берёт из `lr186.db` лиды с пустым полем `skorozvon_info`, записывает их в таблицу клиента и отмечает отправленные строки статусом `sent_bot`.

## Основные переменные `.env`

```env
GOOGLE_CREDENTIALS_FILE=credentials/service-account.json
GOOGLE_SHEET_ID=...
GOOGLE_SHEET_NAME=...
GOOGLE_SHEET_ID_CLIENT=...
GOOGLE_SHEET_NAME_CLIENT=...
```

В `.env` нельзя хранить лишние данные или публиковать его в репозиторий, потому что там могут быть доступы и ID рабочих таблиц.

## Запуск вручную

```bash
python 1_save_gsheet_to_sqlite.py
python 2_upload_missing_leads_from_db.py
```

## Пример crontab для сервера

Открыть расписание:

```bash
crontab -e
```

Добавить:

```bash
# ==== ROOMROOM_G_TO_G SCHEDULE ====
CRON_TZ=Europe/Moscow
32 8-17 * * * cd /opt/Roomroom_g_to_g && /opt/Roomroom_g_to_g/venv/bin/python /opt/Roomroom_g_to_g/1_save_gsheet_to_sqlite.py >> /opt/Roomroom_g_to_g/logs/to_sqlite_cron.log 2>&1
34 8-17 * * * cd /opt/Roomroom_g_to_g && /opt/Roomroom_g_to_g/venv/bin/python /opt/Roomroom_g_to_g/2_upload_missing_leads_from_db.py >> /opt/Roomroom_g_to_g/logs/leads_from_db_cron.log 2>&1
```
