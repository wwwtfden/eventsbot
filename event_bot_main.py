import database
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
    level=logging.DEBUG # .INFO после исправления багов
)

logger = logging.getLogger(__name__)

config = configparser.ConfigParser()
config.read('bot_config.ini', encoding='utf-8')

TOKEN = config['Main']['TOKEN']

try:
    ADMIN_IDS = [
        int(admin_id.strip())
        for admin_id in config.get('Main', 'ADMIN_ID').split(',')
        if admin_id.strip().isdigit()
    ]
except (configparser.NoOptionError, configparser.NoSectionError):
    ADMIN_IDS = []

DATABASE_NAME = config['Main']['DATABASE_NAME']

persistence = PicklePersistence(filepath="conversationbot")

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
    CREATE_MAX, CREATE_END, CREATE_TIME, CREATE_INFO,
    EDIT_CHOICE, EDIT_VALUE, DELETE_CONFIRM,
    WAITING_FOR_MESSAGE, WAITING_FOR_LINK, CONFIRM_LINK
) = range(10)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS
    #return user_id == ADMIN_ID

global db
db = None
# db = database.Database(DATABASE_NAME)

# Отправка уведомлений

async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    try:
        logger.info(f"Попытка отправить напоминание для мероприятия {context.job.data}")
        event_id = context.job.data
        event = db.get_event_by_id(event_id)
        if not event:
            logger.error(f"Мероприятие {event_id} не найдено!")
            return

        event_date = datetime.strptime(event['end_date'], "%Y-%m-%d").date()
        event_time = datetime.strptime(event['event_time'], "%H:%M").time()
        # Проверка актуальности времени
        event_datetime = datetime.combine(
            event_date,
            event_time
        )
        if event_datetime < datetime.now():
            logger.warning(f"Мероприятие {event_id} уже завершилось")
            return

        # Отправка сообщений участникам
        participants = db.get_event_participant_ids(event_id)
        for user_id in participants:
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"Привет! 🐴\n"
                        f"Напоминаю, что рабочая сессия начнется в {event_time}\n"
                        f"Ссылку на связь пришлю тебе за 5-10 минут до созвона, не пропусти 🤎"
                        f"Оля #КоньНеВалялся"
                        f"* время по мск"
                        f"* можно прийти позже/уйти раньше, но желательно об этом написать"
                    )
                )
            except Exception as e:
                logger.error(f"Ошибка отправки пользователю {user_id}: {str(e)}")
    except Exception as e:
        logger.error(f"Критическая ошибка в send_reminder: {str(e)}", exc_info=True)

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
            event_id, max_p, end_date, event_time, info, current = event
            available = max_p - current
            formatted_date = datetime.strptime(end_date, "%Y-%m-%d").strftime("%d.%m.%Y")
            event_text = f"{formatted_date} {event_time} | Мест: {available}/{max_p}\nОписание: {info}"
            keyboard.append([InlineKeyboardButton(event_text, callback_data=f"event_{event_id}")])

        reply_markup = InlineKeyboardMarkup(keyboard)
        await message.reply_text("Выберите мероприятие:", reply_markup=reply_markup)

    except Exception as e:
        logger.error(f"Ошибка в show_events: {str(e)}", exc_info=True)
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


async def handle_back_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await admin_events(update, context)


async def admin_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data.startswith("view_"):
        if not await check_admin_access(update):
            return

        event_id = int(query.data.split("_")[1])
        participants = db.get_event_participants(event_id)

        participants_text = "Участники:\n" + "\n".join(
            [f"{i + 1}. @{username}" for i, username in enumerate(participants)]
        ) if participants else "Нет участников"

        keyboard = [
            [InlineKeyboardButton("📨 Отправить сообщение", callback_data=f"sendmsg_{event_id}")],
            [InlineKeyboardButton("🔗 Отправить ссылку", callback_data=f"sendlink_{event_id}")],
            [InlineKeyboardButton("↩️ Назад", callback_data="adminevents")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            text=f"📋 Список участников мероприятия:\n\n{participants_text}",
            reply_markup=reply_markup
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
        return DELETE_CONFIRM


async def send_message_to_participants(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await check_admin_access(update):
        return ConversationHandler.END

    event_id = int(query.data.split('_')[1])
    context.user_data['sendmsg_event_id'] = event_id

    await query.edit_message_text("✍️ Введите сообщение для участников:")
    return WAITING_FOR_MESSAGE


async def send_link_to_participants(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await check_admin_access(update):
        return ConversationHandler.END

    event_id = int(query.data.split('_')[1])
    context.user_data['sendlink_event_id'] = event_id

    await query.edit_message_text("🔗 Введите ссылку для участников:")
    return WAITING_FOR_LINK


async def confirm_link_sending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    event_id = context.user_data.get('sendlink_event_id')
    participants = db.get_event_participant_ids(event_id)
    message_text = context.user_data.get('generated_message', "Ссылка: {link}").format(
        link=context.user_data.get('link', '')
    )

    success = 0
    for user_id in participants:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=message_text,
                disable_web_page_preview=False
            )
            success += 1
        except Exception as e:
            logger.error(f"Ошибка отправки пользователю {user_id}: {str(e)}")

    await query.edit_message_text(f"✅ Сообщение отправлено {success} участникам!")
    return ConversationHandler.END


async def process_link_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    link = update.message.text
    context.user_data['link'] = link

    try:
        with open("link-template.txt", "r", encoding="utf-8") as f:
            template = f.read()
    except FileNotFoundError:
        template = "Ссылка на мероприятие: {link}"

    message_text = template.format(link=link)
    context.user_data['generated_message'] = message_text

    keyboard = [
        [InlineKeyboardButton("✅ Отправить", callback_data="confirm_link")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_link")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"📝 Сообщение для отправки:\n\n{message_text}\n\nПодтвердите отправку:",
        reply_markup=reply_markup
    )
    return CONFIRM_LINK


async def send_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_text = update.message.text
    event_id = context.user_data.get('sendmsg_event_id')
    participant_ids = db.get_event_participant_ids(event_id)

    # Исключаем администратора
    participant_ids = [uid for uid in participant_ids if uid not in ADMIN_IDS]

    # Отправка сообщений
    success, failed = 0, 0
    for user_id in participant_ids:
        try:
            await context.bot.send_message(chat_id=user_id, text=message_text)
            success += 1
        except Exception as e:
            logger.error(f"Ошибка отправки пользователю {user_id}: {e}")
            failed += 1

    await update.message.reply_text(
        f"✅ Сообщение отправлено {success} участникам.\n"
        f"❌ Не удалось отправить: {failed}"
    )
    context.user_data.clear()
    return ConversationHandler.END


async def confirm_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        event_id = context.user_data.get('delete_event_id')

        # Удаляем все задачи мероприятия
        job_name = f"reminder_{event_id}"
        for job in context.job_queue.jobs():
            if job.name == job_name:
                job.schedule_removal()

        # Удаляем из БД
        db.delete_event(event_id)
        await query.edit_message_text("✅ Мероприятие удалено!")

    except Exception as e:
        logger.error(f"Ошибка: {str(e)}", exc_info=True)
        await query.edit_message_text("❌ Ошибка удаления")


async def create_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Проверка прав администратора
    if not await check_admin_access(update):
        return ConversationHandler.END

    # Очищаем предыдущие данные (контекст)
    context.user_data.clear()

    # Получаем сообщение из callback_query или message
    query = update.callback_query
    if query:
        await query.answer()
        message = query.message
    else:
        message = update.message

    await message.reply_text("Введите количество участников:")
    return CREATE_MAX


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


async def create_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    time_str = update.message.text
    try:
        event_time = datetime.strptime(time_str, "%H:%M").time()
        context.user_data['event_time'] = time_str

        await update.message.reply_text("Введите описание мероприятия:")
        return CREATE_INFO

    except ValueError:
        await update.message.reply_text("❌ Неверный формат времени! Используйте ЧЧ:ММ")
        return CREATE_TIME


async def create_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        info = update.message.text
        context.user_data["info"] = info

        # Проверяем наличие всех данных
        if "event_max" not in context.user_data:
            await update.message.reply_text("❌ Ошибка: не указано количество участников!")
            return ConversationHandler.END

        if "end_date" not in context.user_data:
            await update.message.reply_text("❌ Ошибка: не указана дата мероприятия!")
            return ConversationHandler.END

        if "event_time" not in context.user_data:
            await update.message.reply_text("❌ Ошибка: не указано время мероприятия!")
            return ConversationHandler.END

        # Извлекаем данные
        max_p = context.user_data["event_max"]
        end_date = context.user_data["end_date"].strftime("%Y-%m-%d")  # Конвертируем дату в строку
        event_time = context.user_data["event_time"]

        # Добавляем мероприятие в БД
        event_id = db.add_event(max_p, end_date, event_time, info)  # Все 4 параметра!

        # Планируем напоминание
        event_datetime = datetime.combine(
            context.user_data["end_date"],  # Объект date
            datetime.strptime(event_time, "%H:%M").time()
        )
        reminder_time = event_datetime - timedelta(hours=3)

        if reminder_time > datetime.now():
            delta = (reminder_time - datetime.now()).total_seconds()
            context.job_queue.run_once(
                send_reminder,
                when=delta,
                data=event_id,
                name=f"reminder_{event_id}"
            )

        await update.message.reply_text("✅ Мероприятие успешно создано!")
        return ConversationHandler.END

    except Exception as e:
        logger.error(f"Ошибка в create_info: {str(e)}", exc_info=True)
        await update.message.reply_text("❌ Внутренняя ошибка при создании мероприятия.")
        return ConversationHandler.END


async def create_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Received end date: {update.message.text}")
    try:
        end_date = datetime.strptime(update.message.text, "%Y-%m-%d").date()
        today = datetime.now().date()

        if end_date < today:
            await update.message.reply_text("Дата не может быть в прошлом!")
            return CREATE_END

        context.user_data['end_date'] = end_date
        await update.message.reply_text("Введите время мероприятия в формате ЧЧ:ММ:")
        return CREATE_TIME
    except ValueError:
        await update.message.reply_text("Некорректный формат даты! Используйте ГГГГ-ММ-ДД:")
        return CREATE_END


# logger.error(f"🔥 Критическая ошибка в send_reminder: {str(e)}", exc_info=True)


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
            # Распаковываем 6 полей:
            event_id, max_p, end_date, event_time, info, current = event  # Важно: 6 элементов!
            text = f"{end_date} {event_time} ({current}/{max_p})\nОписание: {info[:20]}..."
            keyboard.append([
                InlineKeyboardButton(text, callback_data=f"view_{event_id}"),
                InlineKeyboardButton("✏️", callback_data=f"edit_{event_id}"),
                InlineKeyboardButton("❌", callback_data=f"delete_{event_id}")
            ])

        reply_markup = InlineKeyboardMarkup(keyboard)
        message = update.message or update.callback_query.message
        await message.reply_text("Управление мероприятиями:", reply_markup=reply_markup)

    except Exception as e:
        logger.error(f"Ошибка в admin_events: {str(e)}", exc_info=True)
        await message.reply_text("❌ Ошибка при загрузке панели управления")


async def my_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message or update.callback_query.message
    events = db.get_user_events(update.effective_user.id)

    if not events:
        await message.reply_text("Вы не зарегистрированы ни на одно мероприятие")
        return

    keyboard = []
    for event in events:
        event_id, end_date, event_time = event
        text = f"( {end_date.split()[0]}) {event_time}"
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
        context.user_data['edit_event_id'] = event_id

        # клавиатура берется отсюда
        keyboard = [
            [InlineKeyboardButton("Макс. участников", callback_data="field_max_participants")],
            [InlineKeyboardButton("Дата окончания", callback_data="field_end_date")],
            [InlineKeyboardButton("Время мероприятия", callback_data="field_event_time")],
            [InlineKeyboardButton("Описание", callback_data="field_info")]  # Новая кнопка
        ]

        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Выберите поле для редактирования:", reply_markup=reply_markup)
        return EDIT_CHOICE

    except Exception as e:
        logger.error(f"Error in edit_event_start: {str(e)}", exc_info=True)
        await query.edit_message_text("❌ Произошла ошибка")
        return ConversationHandler.END

async def edit_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        # Извлекаем название поля из callback_data (например, "field_info" -> "info")
        _, field = query.data.split('_', 1)
        context.user_data['edit_field'] = field

        # Проверяем наличие event_id в контексте
        event_id = context.user_data.get('edit_event_id')
        if not event_id:
            await query.edit_message_text("❌ Мероприятие не выбрано!")
            return ConversationHandler.END

        # Получаем данные мероприятия
        event = db.get_event_by_id(event_id)
        if not event:
            await query.edit_message_text("❌ Мероприятие не найдено!")
            return ConversationHandler.END

        # Формируем сообщение с текущим значением поля
        field_data = {
            'max_participants': ('максимальное количество участников', event['max_participants']),
            'end_date': ('дату окончания', event['end_date']),
            'event_time': ('время мероприятия', event['event_time']),
            'info': ('описание', event['info'])
        }

        # Проверяем корректность поля
        if field not in field_data:
            await query.edit_message_text("❌ Некорректное поле для редактирования!")
            return ConversationHandler.END

        # Формируем текст сообщения
        field_name, current_value = field_data[field]
        message_text = (
            f"Текущее {field_name}:\n"
            f"`{current_value}`\n\n"
            f"Введите новое значение:"
        )

        await query.edit_message_text(
            message_text,
            parse_mode="MarkdownV2"  # Для корректного отображения `
        )
        return EDIT_VALUE

    except ValueError as e:
        logger.error(f"Ошибка разбора callback_data: {query.data} -> {str(e)}")
        await query.edit_message_text("❌ Ошибка обработки запроса")
        return ConversationHandler.END

    except Exception as e:
        logger.error(f"Ошибка в edit_choice: {str(e)}", exc_info=True)
        await query.edit_message_text("❌ Внутренняя ошибка")
        return ConversationHandler.END

async def edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_data = context.user_data
        field = user_data.get("edit_field")
        value = update.message.text.strip()
        event_id = user_data.get("edit_event_id")

        if not all([field, event_id]):
            await update.message.reply_text("❌ Сессия редактирования устарела!")
            return ConversationHandler.END

        # Получаем актуальные данные мероприятия
        event = db.get_event_by_id(event_id)
        if not event:
            await update.message.reply_text("❌ Мероприятие не найдено!")
            return ConversationHandler.END

        # Обработка разных полей
        if field == "max_participants":
            try:
                new_max = int(value)
                if new_max < event["current_participants"]:
                    await update.message.reply_text(
                        f"⚠️ Нельзя установить меньше {event['current_participants']} (уже зарегистрированные участники)!"
                    )
                    return EDIT_VALUE
                db.update_event_field(event_id, field, new_max)
                await update.message.reply_text("✅ Лимит участников обновлен!")

            except ValueError:
                await update.message.reply_text("❌ Введите целое число!")
                return EDIT_VALUE

        elif field == "end_date":
            try:
                parsed_date = datetime.strptime(value, "%Y-%m-%d").date()
                if parsed_date < datetime.now().date():
                    await update.message.reply_text("❌ Дата не может быть в прошлом!")
                    return EDIT_VALUE
                db.update_event_field(event_id, field, value)
                await update.message.reply_text("✅ Дата обновлена!")

            except ValueError:
                await update.message.reply_text("❌ Формат: ГГГГ-ММ-ДД")
                return EDIT_VALUE

        elif field == "event_time":
            try:
                datetime.strptime(value, "%H:%M")  # Валидация формата
                db.update_event_field(event_id, field, value)
                await update.message.reply_text("✅ Время обновлено!")

            except ValueError:
                await update.message.reply_text("❌ Формат: ЧЧ:ММ")
                return EDIT_VALUE

        elif field == "info":
            if len(value) > 500:
                await update.message.reply_text("❌ Описание слишком длинное (макс. 500 символов)")
                return EDIT_VALUE
            db.update_event_field(event_id, "info", value)
            await update.message.reply_text("✅ Описание обновлено!")

        # Обновляем напоминание если нужно
        if field in ("end_date", "event_time"):
            event = db.get_event_by_id(event_id)  # Получаем новые данные
            end_date = datetime.strptime(event["end_date"], "%Y-%m-%d").date()
            event_time = datetime.strptime(event["event_time"], "%H:%M").time()
            event_datetime = datetime.combine(end_date, event_time)
            reminder_time = event_datetime - timedelta(hours=3)

            # Удаляем старую задачу
            job_name = f"reminder_{event_id}"
            for job in context.job_queue.jobs():
                if job.name == job_name:
                    job.schedule_removal()

            # Создаем новую задачу если время актуально
            if reminder_time > datetime.now():
                delta = (reminder_time - datetime.now()).total_seconds()
                context.job_queue.run_once(
                    send_reminder,
                    when=delta,
                    data=event_id,
                    name=job_name
                )
                logger.info(f"🔄 Напоминание для {event_id} перепланировано")

        # Возвращаем в админ-панель
        await admin_events(update, context)
        return ConversationHandler.END

    except Exception as e:
        logger.error(f"Ошибка в edit_value: {str(e)}", exc_info=True)
        await update.message.reply_text("❌ Критическая ошибка при обновлении")
        return ConversationHandler.END

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    keyboard = []

    if user.id in ADMIN_IDS:
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
            if user_id in ADMIN_IDS:
                await admin_events(update, context)
            else:
                await query.edit_message_text("⛔ Доступ запрещен!")
        elif command == "createevent":
            if user_id in ADMIN_IDS:
                await create_event(update, context)
            else:
                await query.edit_message_text("⛔ Доступ запрещен!")
        elif command.startswith("sendmsg_"):
            await send_message_to_participants(update, context)
            return
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


async def cancel_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("✖️ Отправка ссылки отменена")
    return ConversationHandler.END


async def restore_reminders(context: ContextTypes.DEFAULT_TYPE):
    try:
        # Создаем новый экземпляр БД
        db = database.Database(DATABASE_NAME)
        events = db.get_all_events()

        for event in events:
            event_id = event[0]
            end_date_str = event[2]
            event_time_str = event[3]

            # Парсим время
            end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
            event_time = datetime.strptime(event_time_str, "%H:%M").time()
            event_datetime = datetime.combine(end_date, event_time)
            reminder_time = event_datetime - timedelta(hours=3)

            # Создаем задачу если время актуально
            if reminder_time > datetime.now():
                delta = (reminder_time - datetime.now()).total_seconds()
                context.job_queue.run_once(
                    send_reminder,
                    when=delta,
                    data=event_id,
                    name=f"reminder_{event_id}"
                )
                logger.info(f"♻️ Восстановлено напоминание для {event_id}")

    except Exception as e:
        logger.error(f"Ошибка восстановления: {str(e)}", exc_info=True)

def main():
    global db
    db = database.Database(DATABASE_NAME)

    application = (
        Application.builder()
        .token(TOKEN)
        .persistence(persistence)
        .build()
    )
    # application.bot_data['db'] = db

    application.job_queue.run_once(
        callback=restore_reminders,
        when=5,
        name="init_restore"
    )

    # application.post_init(restore_reminders)

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
            CREATE_MAX: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_max)],
            CREATE_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_end)],
            CREATE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_time)],
            CREATE_INFO: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_info)]  # Добавлено
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        persistent=True,
        name="create_event_conv"
    )
    application.add_handler(create_event_conv)

    edit_event_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(edit_event_start, pattern=r"^edit_\d+$")  # Обработка кнопки "✏️"
        ],
        states={
            EDIT_CHOICE: [
                CallbackQueryHandler(edit_choice, pattern=r"^field_(max_participants|end_date|event_time|info)$")
            ],
            EDIT_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_value)
            ]
        },
        fallbacks=[
            CommandHandler("cancel", cancel_edit),
            CallbackQueryHandler(cancel_edit, pattern="^cancel_edit$")
        ],
        map_to_parent={  # Важно: возврат в родительский ConversationHandler
            ConversationHandler.END: ConversationHandler.END
        },
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

    send_message_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                send_message_to_participants,
                pattern=r"^sendmsg_\d+$"  # Регулярное выражение для sendmsg_ + цифры
            )
        ],
        states={
            WAITING_FOR_MESSAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, send_message_handler)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        persistent=True,
        name="send_message_conv"
    )
    application.add_handler(send_message_conv)

    send_link_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(send_link_to_participants, pattern=r"^sendlink_\d+$")
        ],
        states={
            WAITING_FOR_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_link_input)],
            CONFIRM_LINK: [CallbackQueryHandler(confirm_link_sending, pattern="^confirm_link$")]
        },
        fallbacks=[
            CallbackQueryHandler(lambda u, c: cancel_link(u, c), pattern="^cancel_link$")
        ],
        map_to_parent={ConversationHandler.END: ConversationHandler.END},
        persistent=True,
        name="send_link_conv"
    )
    application.add_handler(send_link_conv)

    # Обработчики callback-запросов
    application.add_handler(CallbackQueryHandler(event_button, pattern="^event_"))
    application.add_handler(CallbackQueryHandler(edit_event_start, pattern="^edit_"))
    application.add_handler(CallbackQueryHandler(cancel_registration, pattern="^unreg_"))
    application.add_handler(CallbackQueryHandler(admin_actions, pattern="^(view|delete)_"))
    application.add_handler(CallbackQueryHandler(send_message_to_participants, pattern=r"^sendmsg_\d+$"))
    application.add_handler(CallbackQueryHandler(menu_handler))
    application.add_handler(CallbackQueryHandler(handle_back_button, pattern="^adminevents$"))

    # Запуск бота
    application.run_polling()

if __name__ == "__main__":
    main()
