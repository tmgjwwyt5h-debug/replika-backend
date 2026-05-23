# Реплика · Backend MVP

Платформа для создания Telegram-ботов на GigaChat. Веб-админка + бэкенд + Telegram polling в одном процессе.

## Что умеет

- Создавать ботов через веб-интерфейс (название, system prompt, температура, база знаний)
- Хранить настройки и историю диалогов в SQLite
- Принимать Telegram-токен и автоматически запускать бота через polling
- Использовать GigaChat (или OpenAI/Claude если есть ключ) для генерации ответов
- Логировать все диалоги — видно в админке

## Требования

- Python 3.10+
- GigaChat API ключ (бесплатно, [developers.sber.ru](https://developers.sber.ru/portal/products/gigachat-api))
- Telegram-бот через [@BotFather](https://t.me/BotFather) (для каждого бота, которого создадите)

## Запуск

```bash
# 1. Установить зависимости
pip install -r requirements.txt

# 2. Скопировать .env.example в .env и заполнить
cp .env.example .env
# затем отредактировать .env и добавить GIGACHAT_CREDENTIALS

# 3. Запустить
python -m app.main
```

Откроется на http://localhost:8000

## Структура

```
replika/
├── app/
│   ├── main.py            # FastAPI приложение + entry point
│   ├── db.py              # SQLite модели (Bot, Message, KnowledgeChunk)
│   ├── llm.py             # GigaChat клиент + ответы
│   ├── telegram_runner.py # Менеджер polling для всех ботов
│   ├── templates/         # Jinja2 шаблоны (в стиле лендинга)
│   └── static/            # CSS
├── data/
│   └── replika.db         # SQLite база
└── .env                   # Ключи и настройки
```

## Как создать своего первого бота

1. Открыть http://localhost:8000
2. Нажать «Создать бота»
3. Заполнить:
   - Имя: `Роза`
   - Telegram токен: получить у @BotFather, вставить
   - System prompt: «Ты Роза, помощник стоматологии. Записываешь пациентов...»
   - Температура: 0.3
4. Сохранить. Бот сразу начинает отвечать в Telegram.
5. Зайти в Telegram, найти своего бота, написать `/start`.

## Где взять ключ GigaChat

1. Зайти на https://developers.sber.ru/portal/products/gigachat-api
2. Войти через Сбер ID
3. Создать проект → получить `Authorization data` (это base64 строка типа `MTIz...==`)
4. Вставить в `.env` как `GIGACHAT_CREDENTIALS=MTIz...==`

GigaChat бесплатный: 1 млн токенов в месяц.

## Альтернативные LLM

В `.env` можно поставить вместо GigaChat:
- `OPENAI_API_KEY=sk-...` — для GPT-4
- `ANTHROPIC_API_KEY=sk-ant-...` — для Claude

Если есть оба — выберите в админке для каждого бота.
