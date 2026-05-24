"""FastAPI приложение: админка + Telegram-раннер в одном процессе."""
import os
import logging
from contextlib import asynccontextmanager
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import select

from app import db, llm, telegram_runner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        db.init_db()
        logger.info("База данных инициализирована")
    except Exception as e:
        logger.error(f"Ошибка БД: {e}")
        raise
    try:
        await telegram_runner.start_all_active()
    except Exception as e:
        logger.warning(f"Не удалось запустить ботов при старте: {e}")
    yield
    await telegram_runner.stop_all()


app = FastAPI(title="Реплика · Платформа ботов", lifespan=lifespan)

# Статика и шаблоны — пути относительно рабочей директории /app в контейнере
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


# ============ HEALTH CHECK ============
@app.get("/health")
async def health():
    return {"status": "ok"}


# ============ DASHBOARD ============
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    bots = db.get_all_bots()
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "bots": bots,
        "sidebar_bots": bots[:8],
        "running_ids": list(telegram_runner._running.keys()),
        "total_messages": sum(b.total_messages for b in bots),
        "active_count": sum(1 for b in bots if b.status == "active"),
    })


# ============ CREATE BOT ============
@app.get("/bots/new", response_class=HTMLResponse)
async def new_bot_form(request: Request):
    return templates.TemplateResponse("bot_new.html", {"request": request})


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
        name=name.strip(),
        industry=industry,
        description=description.strip(),
        system_prompt=system_prompt.strip(),
        greeting=greeting.strip(),
        llm_provider=llm_provider,
        llm_model=llm_model.strip() or default_model(llm_provider),
        temperature=temperature,
        telegram_token=telegram_token.strip() or None,
    )
    with db.get_session() as s:
        s.add(bot); s.commit(); s.refresh(bot)
    if bot.telegram_token:
        bot.telegram_enabled = True
        with db.get_session() as s:
            s.add(bot); s.commit()
        await telegram_runner.start_bot(bot.id)
    return RedirectResponse(f"/bots/{bot.id}", status_code=303)


def default_model(provider: str) -> str:
    return {"gigachat": "GigaChat", "openai": "gpt-4o-mini", "anthropic": "claude-3-5-haiku-20241022"}.get(provider, "GigaChat")


# ============ BOT DETAIL ============
@app.get("/bots/{bot_id}", response_class=HTMLResponse)
async def bot_detail(request: Request, bot_id: int):
    bot = db.get_bot(bot_id)
    if not bot:
        raise HTTPException(404)
    knowledge = db.get_bot_knowledge(bot_id)
    with db.get_session() as s:
        convs = list(s.exec(
            select(db.Conversation).where(db.Conversation.bot_id == bot_id)
            .order_by(db.Conversation.last_message_at.desc()).limit(10)
        ))
    return templates.TemplateResponse("bot_detail.html", {
        "request": request, "bot": bot, "knowledge": knowledge,
        "conversations": convs, "is_running": telegram_runner.is_running(bot_id),
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
        if not bot:
            raise HTTPException(404)
        token_changed = (telegram_token.strip() or None) != bot.telegram_token
        bot.name = name.strip(); bot.description = description.strip()
        bot.industry = industry; bot.system_prompt = system_prompt.strip()
        bot.greeting = greeting.strip(); bot.llm_provider = llm_provider
        bot.llm_model = llm_model.strip(); bot.temperature = temperature
        bot.telegram_token = telegram_token.strip() or None
        bot.updated_at = datetime.utcnow()
        s.add(bot); s.commit()
    if token_changed:
        await telegram_runner.stop_bot(bot_id)
        if telegram_token.strip():
            await telegram_runner.start_bot(bot_id)
    return RedirectResponse(f"/bots/{bot_id}", status_code=303)


# ============ START / STOP ============
@app.post("/bots/{bot_id}/start")
async def bot_start(bot_id: int):
    bot = db.get_bot(bot_id)
    if not bot: raise HTTPException(404)
    if not bot.telegram_token: raise HTTPException(400, "Нет токена")
    with db.get_session() as s:
        b = s.get(db.Bot, bot_id); b.telegram_enabled = True; s.add(b); s.commit()
    await telegram_runner.start_bot(bot_id)
    return RedirectResponse(f"/bots/{bot_id}", status_code=303)


@app.post("/bots/{bot_id}/stop")
async def bot_stop(bot_id: int):
    await telegram_runner.stop_bot(bot_id)
    with db.get_session() as s:
        b = s.get(db.Bot, bot_id)
        if b: b.telegram_enabled = False; s.add(b); s.commit()
    return RedirectResponse(f"/bots/{bot_id}", status_code=303)


@app.post("/bots/{bot_id}/delete")
async def bot_delete(bot_id: int):
    await telegram_runner.stop_bot(bot_id)
    with db.get_session() as s:
        bot = s.get(db.Bot, bot_id)
        if bot:
            for table in [db.Message, db.Conversation, db.KnowledgeChunk]:
                for item in s.exec(select(table).where(table.bot_id == bot_id)):
                    s.delete(item)
            s.delete(bot); s.commit()
    return RedirectResponse("/", status_code=303)


# ============ KNOWLEDGE ============
@app.post("/bots/{bot_id}/knowledge")
async def add_knowledge(bot_id: int, title: str = Form(...), content: str = Form(...)):
    with db.get_session() as s:
        if not s.get(db.Bot, bot_id): raise HTTPException(404)
        k = db.KnowledgeChunk(bot_id=bot_id, title=title.strip(), content=content.strip())
        s.add(k); s.commit()
    return RedirectResponse(f"/bots/{bot_id}#knowledge", status_code=303)


@app.post("/bots/{bot_id}/knowledge/{k_id}/delete")
async def delete_knowledge(bot_id: int, k_id: int):
    with db.get_session() as s:
        k = s.get(db.KnowledgeChunk, k_id)
        if k and k.bot_id == bot_id: s.delete(k); s.commit()
    return RedirectResponse(f"/bots/{bot_id}#knowledge", status_code=303)


# ============ CONVERSATION ============
@app.get("/bots/{bot_id}/conversations/{conv_id}", response_class=HTMLResponse)
async def conversation_view(request: Request, bot_id: int, conv_id: int):
    bot = db.get_bot(bot_id)
    if not bot: raise HTTPException(404)
    with db.get_session() as s:
        conv = s.get(db.Conversation, conv_id)
        if not conv or conv.bot_id != bot_id: raise HTTPException(404)
        messages = db.get_recent_messages(conv_id, limit=200)
    return templates.TemplateResponse("conversation.html", {
        "request": request, "bot": bot, "conversation": conv, "messages": messages,
    })


# ============ TEST CHAT ============
@app.post("/bots/{bot_id}/test")
async def bot_test(
    request: Request, bot_id: int,
    message: str = Form(...),
    session_id: str = Form("test-session"),
):
    bot = db.get_bot(bot_id)
    if not bot: raise HTTPException(404)
    conv = db.get_or_create_conversation(
        bot_id=bot_id, channel="web", external_user_id=session_id, user_name="Тестер"
    )
    db.save_message(conv.id, bot_id, "user", message)
    try:
        history = db.get_recent_messages(conv.id, limit=20)[:-1]
        knowledge = db.get_bot_knowledge(bot_id)
        reply, tokens = llm.generate_reply(bot, knowledge, history, message)
        db.save_message(conv.id, bot_id, "assistant", reply, tokens=tokens)
    except Exception as e:
        logger.exception("LLM error in test")
        reply = f"Ошибка: {e}"
    return templates.TemplateResponse("partials/_test_messages.html", {
        "request": request, "user_message": message, "bot_reply": reply,
    })




# ============ ОТДЕЛЬНЫЙ ТЕСТовый ЧАТ ============
@app.get("/bots/{bot_id}/chat", response_class=HTMLResponse)
async def bot_chat_test(request: Request, bot_id: int):
    """Полноэкранный тест-чат без Telegram."""
    bot = db.get_bot(bot_id)
    if not bot:
        raise HTTPException(404)
    return templates.TemplateResponse("chat_test.html", {
        "request": request, "bot": bot,
    })


# ============ ANALYTICS ============
@app.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request):
    a = db.get_platform_analytics(30)
    all_bots = db.get_all_bots()
    bots_map = {b.id: {"name": b.name, "status": b.status} for b in all_bots}
    return templates.TemplateResponse("analytics.html", {
        "request": request, "a": a, "all_bots": all_bots, "bots_map": bots_map,
    })

# ============ KNOWLEDGE BASE (GLOBAL) ============
@app.get("/knowledge", response_class=HTMLResponse)
async def knowledge_page(request: Request, bot: str = ""):
    all_bots = db.get_all_bots()
    bots_map = {b.id: {"name": b.name} for b in all_bots}
    chunks = db.get_all_knowledge()
    if bot:
        try:
            bid = int(bot)
            chunks = [c for c in chunks if c.bot_id == bid]
        except:
            pass
    return templates.TemplateResponse("knowledge.html", {
        "request": request, "chunks": chunks,
        "all_bots": all_bots, "bots_map": bots_map, "bot_filter": bot,
    })

@app.post("/knowledge")
async def knowledge_add(
    title: str = Form(...), content: str = Form(...), bot_id: str = Form(""),
):
    with db.get_session() as s:
        bid = int(bot_id) if bot_id else None
        k = db.KnowledgeChunk(
            bot_id=bid or 0, title=title.strip(), content=content.strip()
        )
        s.add(k); s.commit()
    return RedirectResponse("/knowledge", status_code=303)

@app.post("/knowledge/{k_id}/delete")
async def knowledge_delete_global(k_id: int):
    with db.get_session() as s:
        k = s.get(db.KnowledgeChunk, k_id)
        if k: s.delete(k); s.commit()
    return RedirectResponse("/knowledge", status_code=303)

# ============ BILLING ============
@app.get("/billing", response_class=HTMLResponse)
async def billing_page(request: Request):
    a = db.get_platform_analytics(30)
    return templates.TemplateResponse("billing.html", {
        "request": request, "a": a,
    })

if __name__ == "__main__":
    import uvicorn
    # Railway сам задаёт PORT — берём его, по умолчанию 8000
    port = int(os.getenv("PORT", "8000"))
    logger.info(f"Запуск на 0.0.0.0:{port}")
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=False)

# ============ SIDEBAR HELPER ============
async def _sidebar(db_session):
    bots = db.get_all_bots()
    return bots[:8]

# ============ ANALYTICS ============
@app.get("/analytics", response_class=HTMLResponse)
async def analytics(request: Request):
    from sqlmodel import select, func
    from datetime import datetime, timedelta

    bots = db.get_all_bots()

    with db.get_session() as s:
        today = datetime.utcnow().date()
        month_start = today.replace(day=1)
        week_start = today - timedelta(days=7)

        total_msg = s.exec(select(func.count(db.Message.id))).one()
        today_msg = s.exec(select(func.count(db.Message.id)).where(
            func.date(db.Message.created_at) == str(today))).one()
        total_conv = s.exec(select(func.count(db.Conversation.id))).one()
        week_conv = s.exec(select(func.count(db.Conversation.id)).where(
            db.Conversation.created_at >= str(week_start))).one()
        total_tokens = s.exec(select(func.coalesce(func.sum(db.Message.tokens_used), 0))).one()

        # Per-bot stats
        bot_stats = []
        for b in bots:
            conv_cnt = s.exec(select(func.count(db.Conversation.id)).where(
                db.Conversation.bot_id == b.id)).one()
            tok = s.exec(select(func.coalesce(func.sum(db.Message.tokens_used), 0)).where(
                db.Message.bot_id == b.id)).one()
            bot_stats.append({
                "id": b.id, "name": b.name, "status": b.status,
                "conversations": conv_cnt, "total_messages": b.total_messages,
                "tokens": tok, "llm_provider": b.llm_provider
            })

        # Chart: 14 days
        chart_labels, chart_points, chart_line, chart_area = [], [], "", ""
        try:
            days_data = []
            for i in range(13, -1, -1):
                d = today - timedelta(days=i)
                cnt = s.exec(select(func.count(db.Message.id)).where(
                    func.date(db.Message.created_at) == str(d),
                    db.Message.role == "user")).one()
                days_data.append({"date": d, "count": cnt})

            max_cnt = max((d["count"] for d in days_data), default=1) or 1
            W, H = 700, 140
            pts = []
            for i, d in enumerate(days_data):
                x = 20 + i * (W - 40) / 13
                y = H - 10 - (d["count"] / max_cnt) * (H - 20)
                pts.append({"x": round(x), "y": round(y)})
                if i % 3 == 0:
                    chart_labels.append({"x": round(x), "label": d["date"].strftime("%d.%m")})
            chart_points = pts
            chart_line = "M " + " L ".join(f"{p['x']} {p['y']}" for p in pts)
            chart_area = chart_line + f" L {pts[-1]['x']} {H} L {pts[0]['x']} {H} Z"
        except Exception:
            pass

    free_limit = 1_000_000
    tokens_pct = min(round(total_tokens / free_limit * 100, 1), 100)

    return templates.TemplateResponse("analytics.html", {
        "request": request,
        "sidebar_bots": bots[:8],
        "stats": {
            "total_messages": total_msg,
            "today_messages": today_msg,
            "total_conversations": total_conv,
            "week_conversations": week_conv,
            "total_tokens": total_tokens,
            "tokens_pct": tokens_pct,
            "active_bots": sum(1 for b in bots if b.status == "active"),
            "total_bots": len(bots),
        },
        "bot_stats": bot_stats,
        "chart_points": chart_points,
        "chart_labels": chart_labels,
        "chart_line": chart_line,
        "chart_area": chart_area,
    })


# ============ KNOWLEDGE (GLOBAL) ============
@app.get("/knowledge", response_class=HTMLResponse)
async def knowledge_global(request: Request):
    bots = db.get_all_bots()
    from sqlmodel import select
    with db.get_session() as s:
        all_chunks = list(s.exec(select(db.KnowledgeChunk).order_by(db.KnowledgeChunk.created_at.desc())))

    bot_map = {b.id: b.name for b in bots}
    chunks_by_bot = {}
    for k in all_chunks:
        name = bot_map.get(k.bot_id, f"Бот #{k.bot_id}")
        chunks_by_bot.setdefault(name, []).append(k)

    return templates.TemplateResponse("knowledge.html", {
        "request": request,
        "sidebar_bots": bots[:8],
        "bots": bots,
        "chunks_by_bot": chunks_by_bot,
        "total_chunks": len(all_chunks),
    })


@app.post("/knowledge/add")
async def knowledge_add_global(
    bot_id: int = Form(...),
    title: str = Form(...),
    content: str = Form(...),
):
    with db.get_session() as s:
        if not s.get(db.Bot, bot_id):
            raise HTTPException(404)
        k = db.KnowledgeChunk(bot_id=bot_id, title=title.strip(), content=content.strip())
        s.add(k); s.commit()
    return RedirectResponse("/knowledge", status_code=303)


@app.post("/knowledge/{k_id}/delete")
async def knowledge_delete_global(k_id: int):
    with db.get_session() as s:
        k = s.get(db.KnowledgeChunk, k_id)
        if k: s.delete(k); s.commit()
    return RedirectResponse("/knowledge", status_code=303)


@app.post("/knowledge/{k_id}/toggle")
async def knowledge_toggle(k_id: int):
    with db.get_session() as s:
        k = s.get(db.KnowledgeChunk, k_id)
        if k:
            k.enabled = not k.enabled
            s.add(k); s.commit()
    return RedirectResponse("/knowledge", status_code=303)


# ============ BILLING ============
@app.get("/billing", response_class=HTMLResponse)
async def billing(request: Request):
    from sqlmodel import select, func
    from datetime import datetime

    bots = db.get_all_bots()
    month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    with db.get_session() as s:
        month_tokens = s.exec(select(func.coalesce(func.sum(db.Message.tokens_used), 0)).where(
            db.Message.created_at >= month_start)).one()
        month_messages = s.exec(select(func.count(db.Message.id)).where(
            db.Message.created_at >= month_start, db.Message.role == "user")).one()

        bot_usage = []
        total_tok = month_tokens or 1
        for b in bots:
            tok = s.exec(select(func.coalesce(func.sum(db.Message.tokens_used), 0)).where(
                db.Message.bot_id == b.id,
                db.Message.created_at >= month_start)).one()
            msg = s.exec(select(func.count(db.Message.id)).where(
                db.Message.bot_id == b.id,
                db.Message.created_at >= month_start,
                db.Message.role == "user")).one()
            bot_usage.append({
                "id": b.id, "name": b.name, "provider": b.llm_provider,
                "messages": msg, "tokens": tok,
                "pct": round(tok / total_tok * 100) if total_tok else 0,
                "est_usd": round(tok / 1_000_000 * 0.15, 4),
            })

    free_limit = 1_000_000
    tokens_pct = min(round(month_tokens / free_limit * 100, 1), 100)
    remaining = max(free_limit - month_tokens, 0)
    avg_per_msg = round(month_tokens / month_messages) if month_messages else 0
    daily_avg = round(month_tokens / max(datetime.utcnow().day, 1))
    days_left = round(remaining / daily_avg) if daily_avg else 999

    return templates.TemplateResponse("billing.html", {
        "request": request,
        "sidebar_bots": bots[:8],
        "billing": {
            "month_tokens": month_tokens,
            "month_messages": month_messages,
            "tokens_pct": tokens_pct,
            "remaining": remaining,
            "days_left": days_left,
            "avg_tokens_per_msg": avg_per_msg,
            "daily_tokens": daily_avg,
            "est_cost": 0 if month_tokens < free_limit else round((month_tokens - free_limit) / 1_000_000 * 350, 0),
            "bot_usage": sorted(bot_usage, key=lambda x: x["tokens"], reverse=True),
        },
    })
