FROM python:3.11-slim

WORKDIR /app

# Только нужные системные пакеты
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p data

ENV PYTHONPATH=/app
ENV PORT=8000
ENV PYTHONUNBUFFERED=1

# Проверяем импорты при сборке — если упадут увидим в Build Logs
RUN python -c "from app.db import init_db; print('✅ db')"
RUN python -c "from app.llm import generate_reply; print('✅ llm')"
RUN python -c "from app.telegram_runner import start_bot; print('✅ telegram')"
RUN python -c "from app.main import app; print('✅ main — всё OK')"

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
