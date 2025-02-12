import logging
import configparser
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
    JobQueue,
    PicklePersistence
)
import sqlite3
from datetime import datetime, timedelta, time

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.DEBUG  # Измените на DEBUG для детальной информации
)

logger = logging.getLogger(__name__)

config = configparser.ConfigParser()
config.read('bot_config.ini', encoding='utf-8')
TOKEN = config['Main']['TOKEN']
ADMIN_ID = config.getint('Main', 'ADMIN_ID')  # Преобразование в int
DATABASE_NAME = config['Main']['DATABASE_NAME']

persistence = PicklePersistence(filepath="conversationbot")

# Добавляем новые константы для меню
USER_COMMANDS = [
    ("📅 Список мероприятий", "events"),
    ("📌 Мои записи", "myevents"),
    ("ℹ️ Помощь", "help")
]

ADMIN_COMMANDS = USER_COMMANDS + [
    ("🛠 Управление мероприятиями", "adminevents"),
    ("➕ Создать мероприятие", "createevent")
]

# Состояния для ConversationHandler
(
    CREATE_NAME, CREATE_MAX, CREATE_END, CREATE_TIME,
    EDIT_CHOICE, EDIT_VALUE, DELETE_CONFIRM
) = range(7)


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


class Database:
    def __init__(self):
        self.conn = sqlite3.connect(DATABASE_NAME)
        self.create_tables()

    def create_tables(self):
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                max_participants INTEGER NOT NULL,
                end_date DATE NOT NULL,
                event_time TIME NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Проверка существования столбцов
        cursor.execute("PRAGMA table_info(events)")
        columns = [column[1] for column in cursor.fetchall()]
        required_columns = {'name', 'max_participants', 'end_date', 'event_time'}

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

    def add_event(self, name, max_participants, end_date, event_time):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO events (name, max_participants, end_date, event_time)
            VALUES (?, ?, ?, ?)
        ''', (name, max_participants, end_date, event_time))
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
                e.name,
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
            SELECT e.id, e.name, e.end_date
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

    def delete_old_events(self):
        cursor = self.conn.cursor()
        week_ago = datetime.now() - timedelta(days=7)
        cursor.execute('''
            DELETE FROM events WHERE end_date < ?
        ''', (week_ago,))
        deleted = cursor.rowcount
        self.conn.commit()
        return deleted

    def get_event_by_id(self, event_id):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT
                e.id,
                e.name,
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
                'name': result[1],
                'max_participants': result[2],
                'end_date': result[3],  # сохраняем как строку
                'event_time': result[4],
                'current_participants': result[5]
            }
        return None

db = Database()

# Обработчики команд

async def check_admin_access(update: Update) -> bool:
    user = update.effective_user
    if not is_admin(user.id):
        message = update.message or update.callback_query.message
        await message.reply_text("⛔ У вас нет прав администратора!")
        return False
    return True

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Создание мероприятия отменено.")
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("✖️ Редактирование отменено")
    context.user_data.clear()
    return ConversationHandler.END

async def show_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        events = db.get_all_events()
        message = update.message or update.callback_query.message

        if not events:
            await message.reply_text("Сейчас нет доступных мероприятий.")
            return

        keyboard = []
        for event in events:
            event_id, name, max_p, end_date, event_time, current = event
            available = max_p - current
            # Форматируем дату и время
            formatted_date = datetime.strptime(end_date, "%Y-%m-%d").strftime("%d.%m.%Y")
            event_text = f"{name}\n {formatted_date} {event_time}\n {available} своб." #🎫 Свободно: {available}/{max_p}
            keyboard.append([InlineKeyboardButton(event_text, callback_data=f"event_{event_id}")])

        reply_markup = InlineKeyboardMarkup(keyboard)
        await message.reply_text("Выберите мероприятие:", reply_markup=reply_markup)

    except Exception as e:
        logger.error(f"Error in show_events: {str(e)}", exc_info=True)
        await message.reply_text("❌ Произошла ошибка при загрузке мероприятий")


async def event_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data.startswith("event_"):
        event_id = int(query.data.split("_")[1])
        available = db.check_available_slots(event_id)

        if available > 0:
            success = db.register_user(
                query.from_user.id,
                query.from_user.username,
                event_id
            )

            if success:
                await query.edit_message_text(
                    f"✅ Вы успешно записаны на мероприятие! Осталось мест: {available - 1}"
                )
            else:
                await query.edit_message_text("⚠️ Вы уже записаны на это мероприятие!")
        else:
            await query.edit_message_text("⚠️ К сожалению, все места заняты!")


# Админские функции
def get_event_by_id(self, event_id):
    cursor = self.conn.cursor()
    cursor.execute('''
        SELECT
            e.id,
            e.name,
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
            'name': result[1],
            'max_participants': result[2],
            'end_date': result[3],  # сохраняем как строку
            'event_time': result[4],
            'current_participants': result[5]
        }
    return None

async def handle_back_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await admin_events(update, context)


async def admin_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data.startswith("view_"):
        # Проверка прав администратора
        if not await check_admin_access(update):
            return

        event_id = int(query.data.split("_")[1])
        participants = db.get_event_participants(event_id)

        participants_text = "Участники:\n" + "\n".join(
            [f"{i + 1}. @{username}" for i, username in enumerate(participants)]
        ) if participants else "Нет участников"

        await query.edit_message_text(
            text=f"📋 Список участников мероприятия:\n\n{participants_text}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("↩️ Назад", callback_data="adminevents")]
            ])
        )
        return

    elif query.data == "adminevents":
        await admin_events(update, context)

    elif query.data.startswith("delete_"):
        event_id = int(query.data.split("_")[1])
        context.user_data['delete_event_id'] = event_id
        keyboard = [
            [InlineKeyboardButton("✅ Да", callback_data="confirm_delete")],
            [InlineKeyboardButton("❌ Нет", callback_data="cancel_delete")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "Вы уверены, что хотите удалить это мероприятие?",
            reply_markup=reply_markup
        )
        return DELETE_CONFIRM  # Возвращаем состояние подтверждения


async def confirm_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        await query.answer()

        if query.data == "confirm_delete":
            event_id = context.user_data.get('delete_event_id')
            if event_id:
                event_exists = any(e[0] == event_id for e in db.get_all_events())
                if not event_exists:
                    await query.edit_message_text("❌ Мероприятие уже было удалено")
                    return ConversationHandler.END
                else:
                    db.delete_event(event_id)
                    await query.edit_message_text("✅ Мероприятие успешно удалено!")

            else:
                await query.edit_message_text("❌ Ошибка: мероприятие не найдено")
        else:
            await query.edit_message_text("❌ Удаление отменено")

        context.user_data.clear()
        return ConversationHandler.END
    except Exception as e:
            logger.error(f"Error deleting event: {str(e)}")
            await query.edit_message_text("❌ Ошибка при удалении мероприятия")


async def create_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Проверка прав администратора
    if not await check_admin_access(update):
        return ConversationHandler.END

    # Очищаем предыдущие данные
    context.user_data.clear()

    # Получаем сообщение из callback_query или message
    query = update.callback_query
    if query:
        await query.answer()
        message = query.message
    else:
        message = update.message

    await message.reply_text("Введите название мероприятия:")
    return CREATE_NAME  # Явное возвращение первого состояния


async def create_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['event_name'] = update.message.text
        logger.info(f"Received event name: {context.user_data['event_name']}")

        await update.message.reply_text("Введите максимальное количество участников:")
        return CREATE_MAX
    except Exception as e:
        logger.error(f"Error in create_name: {str(e)}")
        return ConversationHandler.END


async def create_max(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        max_p = int(update.message.text)
        if max_p <= 0:
            raise ValueError
        context.user_data['event_max'] = max_p
        logger.info(f"Received max participants: {max_p}")

        await update.message.reply_text("Введите дату мероприятия (ГГГГ-ММ-ДД):")
        return CREATE_END
    except ValueError:
        await update.message.reply_text("❌ Некорректное число! Введите целое положительное число:")
        return CREATE_MAX


async def create_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Received end date: {update.message.text}")
    try:
        end_date = datetime.strptime(update.message.text, "%Y-%m-%d").date()
        today = datetime.now().date()

        if end_date < today:
            await update.message.reply_text("Дата не может быть в прошлом!")
            return CREATE_END

        context.user_data['end_date'] = end_date
        # message = update.message or update.callback_query.message
        await update.message.reply_text("Введите время мероприятия в формате ЧЧ:ММ:")
        return CREATE_TIME
    except ValueError:
        # message = update.message or update.callback_query.message
        await update.message.reply_text("Некорректный формат даты! Используйте ГГГГ-ММ-ДД:")
        return CREATE_END


async def create_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    time_str = update.message.text
    logger.info(f"Received time: {time_str}")
    try:
        # Парсим время и преобразуем в строку
        event_time = datetime.strptime(time_str, "%H:%M").time()
        time_formatted = event_time.strftime("%H:%M")  # Конвертируем в строку

        # Проверяем наличие всех необходимых данных
        if not all(key in context.user_data for key in ['event_name', 'event_max', 'end_date']):
            logger.error("Missing required data in context")
            await update.message.reply_text("❌ Ошибка: потеряны данные мероприятия. Начните заново.")
            return ConversationHandler.END

        # Получаем данные из контекста
        name = context.user_data['event_name']
        max_p = context.user_data['event_max']
        end_date = context.user_data['end_date'].strftime("%Y-%m-%d")  # Конвертируем дату в строку

        # Добавляем мероприятие в БД
        db.add_event(name, max_p, end_date, time_formatted)
        await update.message.reply_text("✅ Мероприятие успешно создано!")
        return ConversationHandler.END

    except ValueError:
        await update.message.reply_text("❌ Некорректный формат времени! Используйте ЧЧ:ММ:")
        return CREATE_TIME
    except Exception as e:
        logger.error(f"Error in create_time: {str(e)}", exc_info=True)
        await update.message.reply_text("❌ Произошла внутренняя ошибка при создании мероприятия")
        return ConversationHandler.END

async def admin_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not await check_admin_access(update):
            return

        events = db.get_all_events()
        if not events:
            message = update.message or update.callback_query.message
            await message.reply_text("Нет мероприятий для управления.")
            return

        keyboard = []
        for event in events:
            event_id, name, max_p, end_date, event_time, current = event
            text = f"{name} ({current}/{max_p})"
            keyboard.append([
                InlineKeyboardButton(text, callback_data=f"view_{event_id}"),
                InlineKeyboardButton("✏️", callback_data=f"edit_{event_id}"),
                InlineKeyboardButton("❌", callback_data=f"delete_{event_id}")
            ])

        reply_markup = InlineKeyboardMarkup(keyboard)
        message = update.message or update.callback_query.message
        await message.reply_text("Управление мероприятиями:", reply_markup=reply_markup)

    except Exception as e:
        logger.error(f"Error in admin_events: {str(e)}", exc_info=True)
        await message.reply_text("❌ Ошибка при загрузке панели управления")


async def my_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message or update.callback_query.message
    events = db.get_user_events(update.effective_user.id)

    if not events:
        await message.reply_text("Вы не зарегистрированы ни на одно мероприятие")
        return

    keyboard = []
    for event in events:
        event_id, name, end_date = event
        text = f"{name} ( {end_date.split()[0]})"
        keyboard.append([InlineKeyboardButton(text, callback_data=f"unreg_{event_id}")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await message.reply_text("Ваши мероприятия:", reply_markup=reply_markup)


async def cancel_registration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    event_id = int(query.data.split("_")[1])
    db.delete_registration(update.effective_user.id, event_id)

    await query.edit_message_text("Регистрация отменена!")


async def edit_event_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        event_id = int(query.data.split("_")[1])
        # Проверка существования мероприятия
        if not any(e[0] == event_id for e in db.get_all_events()):
            await query.edit_message_text("❌ Мероприятие не найдено!")
            return ConversationHandler.END

        context.user_data['edit_event_id'] = event_id
        # Показываем меню редактирования
        keyboard = [
            [InlineKeyboardButton("Название", callback_data="field_name")],
            [InlineKeyboardButton("Макс. участников", callback_data="field_max_participants")],
            [InlineKeyboardButton("Дата окончания", callback_data="field_end_date")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Выберите поле для редактирования:", reply_markup=reply_markup)
        return EDIT_CHOICE

    except (IndexError, ValueError):
        await query.edit_message_text("❌ Ошибка в обработке команды")
        return ConversationHandler.END


async def edit_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Получаем данные о мероприятии
    event_id = context.user_data.get('edit_event_id')
    event = db.get_event_by_id(event_id)  # Нужно реализовать этот метод

    field = query.data.split("field_")[1]
    context.user_data['edit_field'] = field

    # Получаем текущее значение
    current_value = {
        'name': event['name'],
        'max_participants': event['max_participants'],
        'end_date': event['end_date'].strftime("%Y-%m-%d"),
        'event_time': event['event_time']
    }[field]

    field_name = {
        'name': 'название',
        'max_participants': 'максимальное количество участников',
        'end_date': 'дату окончания',
        'event_time': 'время мероприятия'
    }[field]

    await query.edit_message_text(
        f"Текущее {field_name}: {current_value}\n"
        f"Введите новое значение:"
    )
    return EDIT_VALUE


async def edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = context.user_data
    field = user_data.get('edit_field')
    event_id = user_data.get('edit_event_id')
    value = update.message.text
    event = db.get_event_by_id(event_id)

    try:
        if field == 'max_participants':
            new_max = int(value)
            current = event['current_participants']

            if new_max < current:
                await update.message.reply_text(
                    f"❌ Ошибка: {current} участников уже записано!\n"
                    f"Минимальное значение: {current}\n"
                    f"Попробуйте снова:"
                )
                return EDIT_VALUE

            if new_max <= 0:
                await update.message.reply_text("❌ Число должно быть больше 0!")
                return EDIT_VALUE

            db.update_event_field(event_id, field, new_max)
            await update.message.reply_text("✅ Максимальное количество участников обновлено!")

        elif field == 'end_date':
            new_date = datetime.strptime(value, "%Y-%m-%d").date()
            if new_date < datetime.now().date():
                await update.message.reply_text("❌ Дата не может быть в прошлом!")
                return EDIT_VALUE

            db.update_event_field(event_id, field, new_date)
            await update.message.reply_text("✅ Дата окончания обновлена!")

        elif field == 'name':
            if len(value) < 3:
                await update.message.reply_text("❌ Название слишком короткое (мин. 3 символа)!")
                return EDIT_VALUE

            db.update_event_field(event_id, field, value)
            await update.message.reply_text("✅ Название обновлено!")

        elif field == 'event_time':
            datetime.strptime(value, "%H:%M")
            db.update_event_field(user_data['edit_event_id'], 'event_time', value)
            await update.message.reply_text("Время мероприятия обновлено!")

    except ValueError as e:
        error_msg = {
            'max_participants': "❌ Введите целое положительное число!",
            'end_date': "❌ Неверный формат даты! Используйте ГГГГ-ММ-ДД",
            'event_time': "❌ Неверный формат времени! Используйте ЧЧ:ММ"
        }.get(field, "❌ Ошибка ввода")

        await update.message.reply_text(f"{error_msg}\nПопробуйте снова:")
        return EDIT_VALUE

    return ConversationHandler.END

#
# async def auto_cleanup(context: ContextTypes.DEFAULT_TYPE):
#     deleted = db.delete_old_events()
#     if deleted > 0:
#         await context.bot.send_message(
#             ADMIN_ID,
#             f"Автоматически удалено {deleted} старых мероприятий"
#         )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    keyboard = []

    if user.id == ADMIN_ID:
        buttons = [InlineKeyboardButton(text, callback_data=cmd) for text, cmd in ADMIN_COMMANDS]
    else:
        buttons = [InlineKeyboardButton(text, callback_data=cmd) for text, cmd in USER_COMMANDS]

    # Разбиваем кнопки на ряды по 2
    for i in range(0, len(buttons), 2):
        keyboard.append(buttons[i:i + 2])

    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        "Привет! Я бот для записи на коня.\n"
        "Выберите действие:"
    )
    message = update.message or update.callback_query.message
    await message.reply_text(text, reply_markup=reply_markup)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Определяем сообщение и пользователя
    if update.callback_query:
        message = update.callback_query.message
        user = update.callback_query.from_user
    else:
        message = update.message
        user = update.effective_user

    help_text = [
        "📋 Доступные команды:",
        "/start - Главное меню",
        "/events - Показать все мероприятия",
        "/myevents - Показать мои записи"
    ]

    if is_admin(user.id):
        help_text.extend([
            "\n⚙️ Админ-команды:",
            "/adminevents - Управление мероприятиями",
            "/createevent - Создать новое мероприятие"
        ])

    help_text.append("\nℹ️ Выберите действие из меню или используйте команды!")

    # Создаем клавиатуру в зависимости от прав
    keyboard = []
    if is_admin(user.id):
        buttons = [InlineKeyboardButton(text, callback_data=cmd) for text, cmd in ADMIN_COMMANDS]
    else:
        buttons = [InlineKeyboardButton(text, callback_data=cmd) for text, cmd in USER_COMMANDS]

    # Разбиваем кнопки на ряды по 2
    for i in range(0, len(buttons), 2):
        keyboard.append(buttons[i:i + 2])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await message.reply_text("\n".join(help_text), reply_markup=reply_markup)


async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        # Добавляем логирование
        logging.info(f"User {query.from_user.id} pressed button: {query.data}")

        command = query.data
        user_id = query.from_user.id

        if command == "help":
            await help_command(update, context)
        elif command == "events":
            await show_events(update, context)
        elif command == "myevents":
            await my_events(update, context)
        elif command == "adminevents":
            if user_id == ADMIN_ID:
                await admin_events(update, context)
            else:
                await query.edit_message_text("⛔ Доступ запрещен!")
        elif command == "createevent":
            if user_id == ADMIN_ID:
                await create_event(update, context)
            else:
                await query.edit_message_text("⛔ Доступ запрещен!")
        else:
            await query.edit_message_text("⚠️ Команда не распознана")

    except Exception as e:
        logging.error(f"Error: {str(e)}", exc_info=True)
        await query.edit_message_text("❌ Произошла ошибка при обработке запроса")


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.message:
            await update.message.reply_text("❌ Произошла внутренняя ошибка")
        elif update.callback_query:
            await update.callback_query.message.reply_text("❌ Произошла внутренняя ошибка")
    except Exception as e:
        logger.error(f"Error in error handler: {str(e)}")

def main():
    application = (
        Application.builder()
        .token(TOKEN)
        .persistence(persistence)
        .build()
    )

    application.add_error_handler(error_handler)

    # Регистрация обработчиков
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("events", show_events))
    application.add_handler(CommandHandler("myevents", my_events))

    # Административные обработчики
    application.add_handler(CommandHandler("adminevents", admin_events))

    create_event_conv = ConversationHandler(
        entry_points=[
            CommandHandler("createevent", create_event),
            CallbackQueryHandler(create_event, pattern="^createevent$")
        ],
        states={
            CREATE_NAME: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    create_name
                )
            ],
            CREATE_MAX: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    create_max
                )
            ],
            CREATE_END: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    create_end
                )
            ],
            CREATE_TIME: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    create_time
                )
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        persistent=True,
        name="create_event_conv"
    )
    application.add_handler(create_event_conv)

    edit_event_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(edit_event_start, pattern=r"^edit_\d+$")  # Только edit_ с цифрами
        ],
        states={
            EDIT_CHOICE: [
                CallbackQueryHandler(edit_choice, pattern=r"^field_(name|max_participants|end_date|event_time)$")
            ],
            EDIT_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_value)
            ]
        },
        fallbacks=[
            CommandHandler('cancel', cancel),
            CallbackQueryHandler(cancel_edit, pattern="^cancel_edit$")
        ],
        persistent=True,
        name="edit_event_conv"
    )
    application.add_handler(edit_event_conv)

    delete_event_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_actions, pattern="^delete_")
        ],
        states={
            DELETE_CONFIRM: [
                CallbackQueryHandler(confirm_delete, pattern="^(confirm_delete|cancel_delete)$")
            ]
        },
        fallbacks=[],
        map_to_parent={  # Возвращаемся к родительскому состоянию
            ConversationHandler.END: ConversationHandler.END
        }
    )
    application.add_handler(delete_event_conv)

    # Обработчики callback-запросов
    application.add_handler(CallbackQueryHandler(event_button, pattern="^event_"))
    application.add_handler(CallbackQueryHandler(edit_event_start, pattern="^edit_"))
    application.add_handler(CallbackQueryHandler(cancel_registration, pattern="^unreg_"))
    application.add_handler(CallbackQueryHandler(admin_actions, pattern="^(view|delete)_"))
    application.add_handler(CallbackQueryHandler(menu_handler))
    application.add_handler(CallbackQueryHandler(handle_back_button, pattern="^adminevents$"))

    # Запуск бота
    application.run_polling()

if __name__ == "__main__":
    main()
