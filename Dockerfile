FROM python:3.12-slim

# Установка часового пояса
ENV TZ=Asia/Tashkent
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

# Установка зависимостей
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование исходного кода
COPY . .

# config.json и bot_data.json хранятся в /data (подключите том, чтобы не терять данные)
ENV DATA_DIR=/data
VOLUME /data

# Запуск бота (токен передаётся через переменную окружения BOT_TOKEN)
CMD ["python", "bot.py"]
