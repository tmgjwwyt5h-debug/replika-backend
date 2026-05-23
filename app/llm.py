"""LLM клиент. Поддерживает GigaChat, OpenAI и Claude через единый интерфейс."""
import os
from typing import Optional
from app.db import Bot, Message, KnowledgeChunk


def build_system_prompt(bot: Bot, knowledge: list[KnowledgeChunk]) -> str:
    """Собирает финальный system prompt: бот.system_prompt + база знаний."""
    parts = [bot.system_prompt.strip()]

    if knowledge:
        parts.append("\n\n=== БАЗА ЗНАНИЙ ===")
        parts.append("Используй информацию ниже для ответов. Если в базе знаний нет ответа — честно скажи об этом, не выдумывай.\n")
        for k in knowledge:
            parts.append(f"\n### {k.title}\n{k.content}")

    parts.append(
        "\n\n=== ОБЩИЕ ПРАВИЛА ===\n"
        "- Отвечай на русском языке.\n"
        "- Будь краток: 1-3 предложения, если не просили подробнее.\n"
        "- Не выдумывай факты, которых нет в инструкции и базе знаний.\n"
        "- Если не знаешь ответа — предложи связаться с человеком."
    )
    return "\n".join(parts)


def to_provider_messages(provider: str, system_prompt: str, history: list[Message], new_user_text: str):
    """Конвертирует историю в формат, понятный конкретному провайдеру."""
    messages = []

    if provider in ("openai", "anthropic", "gigachat"):
        # У всех трёх схожий формат
        if provider != "anthropic":
            messages.append({"role": "system", "content": system_prompt})
        for m in history:
            if m.role in ("user", "assistant"):
                messages.append({"role": m.role, "content": m.content})
        messages.append({"role": "user", "content": new_user_text})

    return messages


def generate_reply_gigachat(bot: Bot, system_prompt: str, history: list[Message], new_user_text: str) -> tuple[str, int]:
    """Ответ через GigaChat. Возвращает (текст, токенов)."""
    from gigachat import GigaChat
    from gigachat.models import Chat, Messages, MessagesRole

    creds = os.getenv("GIGACHAT_CREDENTIALS")
    if not creds:
        raise RuntimeError("GIGACHAT_CREDENTIALS не задан в .env")

    scope = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")

    msgs = [Messages(role=MessagesRole.SYSTEM, content=system_prompt)]
    for m in history:
        if m.role == "user":
            msgs.append(Messages(role=MessagesRole.USER, content=m.content))
        elif m.role == "assistant":
            msgs.append(Messages(role=MessagesRole.ASSISTANT, content=m.content))
    msgs.append(Messages(role=MessagesRole.USER, content=new_user_text))

    payload = Chat(
        messages=msgs,
        temperature=bot.temperature,
        max_tokens=bot.max_tokens,
        model=bot.llm_model or "GigaChat",
    )

    with GigaChat(credentials=creds, scope=scope, verify_ssl_certs=False) as giga:
        response = giga.chat(payload)

    text = response.choices[0].message.content
    tokens = getattr(response.usage, "total_tokens", 0) if response.usage else 0
    return text.strip(), tokens


def generate_reply_openai(bot: Bot, system_prompt: str, history: list[Message], new_user_text: str) -> tuple[str, int]:
    """Ответ через OpenAI."""
    from openai import OpenAI
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY не задан")
    client = OpenAI(api_key=api_key)

    messages = to_provider_messages("openai", system_prompt, history, new_user_text)
    resp = client.chat.completions.create(
        model=bot.llm_model or "gpt-4o-mini",
        messages=messages,
        temperature=bot.temperature,
        max_tokens=bot.max_tokens,
    )
    text = resp.choices[0].message.content.strip()
    tokens = resp.usage.total_tokens if resp.usage else 0
    return text, tokens


def generate_reply_anthropic(bot: Bot, system_prompt: str, history: list[Message], new_user_text: str) -> tuple[str, int]:
    """Ответ через Claude."""
    import anthropic
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY не задан")
    client = anthropic.Anthropic(api_key=api_key)

    messages = to_provider_messages("anthropic", system_prompt, history, new_user_text)
    resp = client.messages.create(
        model=bot.llm_model or "claude-3-5-haiku-20241022",
        max_tokens=bot.max_tokens,
        temperature=bot.temperature,
        system=system_prompt,
        messages=messages,
    )
    text = resp.content[0].text.strip()
    tokens = (resp.usage.input_tokens + resp.usage.output_tokens) if resp.usage else 0
    return text, tokens


def generate_reply(bot: Bot, knowledge: list[KnowledgeChunk], history: list[Message], new_user_text: str) -> tuple[str, int]:
    """Главная точка входа — выбирает провайдер и возвращает ответ."""
    system_prompt = build_system_prompt(bot, knowledge)

    provider = bot.llm_provider.lower()
    if provider == "gigachat":
        return generate_reply_gigachat(bot, system_prompt, history, new_user_text)
    elif provider == "openai":
        return generate_reply_openai(bot, system_prompt, history, new_user_text)
    elif provider == "anthropic":
        return generate_reply_anthropic(bot, system_prompt, history, new_user_text)
    else:
        raise RuntimeError(f"Неизвестный провайдер: {provider}")
