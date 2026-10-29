# Бот работает на стандартной библиотеке Python — внешних зависимостей нет.
FROM python:3.11-slim

WORKDIR /app

# Копируем код.
COPY . .

# Ничего ставить не нужно, но на всякий случай убедимся, что pip в порядке.
RUN python -m pip install --no-cache-dir --upgrade pip

# Health-сервер слушает PORT (Render сам выставляет эту переменную).
EXPOSE 10000

# Запускаем бота (он сам поднимает health-сервер в фоне).
CMD ["python", "main.py"]