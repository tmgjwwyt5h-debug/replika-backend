"""LLM клиент. GigaChat через прямые HTTP-запросы, OpenAI, Claude."""
import os
import ssl
import time
import httpx
import certifi
from typing import Optional
from app.db import Bot, Message, KnowledgeChunk

# Кеш токена GigaChat (действует 30 мин)
_giga_token: Optional[str] = None
_giga_token_expires: float = 0


def build_system_prompt(bot: Bot, knowledge: list[KnowledgeChunk]) -> str:
    parts = [bot.system_prompt.strip()]
    if knowledge:
        parts.append("\n\n=== БАЗА ЗНАНИЙ ===")
        parts.append("Используй информацию ниже для ответов.\n")
        for k in knowledge:
            parts.append(f"\n### {k.title}\n{k.content}")
    parts.append(
        "\n\n=== ПРАВИЛА ===\n"
        "- Отвечай на русском языке.\n"
        "- Будь краток: 1–3 предложения, если не просили подробнее.\n"
        "- Не выдумывай факты которых нет в инструкции.\n"
        "- Если не знаешь ответа — предложи связаться с человеком."
    )
    return "\n".join(parts)


def _get_gigachat_token() -> str:
    """Получаем access token GigaChat. Кешируем на 29 минут."""
    global _giga_token, _giga_token_expires
    if _giga_token and time.time() < _giga_token_expires:
        return _giga_token

    creds = os.getenv("GIGACHAT_CREDENTIALS")
    if not creds:
        raise RuntimeError("GIGACHAT_CREDENTIALS не задан в переменных окружения")

    scope = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")

    # GigaChat использует самоподписанный cert — отключаем верификацию
    with httpx.Client(verify=False, timeout=15) as client:
        resp = client.post(
            "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
            headers={
                "Authorization": f"Basic {creds}",
                "RqUID": "replika-bot",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"scope": scope},
        )
        resp.raise_for_status()
        data = resp.json()

    _giga_token = data["access_token"]
    _giga_token_expires = time.time() + 29 * 60
    return _giga_token


def generate_reply_gigachat(bot: Bot, system_prompt: str, history: list[Message], user_text: str) -> tuple[str, int]:
    token = _get_gigachat_token()
    messages = [{"role": "system", "content": system_prompt}]
    for m in history:
        if m.role in ("user", "assistant"):
            messages.append({"role": m.role, "content": m.content})
    messages.append({"role": "user", "content": user_text})

    with httpx.Client(verify=False, timeout=30) as client:
        resp = client.post(
            "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "model": bot.llm_model or "GigaChat",
                "messages": messages,
                "temperature": bot.temperature,
                "max_tokens": bot.max_tokens,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    text = data["choices"][0]["message"]["content"].strip()
    tokens = data.get("usage", {}).get("total_tokens", 0)
    return text, tokens


def generate_reply_openai(bot: Bot, system_prompt: str, history: list[Message], user_text: str) -> tuple[str, int]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY не задан")
    messages = [{"role": "system", "content": system_prompt}]
    for m in history:
        if m.role in ("user", "assistant"):
            messages.append({"role": m.role, "content": m.content})
    messages.append({"role": "user", "content": user_text})
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": bot.llm_model or "gpt-4o-mini", "messages": messages,
                  "temperature": bot.temperature, "max_tokens": bot.max_tokens},
        )
        resp.raise_for_status()
        data = resp.json()
    text = data["choices"][0]["message"]["content"].strip()
    tokens = data.get("usage", {}).get("total_tokens", 0)
    return text, tokens


def generate_reply(bot: Bot, knowledge: list[KnowledgeChunk], history: list[Message], user_text: str) -> tuple[str, int]:
    system_prompt = build_system_prompt(bot, knowledge)
    provider = bot.llm_provider.lower()
    if provider == "gigachat":
        return generate_reply_gigachat(bot, system_prompt, history, user_text)
    elif provider == "openai":
        return generate_reply_openai(bot, system_prompt, history, user_text)
    else:
        raise RuntimeError(f"Неизвестный провайдер: {provider}")
