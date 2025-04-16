import sqlite3
import logging

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

logger = logging.getLogger(__name__)

class Database:
    def __init__(self, DATABASE_NAME):
        self.conn = sqlite3.connect(DATABASE_NAME)
        self.create_tables()

    def create_tables(self):
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                max_participants INTEGER NOT NULL,
                end_date DATE NOT NULL,
                event_time TIME NOT NULL,
                info TEXT NOT NULL,  -- Исправлено: заменен # на --
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Проверка существования столбцов
        cursor.execute("PRAGMA table_info(events)")
        columns = [column[1] for column in cursor.fetchall()]
        required_columns = {'max_participants', 'end_date', 'event_time', 'info'}  # Обновлено

        if not required_columns.issubset(columns):
            cursor.execute('DROP TABLE IF EXISTS events')
            self.create_tables()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS registrations (
                user_id INTEGER NOT NULL,
                event_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                registered_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, event_id),
                FOREIGN KEY (event_id) REFERENCES events(id)
            )
        ''')
        self.conn.commit()

    def add_event(self, max_participants, end_date, event_time, info):
        cursor = self.conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO events 
                (max_participants, end_date, event_time, info)
                VALUES (?, ?, ?, ?)
            ''', (max_participants, end_date, event_time, info))
            self.conn.commit()
            return cursor.lastrowid
        except sqlite3.Error as e:
            logger.error(f"Ошибка добавления мероприятия: {str(e)}")
            raise

    def delete_event(self, event_id):
        cursor = self.conn.cursor()
        cursor.execute('DELETE FROM events WHERE id = ?', (event_id,))
        cursor.execute('DELETE FROM registrations WHERE event_id = ?', (event_id,))
        self.conn.commit()

    def get_all_events(self):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT 
                e.id,
                e.max_participants,
                e.end_date,
                e.event_time,
                e.info,
                COUNT(r.user_id) as current_participants
            FROM events e
            LEFT JOIN registrations r ON e.id = r.event_id
            WHERE datetime(e.end_date || ' ' || e.event_time) > datetime('now', '-6 hours')
            GROUP BY e.id
            ORDER BY e.end_date ASC, e.event_time ASC
        ''')
        return cursor.fetchall()

    def register_user(self, user_id, username, event_id):
        if user_id in self.get_event_participant_ids(event_id):
            return False
        cursor = self.conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO registrations (user_id, event_id, username)
                VALUES (?, ?, ?)
            ''', (user_id, event_id, username))
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def get_event_participants(self, event_id):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT user_id, username FROM registrations
            WHERE event_id = ?
        ''', (event_id,))
        return cursor.fetchall()

    def get_event_participant_ids(self, event_id):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT user_id FROM registrations
            WHERE event_id = ?
        ''', (event_id,))
        return [row[0] for row in cursor.fetchall()]

    def check_available_slots(self, event_id):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT e.max_participants, COUNT(r.user_id)
            FROM events e
            LEFT JOIN registrations r ON e.id = r.event_id
            WHERE e.id = ?
        ''', (event_id,))
        max_p, current = cursor.fetchone()
        return max_p - current

    def get_user_events(self, user_id):
        cursor = self.conn.cursor()
        try:
            cursor.execute('''
                SELECT 
                    e.id, 
                    e.end_date, 
                    e.event_time,
                    COALESCE(e.info, 'Без описания')
                FROM events e
                JOIN registrations r ON e.id = r.event_id
                WHERE r.user_id = ?
                    AND datetime(e.end_date || ' ' || e.event_time) > datetime('now', '-6 hours')
            ''', (user_id,))  # Добавлено условие
            return cursor.fetchall()
        except Exception as e:
            logger.error(f"Ошибка БД: {str(e)}")
            return []
        finally:
            cursor.close()

    # def delete_registration(self, user_id, event_id):
    #     cursor = self.conn.cursor()
    #     cursor.execute('''
    #         DELETE FROM registrations
    #         WHERE user_id = ? AND event_id = ?
    #     ''', (user_id, event_id))
    #     self.conn.commit()
    #     return cursor.rowcount
    def delete_registration(self, user_id, event_id):
        cursor = self.conn.cursor()
        cursor.execute('''
            DELETE FROM registrations 
            WHERE user_id = ? AND event_id = ?
        ''', (user_id, event_id))
        self.conn.commit()
        return cursor.rowcount

    def update_event_field(self, event_id, field, value):
        allowed_fields = {'max_participants', 'end_date', 'event_time', 'info'}
        if field not in allowed_fields:
            raise ValueError(f"Недопустимое поле: {field}")

        cursor = self.conn.cursor()
        cursor.execute(f'''
            UPDATE events 
            SET {field} = ? 
            WHERE id = ?
        ''', (value, event_id))
        self.conn.commit()

    def get_event_by_id(self, event_id):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT 
                e.id,
                e.max_participants,
                e.end_date,
                e.event_time,
                e.info,
                COUNT(r.user_id) as current_participants
            FROM events AS e
            LEFT JOIN registrations AS r 
                ON e.id = r.event_id
            WHERE e.id = ?
            GROUP BY e.id
        ''', (event_id,))
        result = cursor.fetchone()

        if result:
            return {
                'id': result[0],
                'max_participants': result[1],
                'end_date': result[2],
                'event_time': result[3],
                'info': result[4],  # Теперь ключ 'info' гарантированно есть
                'current_participants': result[5]
            }
        return None

    def get_user_id_by_username(self, username):
        cursor = self.conn.cursor()
        cursor.execute("SELECT user_id FROM registrations WHERE username = ?", (username,))
        result = cursor.fetchone()
        return result[0] if result else None

    def is_user_registered(self, user_id: int, event_id: int) -> bool:
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT 1 FROM registrations 
            WHERE user_id = ? AND event_id = ?
        ''', (user_id, event_id))
        return cursor.fetchone() is not None

    def get_username_by_user_id(self, user_id: int) -> str:
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT username FROM registrations 
            WHERE user_id = ? 
            LIMIT 1
        ''', (user_id,))
        result = cursor.fetchone()
        return result[0] if result else None