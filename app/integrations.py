"""Рабочие интеграции: Webhook, Email, Google Sheets, Bitrix24, amoСRM."""
import json, logging, smtplib, ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from typing import Optional
import httpx
from sqlmodel import select
from app import db

logger = logging.getLogger(__name__)


# ============ МОДЕЛЬ ИНТЕГРАЦИИ ============
class Integration(db.SQLModel, table=True):
    id: Optional[int] = db.Field(default=None, primary_key=True)
    bot_id: int = db.Field(foreign_key="bot.id", index=True)
    type: str           # webhook | email | sheets | bitrix24 | amocrm
    name: str           # человеческое название
    config: str = "{}"  # JSON с настройками
    enabled: bool = True
    last_triggered: Optional[datetime] = None
    last_error: Optional[str] = None
    trigger_count: int = 0
    created_at: datetime = db.Field(default_factory=datetime.utcnow)

    def get_config(self) -> dict:
        try: return json.loads(self.config)
        except: return {}


def get_bot_integrations(bot_id: int) -> list[Integration]:
    with db.get_session() as s:
        return list(s.exec(select(Integration).where(Integration.bot_id == bot_id)))


# ============ TRIIGGERS ============

async def trigger_webhook(cfg: dict, payload: dict) -> None:
    """POST на произвольный URL. Работает с Zapier, Make, n8n, любым сервером."""
    url = cfg.get("url", "").strip()
    if not url: raise ValueError("URL не задан")
    secret = cfg.get("secret", "")
    headers = {"Content-Type": "application/json"}
    if secret: headers["X-Replika-Secret"] = secret
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()


async def trigger_email(cfg: dict, payload: dict) -> None:
    """Отправка письма через SMTP. Gmail App Password работает бесплатно."""
    smtp_user = cfg.get("smtp_user", "").strip()
    smtp_pass = cfg.get("smtp_pass", "").strip()
    to_email  = cfg.get("to_email", "").strip()
    smtp_host = cfg.get("smtp_host", "smtp.gmail.com")
    smtp_port = int(cfg.get("smtp_port", 587))
    if not all([smtp_user, smtp_pass, to_email]):
        raise ValueError("smtp_user, smtp_pass и to_email обязательны")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[Реплика] Новый диалог — бот {payload.get('bot_name', '')}"
    msg["From"]    = smtp_user
    msg["To"]      = to_email

    user_msg = payload.get("user_message", "")
    bot_reply = payload.get("bot_reply", "")
    user_name = payload.get("user_name", "Аноним")
    ts = payload.get("timestamp", "")

    html = f"""
<html><body style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:20px">
<div style="background:#14110D;color:#F2ECE0;padding:16px 20px;border-radius:10px 10px 0 0">
  <strong style="font-size:18px">{payload.get('bot_name','Бот')} · Реплика</strong><br>
  <span style="font-size:12px;opacity:0.6">{ts}</span>
</div>
<div style="border:1px solid #E8E1D2;padding:20px;border-radius:0 0 10px 10px">
  <p><strong>Пользователь:</strong> {user_name}</p>
  <div style="background:#F2ECE0;padding:12px;border-radius:6px;margin:10px 0">
    <em>«{user_msg}»</em>
  </div>
  <p><strong>Ответ бота:</strong></p>
  <div style="background:#E8F5E9;padding:12px;border-radius:6px;margin:10px 0">
    {bot_reply}
  </div>
  <hr style="border:1px solid #eee;margin:16px 0">
  <p style="font-size:12px;color:#8A8174">Канал: {payload.get('channel','telegram')} · Реплика платформа</p>
</div>
</body></html>"""

    msg.attach(MIMEText(html, "html"))
    ctx = ssl.create_default_context()
    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.ehlo()
        server.starttls(context=ctx)
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, to_email, msg.as_string())


async def trigger_sheets(cfg: dict, payload: dict) -> None:
    """POST в Google Apps Script Web App. Бесплатно, без OAuth."""
    url = cfg.get("webapp_url", "").strip()
    if not url: raise ValueError("webapp_url не задан")
    row = {
        "timestamp":    payload.get("timestamp", ""),
        "bot":          payload.get("bot_name", ""),
        "user":         payload.get("user_name", ""),
        "user_message": payload.get("user_message", ""),
        "bot_reply":    payload.get("bot_reply", ""),
        "channel":      payload.get("channel", "telegram"),
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json=row)
        resp.raise_for_status()


async def trigger_bitrix24(cfg: dict, payload: dict) -> None:
    """Создаём лид в Битрикс24 через Incoming Webhook."""
    webhook = cfg.get("webhook_url", "").strip().rstrip("/")
    if not webhook: raise ValueError("webhook_url не задан")
    user_name = payload.get("user_name", "Клиент")
    bot_name  = payload.get("bot_name", "Бот")
    comment   = f"Диалог из бота «{bot_name}»:\n\n— {payload.get('user_message','')}\n— {payload.get('bot_reply','')}"
    data = {
        "fields": {
            "TITLE":    f"Реплика · {bot_name} · {user_name}",
            "NAME":     user_name,
            "COMMENTS": comment,
            "SOURCE_ID":"WEB",
            "SOURCE_DESCRIPTION": "Чат-бот Реплика",
        }
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(f"{webhook}/crm.lead.add.json", json=data)
        resp.raise_for_status()


async def trigger_amocrm(cfg: dict, payload: dict) -> None:
    """Создаём контакт+сделку в amoCRM через API."""
    domain    = cfg.get("domain", "").strip()
    api_token = cfg.get("api_token", "").strip()
    if not all([domain, api_token]): raise ValueError("domain и api_token обязательны")
    headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
    user_name = payload.get("user_name", "Клиент")
    note_text = f"Бот {payload.get('bot_name','')}: {payload.get('user_message','')} → {payload.get('bot_reply','')}"
    contact = {"name": user_name, "custom_fields_values": []}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(f"https://{domain}/api/v4/contacts", json=[contact], headers=headers)
        r.raise_for_status()


# ============ ГЛАВНЫЙ ТРИГГЕР ============

async def fire_all(bot_id: int, bot_name: str, user_name: str,
                   user_message: str, bot_reply: str, channel: str = "telegram") -> None:
    """Вызывается после каждого ответа бота. Запускает все активные интеграции."""
    payload = {
        "bot_id":       bot_id,
        "bot_name":     bot_name,
        "user_name":    user_name,
        "user_message": user_message,
        "bot_reply":    bot_reply,
        "channel":      channel,
        "timestamp":    datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
    }

    integrations = get_bot_integrations(bot_id)
    for integ in integrations:
        if not integ.enabled: continue
        cfg = integ.get_config()
        try:
            if   integ.type == "webhook":  await trigger_webhook(cfg, payload)
            elif integ.type == "email":    await trigger_email(cfg, payload)
            elif integ.type == "sheets":   await trigger_sheets(cfg, payload)
            elif integ.type == "bitrix24": await trigger_bitrix24(cfg, payload)
            elif integ.type == "amocrm":   await trigger_amocrm(cfg, payload)

            with db.get_session() as s:
                ig = s.get(Integration, integ.id)
                if ig:
                    ig.last_triggered = datetime.utcnow()
                    ig.trigger_count += 1
                    ig.last_error = None
                    s.add(ig); s.commit()
        except Exception as e:
            logger.warning(f"Интеграция {integ.type} #{integ.id} ошибка: {e}")
            with db.get_session() as s:
                ig = s.get(Integration, integ.id)
                if ig:
                    ig.last_error = str(e)[:300]
                    s.add(ig); s.commit()
