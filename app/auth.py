"""Простая авторизация по cookie. MVP."""
import os, hmac, hashlib, time
from fastapi import Request, Form, HTTPException
from fastapi.responses import RedirectResponse

# В MVP — захардкожено. Потом перенести в БД.
USERS = {
    "Test1": "123TesT1",
}

COOKIE_NAME = "rep_auth"
SECRET = os.getenv("REP_AUTH_SECRET", "replika-mvp-secret-change-in-prod-2026")

# Публичные пути — не требуют авторизации
PUBLIC_PATHS = ("/login", "/static", "/health", "/logout", "/favicon")


def _sign(username: str) -> str:
    """Подписываем имя пользователя HMAC — простая защита от подделки cookie."""
    msg = f"{username}|{int(time.time())}".encode()
    sig = hmac.new(SECRET.encode(), msg, hashlib.sha256).hexdigest()[:24]
    return f"{username}|{sig}"


def _verify(cookie: str) -> str | None:
    if not cookie or "|" not in cookie:
        return None
    try:
        username, sig = cookie.rsplit("|", 1)
        # Простая проверка — проверяем существование пользователя
        if username not in USERS:
            return None
        return username
    except Exception:
        return None


def get_current_user(request: Request) -> str | None:
    cookie = request.cookies.get(COOKIE_NAME)
    return _verify(cookie) if cookie else None


def is_public(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") or path.startswith(p) for p in PUBLIC_PATHS)


def check_credentials(username: str, password: str) -> bool:
    return USERS.get(username) == password
