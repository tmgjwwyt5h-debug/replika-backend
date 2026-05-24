"""SQLModel модели и работа с БД."""
import os, json
from datetime import datetime, timedelta
from typing import Optional
from collections import defaultdict
from sqlmodel import SQLModel, Field, create_engine, Session, select

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///data/replika.db")


class Bot(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    description: str = ""
    industry: str = "general"
    system_prompt: str
    greeting: str = "Здравствуйте! Чем могу помочь?"
    llm_provider: str = "gigachat"
    llm_model: str = "GigaChat"
    temperature: float = 0.3
    max_tokens: int = 500
    telegram_token: Optional[str] = None
    telegram_username: Optional[str] = None
    telegram_enabled: bool = False
    status: str = "draft"
    last_error: Optional[str] = None
    total_messages: int = 0
    total_users: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class KnowledgeChunk(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    bot_id: int = Field(default=0, index=True)
    title: str
    content: str
    source_type: str = "manual"
    enabled: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Conversation(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    bot_id: int = Field(foreign_key="bot.id", index=True)
    channel: str = "telegram"
    external_user_id: str
    user_name: Optional[str] = None
    user_username: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_message_at: datetime = Field(default_factory=datetime.utcnow)


class Message(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    conversation_id: int = Field(foreign_key="conversation.id", index=True)
    bot_id: int = Field(foreign_key="bot.id", index=True)
    role: str
    content: str
    tokens_used: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Integration(SQLModel, table=True):
    """Интеграция бота с внешним сервисом."""
    id: Optional[int] = Field(default=None, primary_key=True)
    bot_id: int = Field(foreign_key="bot.id", index=True)
    type: str            # webhook | email | sheets | bitrix24 | amocrm
    name: str
    config: str = "{}"   # JSON
    enabled: bool = True
    last_triggered: Optional[datetime] = None
    last_error: Optional[str] = None
    trigger_count: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)

    def get_config(self) -> dict:
        try:    return json.loads(self.config)
        except: return {}


# ── Engine & Session ────────────────────────────────────────
engine = create_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
)

def init_db():
    os.makedirs("data", exist_ok=True)
    SQLModel.metadata.create_all(engine)

def get_session():
    return Session(engine)


# ── Bot helpers ─────────────────────────────────────────────
def get_bot(bot_id: int) -> Optional[Bot]:
    with get_session() as s:
        return s.get(Bot, bot_id)

def get_all_bots() -> list:
    with get_session() as s:
        return list(s.exec(select(Bot).order_by(Bot.created_at.desc())))

def get_bot_knowledge(bot_id: int) -> list:
    with get_session() as s:
        return list(s.exec(select(KnowledgeChunk).where(
            KnowledgeChunk.bot_id == bot_id, KnowledgeChunk.enabled == True
        )))

def get_all_knowledge() -> list:
    with get_session() as s:
        return list(s.exec(select(KnowledgeChunk).order_by(KnowledgeChunk.created_at.desc())))

def get_or_create_conversation(bot_id, channel, external_user_id, user_name=None, user_username=None):
    with get_session() as s:
        conv = s.exec(select(Conversation).where(
            Conversation.bot_id == bot_id,
            Conversation.channel == channel,
            Conversation.external_user_id == external_user_id,
        )).first()
        if conv:
            conv.last_message_at = datetime.utcnow()
            s.add(conv); s.commit(); s.refresh(conv)
            return conv
        conv = Conversation(bot_id=bot_id, channel=channel,
            external_user_id=external_user_id,
            user_name=user_name, user_username=user_username)
        s.add(conv); s.commit(); s.refresh(conv)
        return conv

def get_recent_messages(conversation_id: int, limit: int = 20) -> list:
    with get_session() as s:
        msgs = list(s.exec(
            select(Message).where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc()).limit(limit)
        ))
        return list(reversed(msgs))

def save_message(conversation_id, bot_id, role, content, tokens=0):
    with get_session() as s:
        msg = Message(conversation_id=conversation_id, bot_id=bot_id,
                      role=role, content=content, tokens_used=tokens)
        s.add(msg)
        bot = s.get(Bot, bot_id)
        if bot:
            bot.total_messages += 1
            s.add(bot)
        s.commit()
        s.refresh(msg)
        return msg


# ── Analytics ────────────────────────────────────────────────
def get_platform_analytics(days: int = 30) -> dict:
    """Аналитика платформы. Возвращает всё готовое для шаблона."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    with get_session() as s:
        bots     = list(s.exec(select(Bot)))
        convs    = list(s.exec(select(Conversation)))
        all_msgs = list(s.exec(select(Message)))

    user_msgs = [m for m in all_msgs if m.role == "user"]
    new_msgs  = [m for m in user_msgs if m.created_at >= cutoff]

    # Сообщения по дням
    daily: dict = defaultdict(int)
    for m in new_msgs:
        daily[m.created_at.strftime("%d.%m")] += 1
    daily_sorted = dict(sorted(daily.items()))

    # SVG-путь для графика (считаем в Python, не в Jinja2)
    svg_path = ""
    svg_area = ""
    svg_dots = []
    day_labels = []
    if daily_sorted:
        vals = list(daily_sorted.values())
        keys = list(daily_sorted.keys())
        n    = len(vals)
        mx   = max(vals) or 1
        pts  = []
        for i, v in enumerate(vals):
            x = (i / max(n - 1, 1)) * 560 + 20
            y = 160 - (v / mx) * 140
            pts.append((x, y))
            if i % max(n // 5, 1) == 0:
                day_labels.append((x, keys[i]))
        if pts:
            path_d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
            svg_path = path_d
            svg_area = path_d + f" L {pts[-1][0]:.1f},160 L {pts[0][0]:.1f},160 Z"
            svg_dots = [(f"{x:.1f}", f"{y:.1f}") for x, y in pts]

    # Топ ботов
    bot_msgs: dict = defaultdict(int)
    for m in user_msgs:
        bot_msgs[m.bot_id] += 1
    top = sorted(bot_msgs.items(), key=lambda x: x[1], reverse=True)[:5]
    max_top = max((c for _, c in top), default=1) or 1
    top_bots = [(bid, cnt, round(cnt / max_top * 100)) for bid, cnt in top]

    return {
        "total_bots":   len(bots),
        "active_bots":  sum(1 for b in bots if b.status == "active"),
        "total_convs":  len(convs),
        "total_msgs":   len(user_msgs),
        "total_tokens": sum(m.tokens_used for m in all_msgs),
        "unique_users": len(set(c.external_user_id for c in convs)),
        "days":         days,
        "svg_path":     svg_path,
        "svg_area":     svg_area,
        "svg_dots":     svg_dots,
        "day_labels":   day_labels,
        "top_bots":     top_bots,
    }
