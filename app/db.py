"""SQLModel модели и работа с БД."""
from datetime import datetime
from typing import Optional
from sqlmodel import SQLModel, Field, create_engine, Session, select
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///data/replika.db")


class Bot(SQLModel, table=True):
    """Бот, которого создаёт пользователь платформы."""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    description: str = ""
    industry: str = "general"        # medical, legal, auto, beauty, horeca, real-estate, general
    system_prompt: str
    greeting: str = "Здравствуйте! Чем могу помочь?"

    # LLM настройки
    llm_provider: str = "gigachat"   # gigachat, openai, anthropic
    llm_model: str = "GigaChat"
    temperature: float = 0.3
    max_tokens: int = 500

    # Telegram интеграция
    telegram_token: Optional[str] = None
    telegram_username: Optional[str] = None
    telegram_enabled: bool = False

    # Состояние
    status: str = "draft"            # draft, active, paused, error
    last_error: Optional[str] = None

    # Статистика (обновляется по факту работы)
    total_messages: int = 0
    total_users: int = 0

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class KnowledgeChunk(SQLModel, table=True):
    """Кусок базы знаний — просто текст, который добавляется в системный промт."""
    id: Optional[int] = Field(default=None, primary_key=True)
    bot_id: int = Field(foreign_key="bot.id", index=True)
    title: str
    content: str
    source_type: str = "manual"      # manual, file, url
    enabled: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Conversation(SQLModel, table=True):
    """Диалог с конкретным пользователем в конкретном канале."""
    id: Optional[int] = Field(default=None, primary_key=True)
    bot_id: int = Field(foreign_key="bot.id", index=True)
    channel: str = "telegram"        # telegram, web, etc
    external_user_id: str            # telegram user_id или web session_id
    user_name: Optional[str] = None
    user_username: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_message_at: datetime = Field(default_factory=datetime.utcnow)


class Message(SQLModel, table=True):
    """Одно сообщение в диалоге."""
    id: Optional[int] = Field(default=None, primary_key=True)
    conversation_id: int = Field(foreign_key="conversation.id", index=True)
    bot_id: int = Field(foreign_key="bot.id", index=True)
    role: str                        # user, assistant, system
    content: str
    tokens_used: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)


# Engine и сессии
engine = create_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
)


def init_db():
    """Создать таблицы если их нет."""
    os.makedirs("data", exist_ok=True)
    SQLModel.metadata.create_all(engine)


def get_session():
    """Контекстный менеджер сессии."""
    return Session(engine)


# Хелперы

def get_bot(bot_id: int) -> Optional[Bot]:
    with get_session() as s:
        return s.get(Bot, bot_id)


def get_all_bots() -> list[Bot]:
    with get_session() as s:
        return list(s.exec(select(Bot).order_by(Bot.created_at.desc())))


def get_bot_knowledge(bot_id: int) -> list[KnowledgeChunk]:
    with get_session() as s:
        stmt = select(KnowledgeChunk).where(
            KnowledgeChunk.bot_id == bot_id,
            KnowledgeChunk.enabled == True
        )
        return list(s.exec(stmt))


def get_or_create_conversation(
    bot_id: int, channel: str, external_user_id: str,
    user_name: Optional[str] = None, user_username: Optional[str] = None
) -> Conversation:
    with get_session() as s:
        stmt = select(Conversation).where(
            Conversation.bot_id == bot_id,
            Conversation.channel == channel,
            Conversation.external_user_id == external_user_id,
        )
        conv = s.exec(stmt).first()
        if conv:
            conv.last_message_at = datetime.utcnow()
            s.add(conv); s.commit(); s.refresh(conv)
            return conv

        conv = Conversation(
            bot_id=bot_id, channel=channel,
            external_user_id=external_user_id,
            user_name=user_name, user_username=user_username,
        )
        s.add(conv); s.commit(); s.refresh(conv)
        return conv


def get_recent_messages(conversation_id: int, limit: int = 20) -> list[Message]:
    """Последние N сообщений диалога в хронологическом порядке."""
    with get_session() as s:
        stmt = (select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at.desc())
                .limit(limit))
        msgs = list(s.exec(stmt))
        return list(reversed(msgs))


def save_message(conversation_id: int, bot_id: int, role: str, content: str, tokens: int = 0) -> Message:
    with get_session() as s:
        msg = Message(
            conversation_id=conversation_id, bot_id=bot_id,
            role=role, content=content, tokens_used=tokens,
        )
        s.add(msg)
        bot = s.get(Bot, bot_id)
        if bot:
            bot.total_messages += 1
            s.add(bot)
        s.commit()
        s.refresh(msg)
        return msg


# ============ ANALYTICS QUERIES ============

def analytics_global() -> dict:
    """Глобальная статистика по всей платформе."""
    from sqlmodel import func
    with get_session() as s:
        total_msgs   = s.exec(select(func.count(Message.id))).one() or 0
        total_convs  = s.exec(select(func.count(Conversation.id))).one() or 0
        total_bots   = s.exec(select(func.count(Bot.id))).one() or 0
        active_bots  = s.exec(select(func.count(Bot.id)).where(Bot.status == "active")).one() or 0
        total_tokens = s.exec(select(func.sum(Message.tokens_used))).one() or 0
    return {
        "total_messages": total_msgs,
        "total_conversations": total_convs,
        "total_bots": total_bots,
        "active_bots": active_bots,
        "total_tokens": total_tokens or 0,
    }


def analytics_messages_per_day(days: int = 30) -> list[dict]:
    """Сообщения за последние N дней."""
    from datetime import timedelta
    from sqlmodel import func
    cutoff = datetime.utcnow() - timedelta(days=days)
    with get_session() as s:
        rows = s.exec(
            select(
                func.date(Message.created_at).label("day"),
                func.count(Message.id).label("count")
            ).where(
                Message.created_at >= cutoff,
                Message.role == "user"
            ).group_by(func.date(Message.created_at))
            .order_by(func.date(Message.created_at))
        ).all()
    return [{"day": str(r.day), "count": r.count} for r in rows]


def analytics_per_bot() -> list[dict]:
    """Статистика по каждому боту."""
    from sqlmodel import func
    with get_session() as s:
        bots = list(s.exec(select(Bot).order_by(Bot.total_messages.desc())))
        result = []
        for bot in bots:
            convs = s.exec(
                select(func.count(Conversation.id)).where(Conversation.bot_id == bot.id)
            ).one() or 0
            tokens = s.exec(
                select(func.sum(Message.tokens_used)).where(Message.bot_id == bot.id)
            ).one() or 0
            result.append({
                "id": bot.id,
                "name": bot.name,
                "status": bot.status,
                "messages": bot.total_messages,
                "conversations": convs,
                "tokens": tokens or 0,
                "provider": bot.llm_provider,
            })
    return result


def get_all_knowledge() -> list:
    """Все чанки базы знаний со всех ботов."""
    with get_session() as s:
        chunks = list(s.exec(
            select(KnowledgeChunk).order_by(KnowledgeChunk.created_at.desc())
        ))
        bots = {b.id: b.name for b in s.exec(select(Bot))}
        result = []
        for c in chunks:
            result.append({
                "id": c.id,
                "bot_id": c.bot_id,
                "bot_name": bots.get(c.bot_id, "Неизвестно"),
                "title": c.title,
                "content": c.content,
                "enabled": c.enabled,
                "created_at": c.created_at,
            })
    return result


# ============ ANALYTICS ============
from datetime import timedelta
from collections import defaultdict

def get_platform_analytics(days: int = 30) -> dict:
    """Сводная аналитика платформы за последние N дней."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    with get_session() as s:
        bots     = list(s.exec(select(Bot)))
        convs    = list(s.exec(select(Conversation)))
        msgs_all = list(s.exec(select(Message)))
        msgs_new = [m for m in msgs_all if m.created_at >= cutoff]

        # Сообщения по дням (пользователь)
        daily: dict = defaultdict(int)
        for m in msgs_new:
            if m.role == "user":
                daily[m.created_at.strftime("%d.%m")] += 1

        # Топ ботов по числу сообщений
        bot_msgs: dict = defaultdict(int)
        for m in msgs_all:
            if m.role == "user":
                bot_msgs[m.bot_id] += 1
        top_bots = sorted(bot_msgs.items(), key=lambda x: x[1], reverse=True)[:5]

        # Уникальных пользователей
        unique_users = len(set(c.external_user_id for c in convs))

        return {
            "total_bots":    len(bots),
            "active_bots":   sum(1 for b in bots if b.status == "active"),
            "total_convs":   len(convs),
            "total_msgs":    sum(1 for m in msgs_all if m.role == "user"),
            "total_tokens":  sum(m.tokens_used for m in msgs_all),
            "unique_users":  unique_users,
            "daily_msgs":    dict(sorted(daily.items())),
            "top_bots":      [(bot_id, count) for bot_id, count in top_bots],
            "days":          days,
        }


def get_all_knowledge() -> list:
    """Все чанки базы знаний со всех ботов."""
    with get_session() as s:
        chunks = list(s.exec(select(KnowledgeChunk).order_by(KnowledgeChunk.created_at.desc())))
        return chunks


# ============ INTEGRATIONS ============
class Integration(SQLModel, table=True):
    """Интеграция бота с внешним сервисом."""
    id: Optional[int] = Field(default=None, primary_key=True)
    bot_id: int = Field(foreign_key="bot.id", index=True)
    service: str          # webhook, bitrix24, sheets, email, vk
    name: str             # название интеграции
    enabled: bool = False
    config: str = "{}"    # JSON с настройками (URL, ключи)
    last_triggered_at: Optional[datetime] = None
    trigger_count: int = 0
    last_error: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

def get_bot_integrations(bot_id: int) -> list:
    with get_session() as s:
        return list(s.exec(select(Integration).where(Integration.bot_id == bot_id)))

def get_integration(integ_id: int) -> Optional[Integration]:
    with get_session() as s:
        return s.get(Integration, integ_id)


# ============ INTEGRATIONS MODEL ============
import json as _json

class Integration(SQLModel, table=True):
    """Интеграция бота с внешним сервисом."""
    id: Optional[int] = Field(default=None, primary_key=True)
    bot_id: int = Field(foreign_key="bot.id", index=True)
    type: str          # webhook | sheets | email | bitrix24 | airtable
    name: str
    enabled: bool = True
    config: str = "{}" # JSON строка с настройками
    last_triggered: Optional[datetime] = None
    trigger_count: int = 0
    last_error: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


def get_bot_integrations(bot_id: int) -> list:
    with get_session() as s:
        return list(s.exec(select(Integration).where(Integration.bot_id == bot_id)))


def get_integration(integ_id: int) -> Optional[Integration]:
    with get_session() as s:
        return s.get(Integration, integ_id)
