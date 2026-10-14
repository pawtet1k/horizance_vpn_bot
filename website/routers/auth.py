import hashlib
import hmac
import io
import re
import secrets
import time
from pathlib import Path

import httpx
import qrcode
import qrcode.image.svg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

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


def _sign_in(request: Request, user_id: int, username, first_name, last_name, how: str) -> bool:
    """Сессия личного кабинета; первого входа ждёт новый аккаунт. True — аккаунт только что создан."""
    is_new = _db.get_user(user_id) is None
    if is_new:
        _db.add_user(
            user_id=user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            phone=None,
            language=None,
            referrer_id=None,
        )
        logger.info(f"{user_id}: новый пользователь создан через {how}.")
    request.session["user_id"] = user_id
    logger.info(f"{user_id}: вход через {how} выполнен.")
    return is_new


@router.post("/api/auth/telegram/callback")
async def telegram_auth_callback(request: Request):
    data = await request.json()

    if not _verify_telegram_auth(data):
        logger.warning(f"Telegram-логин: неверная подпись, data={data}")
        return JSONResponse({"ok": False}, status_code=401)

    is_new = _sign_in(request, int(data["id"]), data.get("username"), data.get("first_name"),
                      data.get("last_name"), "Telegram Login Widget")
    return JSONResponse({"ok": True, "is_new": is_new})


# ─── Вход через бота: QR-код на компьютере, кнопка «Открыть Telegram» на телефоне (14.10.26) ───
# Токен лежит в сессии браузера, который его получил: статус и QR отдаются только ему. Кто отсканировал
# QR, тот токен знает, но войти вместо этого браузера не может — сессия не его. Подмену QR (вам показывают
# чужой код) закрывает подтверждение в боте с устройством и городом входа.

def _device_label(ua: str) -> str:
    os_name = next((name for pattern, name in (
        (r"iPhone", "iPhone"), (r"iPad", "iPad"), (r"Android", "Android"), (r"Windows", "Windows"),
        (r"Mac OS X|Macintosh", "Mac"), (r"CrOS", "Chromebook"), (r"Linux", "Linux"),
    ) if re.search(pattern, ua)), "")
    browser = next((name for pattern, name in (
        (r"YaBrowser", "Яндекс Браузер"), (r"Edg/", "Edge"), (r"OPR/|Opera", "Opera"), (r"Firefox|FxiOS", "Firefox"),
        (r"Chrome|CriOS", "Chrome"), (r"Safari", "Safari"),
    ) if re.search(pattern, ua)), "")
    return ", ".join(x for x in (os_name, browser) if x) or "неизвестное устройство"


async def _ip_place(ip: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            r = await client.get(f"http://ip-api.com/json/{ip}?fields=status,country,city&lang=ru")
        data = r.json()
    except Exception:
        return ""
    if data.get("status") != "success":
        return ""
    return ", ".join(x for x in (data.get("city"), data.get("country")) if x)


def _session_login(request: Request) -> dict | None:
    token = request.session.get("bot_login")
    return _db.get_web_login(token) if token else None


@router.post("/api/auth/bot/new")
async def bot_login_new(request: Request):
    ip = get_client_ip(request)
    if is_rate_limited(f"bot_login_new:{ip}", max_calls=10, window_seconds=60):
        return JSONResponse({"ok": False}, status_code=429)
    token = secrets.token_urlsafe(16)  # 128 бит, 22 символа [A-Za-z0-9_-] — влезает в start-параметр
    _db.add_web_login(token, ip, _device_label(request.headers.get("user-agent", "")), await _ip_place(ip))
    request.session["bot_login"] = token
    logger.info(f"Вход через бота: выдан токен {token[:6]}…, ip={ip}")
    return JSONResponse({"ok": True, "link": settings.web_login_link(token), "ttl": settings.WEB_LOGIN_TTL})


@router.get("/api/auth/bot/qr.svg")
async def bot_login_qr(request: Request):
    login = _session_login(request)
    if login is None:
        return Response(status_code=404)
    img = qrcode.make(settings.web_login_link(login["token"]), image_factory=qrcode.image.svg.SvgPathImage, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


@router.get("/api/auth/bot/status")
async def bot_login_status(request: Request):
    login = _session_login(request)
    if login is None:
        return JSONResponse({"status": "expired"})
    age = time.time() - login["created_at"]
    if login["status"] == "approved":
        if age > settings.WEB_LOGIN_TTL + settings.WEB_LOGIN_APPROVED_TTL or not _db.update_web_login(
                login["token"], "approved", status="used"):
            return JSONResponse({"status": "expired"})
        request.session.pop("bot_login", None)
        is_new = _sign_in(request, login["user_id"], login["username"], login["first_name"],
                          login["last_name"], "бота (QR/ссылка)")
        return JSONResponse({"status": "ok", "is_new": is_new})
    if login["status"] == "denied":
        return JSONResponse({"status": "denied"})
    # QR сгорает через WEB_LOGIN_TTL, но кто уже открыл бота, тому даём время нажать «Войти»
    if (login["status"] == "pending" and age <= settings.WEB_LOGIN_TTL
            or login["status"] == "opened" and age <= settings.WEB_LOGIN_TTL + settings.WEB_LOGIN_APPROVED_TTL):
        return JSONResponse({"status": login["status"]})
    return JSONResponse({"status": "expired"})


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
