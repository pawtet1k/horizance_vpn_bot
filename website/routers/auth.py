import hashlib
import hmac
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

import credentials
import settings
from db import UserManager
from request_helpers import get_client_ip, is_rate_limited
from utils import send_email
from my_logger import logger

_db = UserManager(db_name=str(Path("~/horizance_vpn_bot/users.db").expanduser().resolve()))

# Telegram требует отклонять данные виджета старше суток — защита от replay.
TELEGRAM_AUTH_MAX_AGE = 86400

router = APIRouter(tags=["auth"])


def _verify_telegram_auth(data: dict) -> bool:
    """Проверка подписи Telegram Login Widget.
    https://core.telegram.org/widgets/login#checking-authorization
    """
    received_hash = data.get("hash")
    if not received_hash:
        return False

    check_fields = {k: v for k, v in data.items() if k != "hash"}
    data_check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(check_fields.items())
    )
    secret_key = hashlib.sha256(credentials.BOT_TOKEN.encode()).digest()
    computed_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return False

    auth_date = int(data.get("auth_date", 0))
    return (time.time() - auth_date) <= TELEGRAM_AUTH_MAX_AGE


@router.post("/api/auth/telegram/callback")
async def telegram_auth_callback(request: Request):
    data = await request.json()

    if not _verify_telegram_auth(data):
        logger.warning(f"Telegram-логин: неверная подпись, data={data}")
        return JSONResponse({"ok": False}, status_code=401)

    user_id = int(data["id"])
    user = _db.get_user(user_id)
    is_new = user is None
    if is_new:
        user = _db.add_user(
            user_id=user_id,
            username=data.get("username"),
            first_name=data.get("first_name"),
            last_name=data.get("last_name"),
            phone=None,
            language=None,
            referrer_id=None,
        )
        logger.info(f"{user_id}: новый пользователь создан через Telegram Login Widget.")

    request.session["user_id"] = user_id
    logger.info(f"{user_id}: вход через Telegram Login Widget выполнен.")
    return JSONResponse({"ok": True, "is_new": is_new})


# Способы входа, ещё не реализованные бэкендом (соц-логины/пароль) — кнопка на
# /auth шлёт сюда fetch() (без ухода со страницы) и показывает модалку "скоро
# будет", а мы просто логируем интерес и уведомляем на почту. Аналитическое
# событие шлёт сама страница (auth.html), т.к. это обычный JS fetch без
# редиректа — сервер тут не нужен.
AUTH_STUB_PROVIDERS = {
    "yandex": "Яндекс ID",
    "google": "Google",
    "vk": "VK ID",
    "password": "логин и пароль",
}


@router.post("/api/auth/{provider}/click")
async def auth_stub_click(provider: str, request: Request):
    label = AUTH_STUB_PROVIDERS.get(provider)
    if label is None:
        return JSONResponse({"ok": False}, status_code=404)

    client_ip = get_client_ip(request)
    if is_rate_limited(f"auth_stub_click:{client_ip}", max_calls=5, window_seconds=600):
        logger.warning(f"auth_stub_click: превышен лимит запросов, ip={client_ip}")
        return JSONResponse({"ok": False}, status_code=429)

    logger.info(f"Клик по кнопке входа «{label}» (заглушка), ip={client_ip}")
    send_email(
        subject=f"Horizance VPN: клик по кнопке входа «{label}»",
        body=(
            f"Кто-то нажал кнопку входа «{label}» (функция ещё не реализована).\n"
            f"IP: {client_ip}\nUser-Agent: {request.headers.get('user-agent', '-')}"
        ),
        to=settings.NOTIFY_EMAIL,
    )
    return JSONResponse({"ok": True})


@router.get("/logout")
async def logout(request: Request):
    user_id = request.session.get("user_id")
    request.session.clear()
    logger.info(f"{user_id}: выход из личного кабинета.")
    return RedirectResponse(url="/")
