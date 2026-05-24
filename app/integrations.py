"""Рабочие интеграции: Webhook, Email, Google Sheets, Bitrix24, amoCRM."""
import json, logging, smtplib, ssl, uuid
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import httpx
from sqlmodel import select
from app.db import Integration, get_session

logger = logging.getLogger(__name__)


def get_bot_integrations(bot_id: int) -> list:
    with get_session() as s:
        return list(s.exec(select(Integration).where(Integration.bot_id == bot_id)))


async def trigger_webhook(cfg: dict, payload: dict) -> None:
    url = cfg.get("url", "").strip()
    if not url: raise ValueError("URL не задан")
    headers = {"Content-Type": "application/json"}
    if cfg.get("secret"): headers["X-Replika-Secret"] = cfg["secret"]
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()


async def trigger_email(cfg: dict, payload: dict) -> None:
    smtp_user = cfg.get("smtp_user", "").strip()
    smtp_pass = cfg.get("smtp_pass", "").strip()
    to_email  = cfg.get("to_email", "").strip()
    smtp_host = cfg.get("smtp_host", "smtp.gmail.com")
    smtp_port = int(cfg.get("smtp_port", 587))
    if not all([smtp_user, smtp_pass, to_email]):
        raise ValueError("smtp_user, smtp_pass и to_email обязательны")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[Реплика] Новый диалог — {payload.get('bot_name','')}"
    msg["From"] = smtp_user; msg["To"] = to_email
    html = f"""<html><body style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:20px">
<div style="background:#12100C;color:#F7F2E8;padding:16px 20px;border-radius:10px 10px 0 0">
  <strong>{payload.get('bot_name','Бот')}</strong> · Реплика · {payload.get('timestamp','')}
</div>
<div style="border:1px solid #EDE6D8;padding:20px;border-radius:0 0 10px 10px">
  <p><strong>Пользователь:</strong> {payload.get('user_name','')}</p>
  <div style="background:#F7F2E8;padding:12px;border-radius:6px;margin:8px 0"><em>«{payload.get('user_message','')}»</em></div>
  <p><strong>Ответ бота:</strong></p>
  <div style="background:#E8F5E9;padding:12px;border-radius:6px;margin:8px 0">{payload.get('bot_reply','')}</div>
  <p style="font-size:12px;color:#847B6E;margin-top:12px">Канал: {payload.get('channel','telegram')}</p>
</div></body></html>"""
    msg.attach(MIMEText(html, "html"))
    ctx = ssl.create_default_context()
    with smtplib.SMTP(smtp_host, smtp_port) as srv:
        srv.ehlo(); srv.starttls(context=ctx); srv.login(smtp_user, smtp_pass)
        srv.sendmail(smtp_user, to_email, msg.as_string())


async def trigger_sheets(cfg: dict, payload: dict) -> None:
    url = cfg.get("webapp_url", "").strip()
    if not url: raise ValueError("webapp_url не задан")
    row = {k: payload.get(k, "") for k in ["timestamp","bot_name","user_name","user_message","bot_reply","channel"]}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json=row); resp.raise_for_status()


async def trigger_bitrix24(cfg: dict, payload: dict) -> None:
    webhook = cfg.get("webhook_url", "").strip().rstrip("/")
    if not webhook: raise ValueError("webhook_url не задан")
    comment = f"Бот «{payload.get('bot_name','')}»:\n\n— {payload.get('user_message','')}\n— {payload.get('bot_reply','')}"
    data = {"fields": {"TITLE": f"Реплика · {payload.get('bot_name','')} · {payload.get('user_name','')}",
        "NAME": payload.get("user_name","Клиент"), "COMMENTS": comment, "SOURCE_ID": "WEB"}}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(f"{webhook}/crm.lead.add.json", json=data); resp.raise_for_status()


async def trigger_amocrm(cfg: dict, payload: dict) -> None:
    domain = cfg.get("domain","").strip(); token = cfg.get("api_token","").strip()
    if not all([domain, token]): raise ValueError("domain и api_token обязательны")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(f"https://{domain}/api/v4/contacts",
            json=[{"name": payload.get("user_name","Клиент")}], headers=headers)
        resp.raise_for_status()


async def fire_all(bot_id: int, bot_name: str, user_name: str,
                   user_message: str, bot_reply: str, channel: str = "telegram") -> None:
    payload = {
        "bot_id": bot_id, "bot_name": bot_name, "user_name": user_name,
        "user_message": user_message, "bot_reply": bot_reply, "channel": channel,
        "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    }
    for integ in get_bot_integrations(bot_id):
        if not integ.enabled: continue
        cfg = integ.get_config()
        try:
            fn = {"webhook": trigger_webhook, "email": trigger_email, "sheets": trigger_sheets,
                  "bitrix24": trigger_bitrix24, "amocrm": trigger_amocrm}.get(integ.type)
            if fn: await fn(cfg, payload)
            with get_session() as s:
                ig = s.get(Integration, integ.id)
                if ig: ig.last_triggered = datetime.utcnow(); ig.trigger_count += 1; ig.last_error = None; s.add(ig); s.commit()
        except Exception as e:
            logger.warning(f"Интеграция {integ.type}: {e}")
            with get_session() as s:
                ig = s.get(Integration, integ.id)
                if ig: ig.last_error = str(e)[:300]; s.add(ig); s.commit()
