"""Реплика · FastAPI backend + Telegram runner."""
import os, json, logging
from contextlib import asynccontextmanager
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import select

import app.db as db
from app.db import Integration, BotFlow, get_bot_flow, save_bot_flow
from app.llm import generate_reply
from app.integrations import get_bot_integrations, fire_all
from app import auth
import app.telegram_runner as tg

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

BASE = os.path.dirname(os.path.abspath(__file__))


@asynccontextmanager
async def lifespan(app_: FastAPI):
    try:
        db.init_db()
        logger.info("DB OK")
    except Exception as e:
        logger.error(f"DB init failed: {e}")
        raise
    try:
        await tg.start_all_active()
    except Exception as e:
        logger.warning(f"TG start failed: {e}")
    yield
    await tg.stop_all()


app = FastAPI(title="Реплика", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
T = Jinja2Templates(directory=os.path.join(BASE, "templates"))


# ════════════════════════════════════════════════════════════
#  AUTH MIDDLEWARE & LOGIN
# ════════════════════════════════════════════════════════════
@app.middleware("http")
async def auth_mw(request: Request, call_next):
    path = request.url.path
    if auth.is_public(path):
        return await call_next(request)
    user = auth.get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    # Если уже залогинен — на главную
    if auth.get_current_user(request):
        return RedirectResponse("/", status_code=303)
    return T.TemplateResponse("login.html", {
        "request": request, "error": bool(error),
    })


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...), password: str = Form(...),
):
    if auth.check_credentials(username.strip(), password):
        resp = RedirectResponse("/", status_code=303)
        token = auth._sign(username.strip())
        resp.set_cookie(
            auth.COOKIE_NAME, token,
            max_age=7 * 24 * 3600,   # 7 дней
            httponly=True, samesite="lax",
        )
        return resp
    return RedirectResponse("/login?error=1", status_code=303)


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp



# ── Health ──────────────────────────────────────────────────
@app.get("/health")
async def health(): return {"ok": True}


# ── Dashboard ───────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    bots = db.get_all_bots()
    return T.TemplateResponse("dashboard.html", {
        "request": request, "bots": bots,
        "running_ids": list(tg._running.keys()),
        "total_messages": sum(b.total_messages for b in bots),
        "active_count": sum(1 for b in bots if b.status == "active"),
    })


# ── Create bot ──────────────────────────────────────────────
@app.get("/bots/new", response_class=HTMLResponse)
async def new_bot_form(request: Request):
    return T.TemplateResponse("bot_new.html", {"request": request})


@app.post("/bots/new")
async def new_bot_create(
    request: Request,
    name: str = Form(...),
    industry: str = Form("general"),
    description: str = Form(""),
    system_prompt: str = Form(...),
    greeting: str = Form("Здравствуйте! Чем могу помочь?"),
    llm_provider: str = Form("gigachat"),
    llm_model: str = Form("GigaChat"),
    temperature: float = Form(0.3),
    telegram_token: str = Form(""),
):
    bot = db.Bot(
        name=name.strip(), industry=industry,
        description=description.strip(),
        system_prompt=system_prompt.strip(),
        greeting=greeting.strip(),
        llm_provider=llm_provider,
        llm_model=(llm_model.strip() or "GigaChat"),
        temperature=temperature,
        telegram_token=(telegram_token.strip() or None),
    )
    with db.get_session() as s:
        s.add(bot); s.commit(); s.refresh(bot)
    if bot.telegram_token:
        with db.get_session() as s:
            b = s.get(db.Bot, bot.id); b.telegram_enabled = True; s.add(b); s.commit()
        await tg.start_bot(bot.id)
    return RedirectResponse(f"/bots/{bot.id}", status_code=303)


# ── Bot detail ──────────────────────────────────────────────
@app.get("/bots/{bot_id}", response_class=HTMLResponse)
async def bot_detail(request: Request, bot_id: int):
    bot = db.get_bot(bot_id)
    if not bot: raise HTTPException(404)
    knowledge = db.get_bot_knowledge(bot_id)
    with db.get_session() as s:
        convs = list(s.exec(select(db.Conversation)
            .where(db.Conversation.bot_id == bot_id)
            .order_by(db.Conversation.last_message_at.desc()).limit(10)))
    return T.TemplateResponse("bot_detail.html", {
        "request": request, "bot": bot, "knowledge": knowledge,
        "conversations": convs, "is_running": tg.is_running(bot_id),
    })


@app.post("/bots/{bot_id}/edit")
async def bot_edit(
    request: Request, bot_id: int,
    name: str = Form(...), description: str = Form(""), industry: str = Form("general"),
    system_prompt: str = Form(...), greeting: str = Form(...),
    llm_provider: str = Form(...), llm_model: str = Form(...),
    temperature: float = Form(...), telegram_token: str = Form(""),
):
    with db.get_session() as s:
        bot = s.get(db.Bot, bot_id)
        if not bot: raise HTTPException(404)
        old_token = bot.telegram_token
        bot.name = name.strip(); bot.description = description.strip()
        bot.industry = industry; bot.system_prompt = system_prompt.strip()
        bot.greeting = greeting.strip(); bot.llm_provider = llm_provider
        bot.llm_model = llm_model.strip(); bot.temperature = temperature
        bot.telegram_token = telegram_token.strip() or None
        bot.updated_at = datetime.utcnow()
        s.add(bot); s.commit()
    if (telegram_token.strip() or None) != old_token:
        await tg.stop_bot(bot_id)
        if telegram_token.strip():
            await tg.start_bot(bot_id)
    return RedirectResponse(f"/bots/{bot_id}", status_code=303)


# ── Start / Stop / Delete ────────────────────────────────────
@app.post("/bots/{bot_id}/start")
async def bot_start(bot_id: int):
    bot = db.get_bot(bot_id)
    if not bot: raise HTTPException(404)
    if not bot.telegram_token: raise HTTPException(400, "Нет токена")
    with db.get_session() as s:
        b = s.get(db.Bot, bot_id); b.telegram_enabled = True; s.add(b); s.commit()
    await tg.start_bot(bot_id)
    return RedirectResponse(f"/bots/{bot_id}", status_code=303)


@app.post("/bots/{bot_id}/stop")
async def bot_stop(bot_id: int):
    await tg.stop_bot(bot_id)
    with db.get_session() as s:
        b = s.get(db.Bot, bot_id)
        if b: b.telegram_enabled = False; s.add(b); s.commit()
    return RedirectResponse(f"/bots/{bot_id}", status_code=303)


@app.post("/bots/{bot_id}/delete")
async def bot_delete(bot_id: int):
    await tg.stop_bot(bot_id)
    with db.get_session() as s:
        bot = s.get(db.Bot, bot_id)
        if bot:
            for tbl in [db.Message, db.Conversation, db.KnowledgeChunk, Integration]:
                for item in s.exec(select(tbl).where(tbl.bot_id == bot_id)):
                    s.delete(item)
            s.delete(bot); s.commit()
    return RedirectResponse("/", status_code=303)


# ── Knowledge ────────────────────────────────────────────────
@app.post("/bots/{bot_id}/knowledge")
async def knowledge_add(bot_id: int, title: str = Form(...), content: str = Form(...)):
    with db.get_session() as s:
        if not s.get(db.Bot, bot_id): raise HTTPException(404)
        s.add(db.KnowledgeChunk(bot_id=bot_id, title=title.strip(), content=content.strip()))
        s.commit()
    return RedirectResponse(f"/bots/{bot_id}#knowledge", status_code=303)


@app.post("/bots/{bot_id}/knowledge/{k_id}/delete")
async def knowledge_delete(bot_id: int, k_id: int):
    with db.get_session() as s:
        k = s.get(db.KnowledgeChunk, k_id)
        if k and k.bot_id == bot_id: s.delete(k); s.commit()
    return RedirectResponse(f"/bots/{bot_id}#knowledge", status_code=303)


# ── Conversations ────────────────────────────────────────────
@app.get("/bots/{bot_id}/conversations/{conv_id}", response_class=HTMLResponse)
async def conversation_view(request: Request, bot_id: int, conv_id: int):
    bot = db.get_bot(bot_id)
    if not bot: raise HTTPException(404)
    with db.get_session() as s:
        conv = s.get(db.Conversation, conv_id)
        if not conv or conv.bot_id != bot_id: raise HTTPException(404)
        messages = db.get_recent_messages(conv_id, limit=200)
    return T.TemplateResponse("conversation.html", {
        "request": request, "bot": bot, "conversation": conv, "messages": messages,
    })


# ── Test chat ────────────────────────────────────────────────
@app.get("/bots/{bot_id}/chat", response_class=HTMLResponse)
async def bot_chat_page(request: Request, bot_id: int):
    bot = db.get_bot(bot_id)
    if not bot: raise HTTPException(404)
    return T.TemplateResponse("chat_test.html", {"request": request, "bot": bot})


@app.post("/bots/{bot_id}/test")
async def bot_test(request: Request, bot_id: int,
                   message: str = Form(...), session_id: str = Form("test-session")):
    bot = db.get_bot(bot_id)
    if not bot: raise HTTPException(404)
    conv = db.get_or_create_conversation(bot_id, "web", session_id, "Тестер")
    db.save_message(conv.id, bot_id, "user", message)
    try:
        history = db.get_recent_messages(conv.id, limit=20)[:-1]
        knowledge = db.get_bot_knowledge(bot_id)
        reply, tokens = generate_reply(bot, knowledge, history, message)
        db.save_message(conv.id, bot_id, "assistant", reply, tokens=tokens)
    except Exception as e:
        logger.exception("LLM error")
        reply = f"Ошибка: {e}"
    return T.TemplateResponse("partials/_test_messages.html", {
        "request": request, "user_message": message, "bot_reply": reply,
    })


# ── Integrations ─────────────────────────────────────────────
@app.get("/bots/{bot_id}/integrations", response_class=HTMLResponse)
async def integrations_page(request: Request, bot_id: int):
    bot = db.get_bot(bot_id)
    if not bot: raise HTTPException(404)
    igs = get_bot_integrations(bot_id)
    return T.TemplateResponse("bot_integrations.html", {
        "request": request, "bot": bot, "integrations": igs,
    })


@app.post("/bots/{bot_id}/integrations/add")
async def integration_add(
    bot_id: int, type: str = Form(...), name: str = Form(...),
    url: str = Form(""), secret: str = Form(""),
    smtp_user: str = Form(""), smtp_pass: str = Form(""),
    to_email: str = Form(""), smtp_host: str = Form("smtp.gmail.com"),
    smtp_port: str = Form("587"), webapp_url: str = Form(""),
    webhook_url: str = Form(""), domain: str = Form(""), api_token: str = Form(""),
):
    cfg_map = {
        "webhook":  {"url": url.strip(), "secret": secret.strip()},
        "email":    {"smtp_user": smtp_user.strip(), "smtp_pass": smtp_pass,
                     "to_email": to_email.strip(), "smtp_host": smtp_host.strip(),
                     "smtp_port": smtp_port.strip()},
        "sheets":   {"webapp_url": webapp_url.strip()},
        "bitrix24": {"webhook_url": webhook_url.strip()},
        "amocrm":   {"domain": domain.strip(), "api_token": api_token},
    }
    cfg = cfg_map.get(type, {})
    with db.get_session() as s:
        ig = Integration(bot_id=bot_id, type=type, name=name.strip(),
                         config=json.dumps(cfg))
        s.add(ig); s.commit()
    return RedirectResponse(f"/bots/{bot_id}/integrations", status_code=303)


@app.post("/bots/{bot_id}/integrations/{ig_id}/toggle")
async def integration_toggle(bot_id: int, ig_id: int):
    with db.get_session() as s:
        ig = s.get(Integration, ig_id)
        if ig and ig.bot_id == bot_id:
            ig.enabled = not ig.enabled; s.add(ig); s.commit()
    return RedirectResponse(f"/bots/{bot_id}/integrations", status_code=303)


@app.post("/bots/{bot_id}/integrations/{ig_id}/delete")
async def integration_delete(bot_id: int, ig_id: int):
    with db.get_session() as s:
        ig = s.get(Integration, ig_id)
        if ig and ig.bot_id == bot_id: s.delete(ig); s.commit()
    return RedirectResponse(f"/bots/{bot_id}/integrations", status_code=303)


# ── Analytics ─────────────────────────────────────────────────
@app.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request):
    a = db.get_platform_analytics(30)
    all_bots = db.get_all_bots()
    bots_map = {b.id: {"name": b.name, "status": b.status} for b in all_bots}
    return T.TemplateResponse("analytics.html", {
        "request": request, "a": a, "all_bots": all_bots, "bots_map": bots_map,
    })


# ── Knowledge base (global) ───────────────────────────────────
@app.get("/knowledge", response_class=HTMLResponse)
async def knowledge_page(request: Request, bot: str = ""):
    all_bots = db.get_all_bots()
    bots_map = {b.id: {"name": b.name} for b in all_bots}
    chunks = db.get_all_knowledge()
    if bot:
        try:
            bid = int(bot); chunks = [c for c in chunks if c.bot_id == bid]
        except: pass
    return T.TemplateResponse("knowledge.html", {
        "request": request, "chunks": chunks,
        "all_bots": all_bots, "bots_map": bots_map, "bot_filter": bot,
    })


@app.post("/knowledge")
async def knowledge_add_global(title: str = Form(...), content: str = Form(...),
                                bot_id: str = Form("")):
    with db.get_session() as s:
        bid = int(bot_id) if bot_id.strip() else 0
        s.add(db.KnowledgeChunk(bot_id=bid, title=title.strip(), content=content.strip()))
        s.commit()
    return RedirectResponse("/knowledge", status_code=303)


@app.post("/knowledge/{k_id}/delete")
async def knowledge_delete_global(k_id: int):
    with db.get_session() as s:
        k = s.get(db.KnowledgeChunk, k_id)
        if k: s.delete(k); s.commit()
    return RedirectResponse("/knowledge", status_code=303)


# ── Billing ──────────────────────────────────────────────────
@app.get("/billing", response_class=HTMLResponse)
async def billing_page(request: Request):
    a = db.get_platform_analytics(30)
    return T.TemplateResponse("billing.html", {"request": request, "a": a})


# ── Entry point ──────────────────────────────────────────────

# ── Flow constructor ──────────────────────────────────────
@app.get("/bots/{bot_id}/flow", response_class=HTMLResponse)
async def flow_page(request: Request, bot_id: int):
    bot = db.get_bot(bot_id)
    if not bot: raise HTTPException(404)
    return T.TemplateResponse("bot_flow.html", {"request": request, "bot": bot})

@app.get("/bots/{bot_id}/flow/data")
async def flow_get(bot_id: int):
    from fastapi.responses import JSONResponse
    flow = get_bot_flow(bot_id)
    if not flow: return JSONResponse({})
    import json
    return JSONResponse(json.loads(flow.data))

@app.post("/bots/{bot_id}/flow/save")
async def flow_save(bot_id: int, request: Request):
    import json
    body = await request.json()
    save_bot_flow(bot_id, json.dumps(body))
    return {"ok": True}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=False)
