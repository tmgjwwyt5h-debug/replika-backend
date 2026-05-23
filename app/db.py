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
