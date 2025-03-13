import os
import database
import logging
from logging.handlers import RotatingFileHandler
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

import improved_logger as ilg

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        ilg.TimestampedRotatingFileHandler(
            "bot.log",
            maxBytes=5*1024*1024,  # 5 MB
            backupCount=20,
            # encoding="utf-8"
        ),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

config = configparser.ConfigParser()
config.read('bot_config.ini', encoding='utf-8')

TOKEN = config['Main']['TOKEN']
admin_url = config['Main']['HELP_ACCOUNT']
hours_to_remind = (int)(config['Main']['HOURS_REMINDER'])
delay_to_send_notification = (int)(config['Main']['NOTIFICATION_DELAY_SEC'])

try:
    ADMIN_IDS = [
        int(admin_id.strip())
        for admin_id in config.get('Main', 'ADMIN_ID').split(',')
        if admin_id.strip().isdigit()
    ]
except (configparser.NoOptionError, configparser.NoSectionError):
    ADMIN_IDS = []

DATABASE_NAME = config['Main']['DATABASE_NAME']

# Сброс состояния при перезапуске
try:
    os.remove(os.path.join(os.path.dirname(__file__), "conversationbot"))
except FileNotFoundError:
    pass

persistence = PicklePersistence(filepath="conversationbot")

USER_COMMANDS = [
    ("📆 Выбрать сессию", "events"),
    ("🧑‍💻 Мои записи", "myevents"),
    ("ℹ️ Меню", "menu"),
    ("🩹 Нужна помощь", "help") 
]

ADMIN_COMMANDS = USER_COMMANDS + [
    ("🛠 Управление сессиями", "adminevents"),
    ("➕ Создать сессию", "createevent")
]

# Состояния для ConversationHandler
(
    CREATE_MAX, CREATE_END, CREATE_TIME, CREATE_INFO,
    EDIT_CHOICE, EDIT_VALUE, DELETE_CONFIRM,
    WAITING_FOR_MESSAGE, WAITING_FOR_LINK, CONFIRM_LINK,
    REMOVE_USER_START, REMOVE_USER_SELECT
) = range(12)


def build_main_menu_keyboard(is_admin: bool) -> InlineKeyboardMarkup:
    commands = ADMIN_COMMANDS if is_admin else USER_COMMANDS
    buttons = [InlineKeyboardButton(text, callback_data=cmd) for text, cmd in commands]
    keyboard = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(keyboard)


def error_logger(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            return await func(update, context)
        except Exception as e:
            try:
                # Частичный сброс данных текущего пользователя
                if context.user_data:
                    context.user_data.clear()
                if context.chat_data:
                    context.chat_data.clear()
                logger.error(f"Error in {func.__name__}: {str(e)}", exc_info=True)
            except Exception as clear_error:
                logger.error(f"Ошибка при очистке данных: {str(clear_error)}")

            await error_handler(update, context)
    return wrapper


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# def get_message_from_file(filename: str, default_text: str) -> str:
#     try:
#         with open(f"misc/{filename}", "r", encoding="utf-8") as f:
#             return f.read().strip()
#     except FileNotFoundError:
#         return default_text


global db
db = None


# Отправка уведомлений
async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    try:
        event_id = context.job.data
        event = db.get_event_by_id(event_id)

        if not event:
            logger.error(f"Напоминание: мероприятие {event_id} не найдено")
            return

        # Получаем время в формате ЧЧ:ММ
        event_time = datetime.strptime(event['event_time'], "%H:%M").strftime("%H:%M")

        try:
            with open("misc/message.txt", "r", encoding="utf-8") as f:
                template = f.read()
            if "{event_time}" not in template:
                template += "\nВремя начала: {event_time}"
        except FileNotFoundError:
            template = (
                "Привет!\n"
                "Мероприятие начнется в {event_time}.\n"
            )

        message_text = template.format(event_time=event_time)

        # Отправка участникам
        participants = db.get_event_participant_ids(event_id)
        for user_id in participants:
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=message_text
                )
            except Exception as e:
                logger.error(f"Ошибка отправки {user_id}: {str(e)}")

    except Exception as e:
        logger.error(f"Ошибка в send_reminder: {str(e)}", exc_info=True)


async def send_delayed_notification(context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = context.job.data["user_id"]
        message_text = context.job.data["message_text"]
        await context.bot.send_message(chat_id=user_id, text=message_text)
    except Exception as e:
        logger.error(f"Ошибка отправки отложенного уведомления {user_id}: {str(e)}")


# Обработчики команд
async def check_admin_access(update: Update) -> bool:
    user = update.effective_user
    if not is_admin(user.id):
        message = update.message or update.callback_query.message
        await message.reply_text("⛔ У вас нет прав администратора!")
        return False
    return True


@error_logger
async def reset_persistence(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update.effective_user.id):
        await context.application.persistence.drop_user_data()
        await context.application.persistence.drop_chat_data()
        await update.message.reply_text("♻️ Все данные persistence сброшены")


@error_logger
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    logger.info(f"User {user.id} canceled the conversation. Clearing user_data: {context.user_data}")
    context.user_data.clear()
    await update.message.reply_text("❌ Действие отменено.")
    return ConversationHandler.END


@error_logger
async def cancel_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("✖️ Редактирование отменено")
    context.user_data.clear()
    return ConversationHandler.END


@error_logger
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global admin_url
    keyboard = [
        [InlineKeyboardButton(
            "✉️ Связаться с администратором", 
            url=admin_url
        )]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message_text = (
        "Для получения помощи нажмите кнопку ниже, "
        "чтобы написать администратору напрямую:"
    )
    
    await (update.message or update.callback_query.message).reply_text(
        message_text,
        reply_markup=reply_markup
    )


@error_logger
async def show_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        events = db.get_all_events()
        message = update.message or update.callback_query.message
        user = update.effective_user
        is_admin_user = is_admin(user.id)

        if not events:
            await message.reply_text("Сейчас нет доступных сессий.")
            return

        keyboard = []
        for event in events:
            event_id, max_p, end_date, event_time, info, current = event
            available = max_p - current
            formatted_date = datetime.strptime(end_date, "%Y-%m-%d").strftime("%d.%m.%Y")
            event_text = (
                f"{formatted_date} {event_time} | {available}/{max_p} | {info}"
                if is_admin_user
                else f"{formatted_date} {event_time} | {info}"
            )
            keyboard.append([InlineKeyboardButton(event_text, callback_data=f"event_{event_id}")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            with open("misc/events_info.txt", "r", encoding="utf-8") as f:
                text = f.read()
        except FileNotFoundError:
            text = "Выберите мероприятие:"

        await message.reply_text(text, reply_markup=reply_markup)

    except Exception as e:
        logger.error(f"Ошибка в show_events: {str(e)}", exc_info=True)
        await message.reply_text("❌ Произошла ошибка при загрузке мероприятий")


@error_logger
@error_logger
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
                    f"✅ Ты записан(а) на сессию!" #Осталось мест: {available - 1} 
                )
            else:
                keyboard = [
                    [
                        InlineKeyboardButton("✅ Да", callback_data=f"confirm_unreg_{event_id}"),
                        InlineKeyboardButton("❌ Нет", callback_data="cancel_unreg")
                    ]
                ]
                await query.edit_message_text(
                    "⚠️ Ты уже записан(а) на эту сессию. Отменить регистрацию?",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
        else:
            await query.edit_message_text("⚠️ К сожалению, все места заняты!")


async def handle_back_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await admin_events(update, context)


@error_logger
async def admin_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data.startswith("view_"):
        event_id = int(query.data.split("_")[1])

        # Получаем полные данные о мероприятии
        event = db.get_event_by_id(event_id)
        if not event:
            await query.edit_message_text("❌ Сессия не найдена")
            return

        formatted_date = datetime.strptime(event['end_date'], "%Y-%m-%d").strftime("%d.%m.%Y")
        event_time = event['event_time']

        message_text = (
            f"📌 ID сессии: {event_id}\n"
            f"📅 Дата: {formatted_date}\n"
            f"⏰ Время: {event_time}\n"
            f"👥 Участники: {event['current_participants']}/{event['max_participants']}\n"
            f"📝 Описание: {event['info'] or 'Без описания'}\n\n"
            "🗒 Список участников:\n"
        )

        participants = db.get_event_participants(event_id)
        if participants:
            message_text += "\n".join([f"• @{username}" for username in participants])
        else:
            message_text += "Нет участников"

        keyboard = [
            [
                InlineKeyboardButton("📨 Сообщение", callback_data=f"sendmsg_{event_id}"),
                InlineKeyboardButton("🔗 Ссылка", callback_data=f"sendlink_{event_id}")
            ],
            [
                InlineKeyboardButton("🗑 Удалить участника", callback_data=f"removeuser_{event_id}"),
                InlineKeyboardButton("↩️ Назад", callback_data="adminevents")
            ]
        ]
        await query.edit_message_text(
            text=message_text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif query.data.startswith("delete_"):
        event_id = int(query.data.split("_")[1])
        context.user_data['delete_event_id'] = event_id
        keyboard = [
            [InlineKeyboardButton("✅ Да", callback_data="confirm_delete")],
            [InlineKeyboardButton("❌ Нет", callback_data="cancel_delete")]
        ]
        await query.edit_message_text(
            "❓ Вы уверены, что хотите удалить мероприятие?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return DELETE_CONFIRM
    else:
        await query.edit_message_text("❌ Неизвестное действие")


@error_logger
@error_logger
async def handle_unregistration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        if query.data.startswith("confirm_unreg_"):
            event_id = int(query.data.split("_")[-1])  # Безопасное получение ID
            user_id = query.from_user.id
            
            if db.delete_registration(user_id, event_id):
                await query.edit_message_text("✅ Регистрация успешно отменена!")
                await show_events(update, context)
            else:
                await query.edit_message_text("❌ Ошибка отмены регистрации")

        elif query.data == "cancel_unreg":
            await query.edit_message_text("✖️ Действие отменено")
        
    except Exception as e:
        logger.error(f"Ошибка в handle_unregistration: {str(e)}", exc_info=True)
        await query.edit_message_text("❌ Произошла внутренняя ошибка")


@error_logger
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


@error_logger
async def confirm_link_sending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    event_id = context.user_data.get('sendlink_event_id')
    participants = db.get_event_participant_ids(event_id)

    participants = [uid for uid in participants if uid not in ADMIN_IDS]

    message_text = context.user_data.get('generated_message', "Ссылка: {link}").format(
        link=context.user_data.get('link', '')
    )

    success, failed = 0, 0
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
            failed += 1

    await query.edit_message_text(
        f"✅ Сообщение отправлено {success} участникам.\n"
        f"❌ Не удалось отправить: {failed}"
    )
    return ConversationHandler.END


@error_logger
async def process_link_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    link = update.message.text
    context.user_data['link'] = link

    try:
        with open("misc/link-template.txt", "r", encoding="utf-8") as f:
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


@error_logger
async def send_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_text = update.message.text
    event_id = context.user_data.get('sendmsg_event_id')
    participant_ids = db.get_event_participant_ids(event_id)

    participant_ids = [uid for uid in participant_ids if uid not in ADMIN_IDS]

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


@error_logger
async def remove_user_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    event_id = int(query.data.split("_")[1])
    context.user_data["current_event_id"] = event_id

    participants = db.get_event_participants(event_id)

    if not participants:
        await query.edit_message_text("❌ В этом мероприятии нет участников")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton(f"@{username}", callback_data=f"remove_{username}")]
        for username in participants
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "Выберите участника для удаления:", reply_markup=reply_markup)

    return REMOVE_USER_SELECT


@error_logger
async def remove_user_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    username = query.data.split("_")[1]
    event_id = context.user_data["current_event_id"]
    user_id = db.get_user_id_by_username(username)

    if user_id:
        db.delete_registration(user_id, event_id)

        # Сообщение при удалении админом
        try:
            with open("misc/user_banned.txt", "r", encoding="utf-8") as f:
                message_text = f.read().strip()
        except FileNotFoundError:
            message_text = "Тебя удалили"

        try:
            await context.bot.send_message(chat_id=user_id, text=message_text)
        except Exception as e:
            logger.error(f"Не удалось отправить {user_id}: {str(e)}")

        await query.edit_message_text(f"✅ Участник @{username} удален!")
    else:
        await query.edit_message_text("❌ Пользователь не найден")

    return ConversationHandler.END


@error_logger
async def confirm_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    event_id = context.user_data.get('delete_event_id')
    if not event_id:
        await query.edit_message_text("❌ Сессия не найдена.")
        return

    try:
        db.delete_event(event_id)
        
        # Удаление всех связанных jobs
        job_name = f"reminder_{event_id}"
        for job in context.job_queue.jobs():
            if job.name == job_name:
                job.schedule_removal()
        
        logger.info(f"Мероприятие {event_id} удалено. Jobs очищены.")
        await query.edit_message_text("✅ Мероприятие удалено!")

    except Exception as e:
        logger.error(f"Ошибка удаления мероприятия {event_id}: {str(e)}")
        await query.edit_message_text("❌ Не удалось удалить мероприятие.")

    finally:
        context.user_data.clear()
        return ConversationHandler.END


@error_logger
async def create_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin_access(update):
        return ConversationHandler.END

    context.user_data.clear()
    
    try:
        query = update.callback_query
        if query:
            await query.answer()
            message = query.message
        else:
            message = update.message
        
        await message.reply_text("Введите количество участников:")
        return CREATE_MAX
        
    except Exception as e:
        logger.error(f"Ошибка в create_event: {str(e)}")
        await message.reply_text("❌ Ошибка инициализации.")
        context.user_data.clear()
        return ConversationHandler.END


@error_logger
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


@error_logger
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


@error_logger
async def create_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        info = update.message.text
        context.user_data["info"] = info

        if "event_max" not in context.user_data:
            await update.message.reply_text("❌ Ошибка: не указано количество участников!")
            return ConversationHandler.END

        if "end_date" not in context.user_data:
            await update.message.reply_text("❌ Ошибка: не указана дата мероприятия!")
            return ConversationHandler.END

        if "event_time" not in context.user_data:
            await update.message.reply_text("❌ Ошибка: не указано время мероприятия!")
            return ConversationHandler.END

        max_p = context.user_data["event_max"]
        end_date = context.user_data["end_date"].strftime("%Y-%m-%d")  # Конвертируем дату в строку
        event_time = context.user_data["event_time"]

        event_id = db.add_event(max_p, end_date, event_time, info)  # Все 4 параметра!

        # Планируем напоминание
        event_datetime = datetime.combine(
            context.user_data["end_date"],
            datetime.strptime(event_time, "%H:%M").time()
        )
        reminder_time = event_datetime - timedelta(hours=hours_to_remind)

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


@error_logger
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


@error_logger
async def admin_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not await check_admin_access(update):
            return

        # Очистка устаревших состояний
        context.user_data.clear()
        
        events = db.get_all_events()

        if not events:
            message = update.message or update.callback_query.message
            await message.reply_text("Нет мероприятий для управления.")
            return

        keyboard = []
        for event in events:
            event_id, max_p, end_date, event_time, info, current = event
            available = max_p - current

            day_month = end_date.split("-")[2] + "." + end_date.split("-")[1]
            event_text = f"{day_month} {event_time}"

            # Добавляем кнопки в один ряд
            keyboard.append([
                InlineKeyboardButton(
                    event_text,
                    callback_data=f"view_{event_id}"
                ),
                InlineKeyboardButton("✏️", callback_data=f"edit_{event_id}"),
                InlineKeyboardButton("❌", callback_data=f"delete_{event_id}")
            ])

        reply_markup = InlineKeyboardMarkup(keyboard)
        message = update.message or update.callback_query.message
        await message.reply_text("Управление мероприятиями:", reply_markup=reply_markup)

    except Exception as e:
        logger.error(f"Ошибка в admin_events (User {update.effective_user.id}): {str(e)}")
        await update.message.reply_text("❌ Ошибка загрузки меню.")
        context.user_data.clear()


@error_logger
async def my_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        events = db.get_user_events(user_id)
        message = update.message or update.callback_query.message

        if not events:
            await message.reply_text("📭 Ты не зарегистрирован(а) ни на одну сессию!")
            return

        keyboard = []

        for event in events:
            try:
                if len(event) < 4:
                    continue

                event_id, end_date, event_time, info = event
                info_display = info[:20] + "..." if len(info) > 20 else info

                formatted_date = datetime.strptime(end_date, "%Y-%m-%d").strftime("%d.%m.%Y")
                
                # Кнопка с информацией о мероприятии
                btn_text = f"{formatted_date} {event_time} | {info_display}"
                # Кнопка для отмены регистрации
                keyboard.append([
                    InlineKeyboardButton(btn_text, callback_data=f"detail_{event_id}"),
                    InlineKeyboardButton("❌", callback_data=f"cancel_{event_id}")
                ])

            except Exception as e:
                logger.error(f"Ошибка обработки мероприятия {event}: {str(e)}", exc_info=True)
                continue

        if not keyboard:
            await message.reply_text("❌ Ошибка формирования списка мероприятий")
            return

        await message.reply_text(
            "📌 Твои сессии:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    except Exception as e:
        logger.error(f"Критическая ошибка в my_events: {str(e)}", exc_info=True)
        await message.reply_text("⚠️ Произошла внутренняя ошибка")


@error_logger
async def show_event_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    event_id = int(query.data.split("_")[1])
    event = db.get_event_by_id(event_id)
    
    if not event:
        await query.edit_message_text("❌ Мероприятие не найдено")
        return

    formatted_date = datetime.strptime(event['end_date'], "%Y-%m-%d").strftime("%d.%m.%Y")
    message_text = (
        f"📌 Детали сессии:\n\n"
        f"📅 Дата: {formatted_date}\n"
        f"⏰ Время: {event['event_time']}\n"
        f"📝 Описание: {event['info'] or 'Без описания'}\n\n"
        f"Статус: ✅ Записан"
    )
    
    await query.edit_message_text(message_text)


@error_logger
async def cancel_registration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    event_id = int(query.data.split("_")[1])
    user_id = update.effective_user.id
    db.delete_registration(user_id, event_id)

    # Формируем сообщение
    try:
        with open("misc/user_leaved.txt", "r", encoding="utf-8") as f:
            message_text = f.read().strip()
    except FileNotFoundError:
        message_text = "Ты удалился"

    # Планируем отправку через 5 минут
    context.job_queue.run_once(
        callback=send_delayed_notification,
        when=delay_to_send_notification,  # 300 секунд = 5 минут
        data={
            "user_id": user_id,
            "message_text": message_text
        },
        name=f"delayed_msg_{user_id}_{datetime.now().timestamp()}"
    )

    await query.edit_message_text("✅ Регистрация отменена!")
    await my_events(update, context)



@error_logger
async def edit_event_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    context.user_data.clear()
    
    try:
        event_id = int(query.data.split("_")[1])
        context.user_data['edit_event_id'] = event_id
        
        keyboard = [
            [InlineKeyboardButton("Макс. участников", callback_data="field_max_participants")],
            [InlineKeyboardButton("Дата сессии", callback_data="field_end_date")],
            [InlineKeyboardButton("Время сессии", callback_data="field_event_time")],
            [InlineKeyboardButton("Описание", callback_data="field_info")]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Выберите поле для редактирования:", reply_markup=reply_markup)
        return EDIT_CHOICE
        
    except Exception as e:
        logger.error(f"Ошибка в edit_event_start: {str(e)}", exc_info=True)
        await query.edit_message_text("❌ Не удалось начать редактирование.")
        context.user_data.clear()
        return ConversationHandler.END
    

@error_logger
async def edit_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _, field = query.data.split('_', 1)
        context.user_data['edit_field'] = field

        event_id = context.user_data.get('edit_event_id')
        if not event_id:
            await query.edit_message_text("❌ Мероприятие не выбрано!")
            return ConversationHandler.END

        event = db.get_event_by_id(event_id)
        if not event:
            await query.edit_message_text("❌ Мероприятие не найдено!")
            return ConversationHandler.END

        field_data = {
            'max_participants': ('максимальное количество участников', event['max_participants']),
            'end_date': ('дату сессии', event['end_date']),
            'event_time': ('время сессии', event['event_time']),
            'info': ('описание', event['info'])
        }

        if field not in field_data:
            await query.edit_message_text("❌ Некорректное поле для редактирования!")
            return ConversationHandler.END

        field_name, current_value = field_data[field]
        message_text = (
            f"Текущее {field_name}:\n"
            f"`{current_value}`\n\n"
            f"Введите новое значение:"
        )

        await query.edit_message_text(
            message_text,
            parse_mode="MarkdownV2"  # Для корректного отображения
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


@error_logger
async def edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_data = context.user_data
        field = user_data.get("edit_field")
        value = update.message.text.strip()
        event_id = user_data.get("edit_event_id")
        user_id = update.effective_user.id

        if not all([field, event_id]):
            logger.error(f"User {user_id}: Пропущены ключевые данные в user_data!")
            await update.message.reply_text("❌ Сессия устарела. Начните заново.")
            context.user_data.clear()
            return ConversationHandler.END

        event = db.get_event_by_id(event_id)
        if not event:
            await update.message.reply_text("❌ Мероприятие не найдено!")
            return ConversationHandler.END

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
            event = db.get_event_by_id(event_id)
            end_date = datetime.strptime(event["end_date"], "%Y-%m-%d").date()
            event_time = datetime.strptime(event["event_time"], "%H:%M").time()
            event_datetime = datetime.combine(end_date, event_time)
            reminder_time = event_datetime - timedelta(hours=hours_to_remind)

            job_name = f"reminder_{event_id}"
            for job in context.job_queue.jobs():
                if job.name == job_name:
                    job.schedule_removal()

            if reminder_time > datetime.now():
                delta = (reminder_time - datetime.now()).total_seconds()
                context.job_queue.run_once(
                    send_reminder,
                    when=delta,
                    data=event_id,
                    name=job_name
                )
                logger.info(f"🔄 Напоминание для {event_id} перепланировано")

        return ConversationHandler.END

    except Exception as e:
        logger.error(f"Ошибка в edit_value (User {update.effective_user.id}): {str(e)}", exc_info=True)
        await update.message.reply_text("❌ Критическая ошибка. Состояние сброшено.")
        context.user_data.clear()
        return ConversationHandler.END

    finally:
        # Всегда вызываем admin_events для возврата в меню
        await admin_events(update, context)
        context.user_data.clear()


@error_logger
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_admin_user = is_admin(user.id)
    
    reply_markup = build_main_menu_keyboard(is_admin_user)

    try:
        with open("misc/hello2.txt", "r", encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        text = (
        "Привет! Я бот для записи на мероприятия.\n"
        "Выберите действие:"
    )

    message = update.message or update.callback_query.message
    await message.reply_text(text, reply_markup=reply_markup)


@error_logger
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        message = update.callback_query.message
        user = update.callback_query.from_user
    else:
        message = update.message
        user = update.effective_user

    menu_text = [
        "📋 Доступные команды:",
        "/start - Главное меню",
        "/events - Показать все сессии",
        "/myevents - Показать мои записи",
        "/menu - Зайти в меню",
        "/help - Нужна помощь!"
    ]

    if is_admin(user.id):
        menu_text.extend([
            "\n⚙️ Админ-команды:",
            "/adminevents - Управление сессиями",
            "/createevent - Создать новую сессию"
        ])

    menu_text.append("\nℹ️ Выбери действие из меню или используй команды!")

    reply_markup = build_main_menu_keyboard(is_admin(user.id))
    await message.reply_text("\n".join(menu_text), reply_markup=reply_markup)


@error_logger
async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        logging.info(f"User {query.from_user.id} pressed button: {query.data}")

        command = query.data
        user_id = query.from_user.id

        if command == "menu":
            await menu_command(update, context)
        elif command == "events":
            await show_events(update, context)
        elif command == "myevents":
            await my_events(update, context)
        elif command == "help":
            await help_command(update, context)
            return
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


@error_logger
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # Сброс persistence при критических ошибках
        await context.application.persistence.drop_user_data()
        await context.application.persistence.drop_chat_data()
        if update.message:
            await update.message.reply_text("❌ Произошла внутренняя ошибка")
        elif update.callback_query:
            await update.callback_query.message.reply_text("❌ Произошла внутренняя ошибка")
    except Exception as e:
        logger.error(f"Error in error handler: {str(e)}")


@error_logger
async def cancel_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("✖️ Отправка ссылки отменена")
    return ConversationHandler.END


async def restore_reminders(context: ContextTypes.DEFAULT_TYPE):
    try:
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
            reminder_time = event_datetime - timedelta(hours=hours_to_remind)

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
        await context.application.persistence.drop_user_data()
        await context.application.persistence.drop_chat_data()
        logger.info("Полный сброс persistence после ошибки восстановления")


def main():
    global db
    db = database.Database(DATABASE_NAME)

    application = (
        Application.builder()
        .token(TOKEN)
        .persistence(persistence)
        .build()
    )

    # application.persistence.drop_user_data()
    # application.persistence.drop_chat_data()
    # logger.info("Состояния пользователей сброшены при запуске.")

    application.job_queue.run_once(
        callback=restore_reminders,
        when=5,
        name="init_restore"
    )

    application.add_error_handler(error_handler)

    # Регистрация обработчиков
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("events", show_events))
    application.add_handler(CommandHandler("myevents", my_events))
    application.add_handler(CommandHandler("help", help_command))

    application.add_handler(CommandHandler("reset_persistence", reset_persistence))

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
            CREATE_INFO: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_info)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        persistent=True,
        name="create_event_conv"
    )
    application.add_handler(create_event_conv)

    edit_event_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(edit_event_start, pattern=r"^edit_\d+$")
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
            CallbackQueryHandler(admin_actions, pattern=r"^delete_\d+$")
        ],
        states={
            DELETE_CONFIRM: [
                CallbackQueryHandler(confirm_delete, pattern="^confirm_delete$"),
                CallbackQueryHandler(cancel_edit, pattern="^cancel_delete$")
            ]
        },
        fallbacks=[],
        map_to_parent={ConversationHandler.END: ConversationHandler.END},
        name="delete_event_conv",
        persistent=True
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

    remove_user_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(remove_user_start, pattern=r"^removeuser_\d+$")
        ],
        states={
            REMOVE_USER_SELECT: [
                CallbackQueryHandler(remove_user_finish, pattern=r"^remove_")
            ],
        },
        fallbacks = [CommandHandler("cancel", cancel)]
    )
    application.add_handler(remove_user_conv)

    # Обработчики callback-запросов
    application.add_handler(CallbackQueryHandler(handle_unregistration, pattern=r"^(confirm_unreg_\d+|cancel_unreg)$"))
    application.add_handler(CallbackQueryHandler(event_button, pattern="^event_"))
    application.add_handler(CallbackQueryHandler(edit_event_start, pattern="^edit_"))
    application.add_handler(CallbackQueryHandler(show_event_details, pattern="^detail_"))
    application.add_handler(CallbackQueryHandler(cancel_registration, pattern="^cancel_"))

    application.add_handler(
        CallbackQueryHandler(admin_actions, pattern=r"^delete_\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(admin_actions, pattern=r"^view_\d+$")
    )
    application.add_handler(CallbackQueryHandler(send_message_to_participants, pattern=r"^sendmsg_\d+$"))
    application.add_handler(CallbackQueryHandler(menu_handler))
    application.add_handler(CallbackQueryHandler(handle_back_button, pattern="^adminevents$"))

    # Запуск бота
    application.run_polling()

if __name__ == "__main__":
    main()