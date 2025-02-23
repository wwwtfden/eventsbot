# Используем официальный образ Python
FROM python:3.12-slim

# Устанавливаем зависимости для SQLite (если нужно)
RUN apt-get update && apt-get install -y sqlite3

# Создаем рабочую директорию
WORKDIR /app

# Копируем зависимости
COPY requirements.txt .

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходный код
COPY . .

# Запускаем бота
CMD ["python", "event_bot_main.py"]