# Используем официальный образ Python 3.11
FROM python:3.11-slim

# Устанавливаем ffmpeg на этапе сборки (тут есть root-права)
RUN apt-get update && apt-get install -y -qq ffmpeg && rm -rf /var/lib/apt/lists/*

# Создаём рабочую директорию
WORKDIR /app

# Копируем зависимости и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь код приложения
COPY . .

# Создаём пользователя без прав root для безопасности
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

# Открываем порт (Render сам передаст $PORT)
EXPOSE 10000

# Команда запуска (gunicorn уже знает, как работать с $PORT)
CMD ["gunicorn", "--bind", "0.0.0.0:10000", "--workers", "2", "app:app"]
