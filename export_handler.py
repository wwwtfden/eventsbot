import io
import sqlite3
from openpyxl import Workbook


def generate_export_file(db_conn: sqlite3.Connection) -> io.BytesIO:
    wb = Workbook()

    ws_events = wb.active
    ws_events.title = "Мероприятия"
    ws_events.append([
        "ID", "Макс. участников",
        "Дата", "Время",
        "Описание", "Участники",
        "Создано"
    ])

    # Лист с детальной регистрацией
    ws_participants = wb.create_sheet("Участники")
    ws_participants.append([
        "Event ID", "User ID",
        "Username", "Дата регистрации"
    ])

    events = db_conn.execute('''
        SELECT id, max_participants, end_date, 
               event_time, info, created_at 
        FROM events
        ORDER BY created_at DESC
    ''').fetchall()

    for event in events:
        event_id = event[0]

        participants = db_conn.execute('''
            SELECT user_id, username, registered_at
            FROM registrations
            WHERE event_id = ?
        ''', (event_id,)).fetchall()

        participants_list = "\n".join(
            [f"@{p[1]} (ID: {p[0]})" for p in participants]
        )

        ws_events.append([
            *event[:-1],  # Все поля кроме created_at
            participants_list or "Нет участников",
            event[-1]  # created_at
        ])

        for p in participants:
            ws_participants.append([event_id, *p])

    # Автоматическое выравнивание ширины колонок
    for sheet in wb.worksheets:
        for column in sheet.columns:
            max_length = 0
            for cell in column:
                try:
                    if len(str(cell.value)) > max_length:
                        max_length = len(str(cell.value))
                except:
                    pass
            adjusted_width = (max_length + 2)
            sheet.column_dimensions[column[0].column_letter].width = adjusted_width

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer