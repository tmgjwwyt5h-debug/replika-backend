"""LLM клиент. GigaChat через прямые HTTP-запросы + OpenAI."""
import os, time, uuid
import httpx
from app.db import Bot, Message, KnowledgeChunk

_giga_token: str = ""
_giga_expires: float = 0


def build_system_prompt(bot: Bot, knowledge: list[KnowledgeChunk]) -> str:
    parts = [bot.system_prompt.strip()]
    if knowledge:
        parts.append("\n\n=== БАЗА ЗНАНИЙ ===")
        for k in knowledge:
            parts.append(f"\n### {k.title}\n{k.content}")
    parts.append(
        "\n\n=== ПРАВИЛА ===\n"
        "- Отвечай на русском языке.\n"
        "- Будь краток: 1–3 предложения.\n"
        "- Не выдумывай факты которых нет в инструкции.\n"
        "- Если не знаешь ответа — предложи связаться с человеком."
    )
    return "\n".join(parts)


def _get_giga_token() -> str:
    global _giga_token, _giga_expires
    if _giga_token and time.time() < _giga_expires:
        return _giga_token

    creds = os.getenv("GIGACHAT_CREDENTIALS", "").strip()
    if not creds:
        raise RuntimeError(
            "GIGACHAT_CREDENTIALS не задан.\n"
            "Добавьте его в Variables на Railway:\n"
            "GIGACHAT_CREDENTIALS = ваш_ключ_авторизации\n"
            "Ключ берётся на https://developers.sber.ru → ваш проект → Authorization key"
        )

    scope = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")

    with httpx.Client(verify=False, timeout=20) as client:
        resp = client.post(
            "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
            headers={
                "Authorization":  f"Basic {creds}",
                "RqUID":          str(uuid.uuid4()),   # ← обязательно UUID формат
                "Accept":         "application/json",  # ← обязательно
                "Content-Type":   "application/x-www-form-urlencoded",
            },
            content=f"scope={scope}",                  # ← строка, не dict
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"GigaChat auth ошибка {resp.status_code}: {resp.text[:300]}\n"
                f"Проверьте GIGACHAT_CREDENTIALS — он должен быть точным значением "
                f"поля 'Authorization key' из личного кабинета Сбер."
            )
        data = resp.json()

    _giga_token = data["access_token"]
    _giga_expires = time.time() + 29 * 60
    return _giga_token


def _gigachat(bot: Bot, system: str, history: list[Message], user_text: str) -> tuple[str, int]:
    token = _get_giga_token()
    messages = [{"role": "system", "content": system}]
    for m in history:
        if m.role in ("user", "assistant"):
            messages.append({"role": m.role, "content": m.content})
    messages.append({"role": "user", "content": user_text})

    with httpx.Client(verify=False, timeout=30) as client:
        resp = client.post(
            "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            json={"model": bot.llm_model or "GigaChat",
                  "messages": messages,
                  "temperature": bot.temperature,
                  "max_tokens": bot.max_tokens},
        )
        resp.raise_for_status()
        d = resp.json()

    return d["choices"][0]["message"]["content"].strip(), d.get("usage", {}).get("total_tokens", 0)


def _openai(bot: Bot, system: str, history: list[Message], user_text: str) -> tuple[str, int]:
    key = os.getenv("OPENAI_API_KEY", "")
    if not key:
        raise RuntimeError("OPENAI_API_KEY не задан в Variables на Railway")
    msgs = [{"role": "system", "content": system}]
    for m in history:
        if m.role in ("user", "assistant"):
            msgs.append({"role": m.role, "content": m.content})
    msgs.append({"role": "user", "content": user_text})
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": bot.llm_model or "gpt-4o-mini",
                  "messages": msgs,
                  "temperature": bot.temperature,
                  "max_tokens": bot.max_tokens},
        )
        resp.raise_for_status()
        d = resp.json()
    return d["choices"][0]["message"]["content"].strip(), d.get("usage", {}).get("total_tokens", 0)


def generate_reply(bot: Bot, knowledge: list[KnowledgeChunk],
                   history: list[Message], user_text: str) -> tuple[str, int]:
    system = build_system_prompt(bot, knowledge)
    if bot.llm_provider == "gigachat":
        return _gigachat(bot, system, history, user_text)
    elif bot.llm_provider == "openai":
        return _openai(bot, system, history, user_text)
    raise RuntimeError(f"Неизвестный провайдер: {bot.llm_provider}")
