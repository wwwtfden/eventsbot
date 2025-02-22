import sqlite3
from datetime import datetime

DATABASE_NAME = "events.db"


def get_events(show_all=False, hours=6):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()

    query = '''
        SELECT 
            e.id,
            e.max_participants,
            e.end_date,
            e.event_time,
            e.info,
            COUNT(r.user_id) as participants,
            datetime(e.end_date || ' ' || e.event_time) as event_datetime
        FROM events e
        LEFT JOIN registrations r ON e.id = r.event_id
    '''

    params = ()
    if not show_all:
        query += " WHERE datetime(e.end_date || ' ' || e.event_time) <= datetime('now', ?)"
        params = (f"-{hours} hours",)

    query += " GROUP BY e.id ORDER BY event_datetime DESC"

    cursor.execute(query, params)
    events = cursor.fetchall()
    conn.close()
    return events


def delete_event(event_id):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()

    try:
        # Проверка существования мероприятия
        cursor.execute("SELECT id FROM events WHERE id = ?", (event_id,))
        if not cursor.fetchone():
            print(f"⚠️ Мероприятие {event_id} не найдено!")
            return

        cursor.execute("DELETE FROM events WHERE id = ?", (event_id,))
        cursor.execute("DELETE FROM registrations WHERE event_id = ?", (event_id,))
        conn.commit()
        print(f"✅ Мероприятие {event_id} и связанные записи удалены!")
    except Exception as e:
        print(f"❌ Ошибка удаления: {str(e)}")
    finally:
        conn.close()


def main():
    while True:
        print("\nУправление мероприятиями")
        print("1. Показать все мероприятия")
        print("2. Показать мероприятия старше 6 часов")
        print("3. Удалить мероприятие")
        print("4. Выход")

        choice = input("Выберите действие: ")

        if choice == "1":
            events = get_events(show_all=True)
            print_events(events, "Все мероприятия:")

        elif choice == "2":
            events = get_events(show_all=False)
            print_events(events, "Мероприятия старше 6 часов:")

        elif choice == "3":
            event_id = input("Введите ID мероприятия для удаления: ")
            if not event_id.isdigit():
                print("❌ Некорректный ID!")
                continue
            delete_event(int(event_id))

        elif choice == "4":
            break

        else:
            print("❌ Некорректный выбор!")


def print_events(events, header):
    if not events:
        print("\n⚠️ Мероприятия не найдены")
        return

    print(f"\n{header}")
    for event in events:
        status = "🟢 Активно" if datetime.fromisoformat(event[6]) > datetime.now() else "🔴 Завершено"
        print(f"\nID: {event[0]} | {status}")
        print(f"Дата: {event[2]} {event[3]}")
        print(f"Участников: {event[5]}/{event[1]}")
        print(f"Описание: {event[4]}")


if __name__ == "__main__":
    main()