import io
import sqlite3
from datetime import datetime
from openpyxl import Workbook


def generate_export_file(
        db_conn: sqlite3.Connection,
        start_date: str = None,
        end_date: str = None
) -> io.BytesIO:

    wb = Workbook()
    ws_events = wb.active
    ws_events.title = "Мероприятия"

    # Заголовки для листа мероприятий
    ws_events.append([
        "ID",
        "Макс. участников",
        "Дата",
        "Время",
        "Описание",
        "Участники",
        "Создано"
    ])

    # Лист для детальных данных участников
    ws_participants = wb.create_sheet("Участники")
    ws_participants.append([
        "Event ID",
        "User ID",
        "Username",
        "Дата регистрации"
    ])

    # Формируем условия для SQL
    where_clauses = []
    params = []

    if start_date == 'all' or end_date == 'all':
        start_date = end_date = None
    if start_date:
        # where_clauses.append("e.created_at >= ?")
        where_clauses.append("datetime(e.end_date || ' ' || e.event_time) >= ?")
        params.append(start_date)
    if end_date:
        # where_clauses.append("e.created_at <= ?")
        where_clauses.append("datetime(e.end_date || ' ' || e.event_time) <= ?")
        params.append(end_date)

    where_query = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    # Получаем мероприятия
    try:
        events = db_conn.execute(f'''
            SELECT 
                e.id, 
                e.max_participants, 
                e.end_date, 
                e.event_time, 
                e.info, 
                e.created_at 
            FROM events e
            {where_query}
            ORDER BY e.created_at DESC
        ''', tuple(params)).fetchall()

    except sqlite3.Error as e:
        raise RuntimeError(f"Database error: {str(e)}")

    # Обработка данных
    for event in events:
        event_id = event[0]

        # Получаем участников для текущего мероприятия
        participants = db_conn.execute('''
            SELECT user_id, username, registered_at
            FROM registrations
            WHERE event_id = ?
        ''', (event_id,)).fetchall()

        # Форматируем список участников
        participants_list = "\n".join(
            [f"@{p[1]} (ID: {p[0]})" for p in participants]
        ) or "Нет участников"

        # Добавляем данные в лист мероприятий
        ws_events.append([
            event[0],  # ID
            event[1],  # Макс. участников
            event[2],  # Дата
            event[3],  # Время
            event[4],  # Описание
            participants_list,
            event[5]  # Создано
        ])

        # Добавляем детализацию в лист участников
        for p in participants:
            ws_participants.append([event_id, p[0], p[1], p[2]])

    # Автоподбор ширины колонок
    for sheet in wb.worksheets:
        for column in sheet.columns:
            max_length = 0
            for cell in column:
                try:
                    cell_value = str(cell.value) if cell.value else ""
                    max_length = max(max_length, len(cell_value))
                except:
                    pass
            adjusted_width = (max_length + 2)
            sheet.column_dimensions[column[0].column_letter].width = adjusted_width

    # Сохраняем в буфер
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    return buffer