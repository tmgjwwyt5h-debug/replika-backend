"""Менеджер Telegram-ботов: запускает polling в фоне для каждого активного бота."""
import asyncio
import logging
from typing import Dict
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters

from app import db, llm

logger = logging.getLogger(__name__)

# bot_id -> Application
_running: Dict[int, Application] = {}
_tasks: Dict[int, asyncio.Task] = {}


def _make_handlers(bot_id: int):
    """Создаёт обработчики для конкретного бота (замыкание по bot_id)."""

    async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        bot = db.get_bot(bot_id)
        if not bot:
            return
        await update.message.reply_text(bot.greeting)

        # сохраняем диалог
        u = update.effective_user
        conv = db.get_or_create_conversation(
            bot_id=bot_id, channel="telegram",
            external_user_id=str(u.id),
            user_name=u.full_name, user_username=u.username,
        )
        db.save_message(conv.id, bot_id, "assistant", bot.greeting)

    async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.message or not update.message.text:
            return

        bot = db.get_bot(bot_id)
        if not bot:
            await update.message.reply_text("Бот недоступен.")
            return

        user_text = update.message.text
        u = update.effective_user

        conv = db.get_or_create_conversation(
            bot_id=bot_id, channel="telegram",
            external_user_id=str(u.id),
            user_name=u.full_name, user_username=u.username,
        )

        # Сохраняем сообщение пользователя
        db.save_message(conv.id, bot_id, "user", user_text)

        # Показываем «печатает»
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

        try:
            history = db.get_recent_messages(conv.id, limit=20)
            # Последнее сообщение — это то что мы только что сохранили,
            # поэтому исключаем его и передаём как new_user_text отдельно
            history_without_last = [m for m in history if not (m.role == "user" and m.content == user_text)][:-1]
            knowledge = db.get_bot_knowledge(bot_id)

            reply, tokens = await asyncio.to_thread(
                llm.generate_reply, bot, knowledge, history_without_last, user_text
            )
            db.save_message(conv.id, bot_id, "assistant", reply, tokens=tokens)
            await update.message.reply_text(reply)

        except Exception as e:
            logger.exception("LLM error")
            error_msg = "Извините, не получилось ответить. Попробуйте ещё раз через минуту."
            await update.message.reply_text(error_msg)

            # Сохраняем ошибку в бота
            with db.get_session() as s:
                b = s.get(db.Bot, bot_id)
                if b:
                    b.last_error = str(e)[:500]
                    b.status = "error"
                    s.add(b); s.commit()

    return on_start, on_message


async def start_bot(bot_id: int) -> bool:
    """Запускает polling для бота. True если успешно."""
    bot = db.get_bot(bot_id)
    if not bot or not bot.telegram_token:
        return False

    if bot_id in _running:
        await stop_bot(bot_id)

    try:
        app = Application.builder().token(bot.telegram_token).build()

        on_start, on_message = _make_handlers(bot_id)
        app.add_handler(CommandHandler("start", on_start))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

        # Получаем username из Telegram
        await app.initialize()
        me = await app.bot.get_me()

        with db.get_session() as s:
            b = s.get(db.Bot, bot_id)
            if b:
                b.telegram_username = me.username
                b.status = "active"
                b.last_error = None
                s.add(b); s.commit()

        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        _running[bot_id] = app
        logger.info(f"Бот {bot_id} (@{me.username}) запущен")
        return True

    except Exception as e:
        logger.exception(f"Не удалось запустить бота {bot_id}")
        with db.get_session() as s:
            b = s.get(db.Bot, bot_id)
            if b:
                b.status = "error"
                b.last_error = str(e)[:500]
                s.add(b); s.commit()
        return False


async def stop_bot(bot_id: int):
    """Останавливает polling для бота."""
    app = _running.pop(bot_id, None)
    if app:
        try:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
        except Exception as e:
            logger.warning(f"При остановке бота {bot_id}: {e}")

    with db.get_session() as s:
        b = s.get(db.Bot, bot_id)
        if b and b.status == "active":
            b.status = "paused"
            s.add(b); s.commit()


def is_running(bot_id: int) -> bool:
    return bot_id in _running


async def start_all_active():
    """При старте сервера — запустить всех ботов со статусом active или telegram_enabled."""
    for bot in db.get_all_bots():
        if bot.telegram_token and bot.telegram_enabled:
            await start_bot(bot.id)


async def stop_all():
    """Остановить всех ботов перед выключением сервера."""
    bot_ids = list(_running.keys())
    for bid in bot_ids:
        await stop_bot(bid)
