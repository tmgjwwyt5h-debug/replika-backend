FROM python:3.11-slim

WORKDIR /app

# Зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код
COPY . .

# Создаём папку для БД
RUN mkdir -p data

# Python должен видеть пакет app из /app
ENV PYTHONPATH=/app
ENV PORT=8000

# Проверяем что всё импортируется без ошибок (упадёт при сборке если что-то не так)
RUN python -c "from app.db import init_db; print('db OK')"
RUN python -c "from app.llm import generate_reply; print('llm OK')"
RUN python -c "from app.telegram_runner import start_bot; print('telegram OK')"
RUN python -c "from app.main import app; print('main OK')"

EXPOSE 8000

# Запускаем через uvicorn напрямую — надёжнее чем python -m
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
