import sqlite3

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
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Проверка существования столбцов
        cursor.execute("PRAGMA table_info(events)")
        columns = [column[1] for column in cursor.fetchall()]
        required_columns = {'max_participants', 'end_date', 'event_time'}

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

    def add_event(self, max_participants, end_date, event_time):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO events (max_participants, end_date, event_time)
            VALUES (?, ?, ?)
        ''', (max_participants, end_date, event_time))
        self.conn.commit()
        return cursor.lastrowid

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
                COUNT(r.user_id) as current_participants
            FROM events e
            LEFT JOIN registrations r ON e.id = r.event_id
            GROUP BY e.id
        ''')
        return cursor.fetchall()

    def register_user(self, user_id, username, event_id):
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
            SELECT username FROM registrations
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
        cursor.execute('''
            SELECT e.id, e.end_date, e.event_time
            FROM events e
            JOIN registrations r ON e.id = r.event_id
            WHERE r.user_id = ?
        ''', (user_id,))
        return cursor.fetchall()

    def delete_registration(self, user_id, event_id):
        cursor = self.conn.cursor()
        cursor.execute('''
            DELETE FROM registrations
            WHERE user_id = ? AND event_id = ?
        ''', (user_id, event_id))
        self.conn.commit()
        return cursor.rowcount

    def update_event_field(self, event_id, field, value):
        cursor = self.conn.cursor()
        cursor.execute(f'''
            UPDATE events SET {field} = ? WHERE id = ?
        ''', (value, event_id))
        self.conn.commit()

    # def delete_old_events(self):
    #     cursor = self.conn.cursor()
    #     week_ago = datetime.now() - timedelta(days=7)
    #     cursor.execute('''
    #         DELETE FROM events WHERE end_date < ?
    #     ''', (week_ago,))
    #     deleted = cursor.rowcount
    #     self.conn.commit()
    #     return deleted

    def get_event_by_id(self, event_id):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT
                e.id,
                e.max_participants,
                e.end_date,
                e.event_time,
                COUNT(r.user_id) as current_participants
            FROM events e
            LEFT JOIN registrations r ON e.id = r.event_id
            WHERE e.id = ?
            GROUP BY e.id
        ''', (event_id,))
        result = cursor.fetchone()
        if result:
            return {
                'id': result[0],
                'max_participants': result[1],
                'end_date': result[2],  # сохраняем как строку
                'event_time': result[3],  # добавляем время
                'current_participants': result[4]
            }
        return None
