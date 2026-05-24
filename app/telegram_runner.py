"""Менеджер Telegram-ботов: текстовые + голосовые сообщения, интеграции."""
import asyncio, logging, io
from typing import Dict
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters
from telegram.constants import ChatAction

from app import db, llm, integrations, integrations

logger = logging.getLogger(__name__)

_running: Dict[int, Application] = {}


def _make_handlers(bot_id: int):

    async def on_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        bot = db.get_bot(bot_id)
        if not bot: return
        await update.message.reply_text(bot.greeting)
        u = update.effective_user
        conv = db.get_or_create_conversation(
            bot_id=bot_id, channel="telegram",
            external_user_id=str(u.id), user_name=u.full_name, user_username=u.username
        )
        db.save_message(conv.id, bot_id, "assistant", bot.greeting)

    async def _process_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user_text: str, is_voice: bool = False):
        bot = db.get_bot(bot_id)
        if not bot: return
        u = update.effective_user
        conv = db.get_or_create_conversation(
            bot_id=bot_id, channel="telegram",
            external_user_id=str(u.id), user_name=u.full_name, user_username=u.username
        )
        db.save_message(conv.id, bot_id, "user", user_text)

        action = ChatAction.RECORD_VOICE if is_voice else ChatAction.TYPING
        await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action=action)

        try:
            history = [m for m in db.get_recent_messages(conv.id, limit=20) if m.content != user_text]
            knowledge = db.get_bot_knowledge(bot_id)
            reply, tokens = await asyncio.to_thread(llm.generate_reply, bot, knowledge, history, user_text)
            db.save_message(conv.id, bot_id, "assistant", reply, tokens=tokens)

            # Если голосовое входящее — отвечаем голосом
            if is_voice and os.getenv("GROQ_API_KEY"):
                try:
                    from app.voice import tts_edge
                    voice_name = getattr(bot, 'voice_name', 'svetlana') or 'svetlana'
                    audio_data = await tts_edge(reply, voice_name)
                    await update.message.reply_voice(
                        voice=io.BytesIO(audio_data),
                        caption=reply[:200] + ("…" if len(reply) > 200 else "")
                    )
                except Exception as ve:
                    logger.warning(f"TTS failed, falling back to text: {ve}")
                    await update.message.reply_text(reply)
            else:
                await update.message.reply_text(reply)

            # Интеграции
            asyncio.create_task(integrations.fire_all(
                bot_id=bot_id, user_message=user_text, bot_reply=reply,
                user_name=u.full_name, user_id=str(u.id),
                channel="telegram", bot_name=bot.name
            ))

        except Exception as e:
            logger.exception(f"Ошибка обработки сообщения для бота {bot_id}")
            await update.message.reply_text("Извините, не получилось ответить. Попробуйте через минуту.")
            with db.get_session() as s:
                b = s.get(db.Bot, bot_id)
                if b: b.last_error = str(e)[:500]; b.status = "error"; s.add(b); s.commit()

    async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.message and update.message.text:
            await _process_text(update, ctx, update.message.text, is_voice=False)

    async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Голосовые сообщения: скачиваем OGG → STT → LLM → TTS (если настроен)."""
        if not update.message or not update.message.voice:
            return

        groq_key = os.getenv("GROQ_API_KEY", "")
        if not groq_key:
            await update.message.reply_text(
                "Голосовые сообщения не настроены.\n"
                "Администратору нужно добавить GROQ_API_KEY в настройки."
            )
            return

        await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        try:
            voice_file = await ctx.bot.get_file(update.message.voice.file_id)
            audio_bytes = await voice_file.download_as_bytearray()

            from app.voice import stt_groq
            text = await stt_groq(bytes(audio_bytes), "voice.ogg")
            if not text:
                await update.message.reply_text("Не удалось распознать речь, попробуйте ещё раз.")
                return

            # Показываем что распознали
            await update.message.reply_text(f"🎤 _{text}_", parse_mode="Markdown")
            await _process_text(update, ctx, text, is_voice=True)

        except Exception as e:
            logger.exception("Voice processing error")
            await update.message.reply_text(f"Ошибка обработки голоса: {e}")

    return on_start, on_text, on_voice


async def start_bot(bot_id: int) -> bool:
    bot = db.get_bot(bot_id)
    if not bot or not bot.telegram_token: return False
    if bot_id in _running: await stop_bot(bot_id)

    try:
        app = Application.builder().token(bot.telegram_token).build()
        on_start, on_text, on_voice = _make_handlers(bot_id)
        app.add_handler(CommandHandler("start", on_start))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
        app.add_handler(MessageHandler(filters.VOICE, on_voice))

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
            if b: b.status = "error"; b.last_error = str(e)[:500]; s.add(b); s.commit()
        return False


async def stop_bot(bot_id: int):
    app = _running.pop(bot_id, None)
    if app:
        try: await app.updater.stop(); await app.stop(); await app.shutdown()
        except Exception as e: logger.warning(f"При остановке бота {bot_id}: {e}")
    with db.get_session() as s:
        b = s.get(db.Bot, bot_id)
        if b and b.status == "active": b.status = "paused"; s.add(b); s.commit()


def is_running(bot_id: int) -> bool: return bot_id in _running


async def start_all_active():
    for bot in db.get_all_bots():
        if bot.telegram_token and bot.telegram_enabled:
            await start_bot(bot.id)


async def stop_all():
    for bid in list(_running.keys()):
        await stop_bot(bid)


import os
