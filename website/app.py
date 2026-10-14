import re
import sys
from pathlib import Path

import httpx
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # horizance_vpn_bot root

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from routers import vpn, trial, faq, pages, auth, actions, payments, happ, status, go
from telemetry import make_router as make_telemetry_router

import credentials
from error_alerts import notify_error
from my_logger import logger
from request_helpers import get_client_ip

app = FastAPI()
app.add_middleware(
    SessionMiddleware,
    secret_key=credentials.SESSION_SECRET_KEY,
    session_cookie="hz_session",
    same_site="lax",
    https_only=True,
)


_BOT_UA = re.compile(r"bot|crawl|spider|slurp|yandex|google|bing|lighthouse|preview", re.I)
_RU_LANGS = {"ru", "uk", "be", "kk"}
_country_cache: dict[str, str | None] = {}


async def _ip_country(ip: str) -> str | None:
    if ip not in _country_cache:
        try:
            async with httpx.AsyncClient(timeout=1.5) as client:
                r = await client.get(f"http://ip-api.com/json/{ip}?fields=countryCode")
            _country_cache[ip] = r.json().get("countryCode")
        except Exception:
            return None
    return _country_cache[ip]


@app.middleware("http")
async def site_language(request: Request, call_next):
    """request.state.en — показывать ли страницу по-английски (templates_env.t). Правило как у бота (i18n.py):
    язык браузера не русский и IP не российский. ?lang=en|ru переключает вручную и запоминается в cookie."""
    request.state.en = False
    forced = request.query_params.get("lang") or request.cookies.get("hz_lang")
    path = request.url.path
    if forced in ("en", "ru"):
        request.state.en = forced == "en"
    elif (request.method == "GET" and not path.startswith(("/static", "/api/payments", "/api/t", "/go"))
          and not _BOT_UA.search(request.headers.get("user-agent", ""))):
        accept = request.headers.get("accept-language", "")
        primary = accept.split(",")[0].split(";")[0].strip().lower().split("-")[0]
        if primary and primary not in _RU_LANGS:
            request.state.en = (await _ip_country(get_client_ip(request))) != "RU"
    response = await call_next(request)
    if request.query_params.get("lang") in ("en", "ru"):
        response.set_cookie("hz_lang", request.query_params["lang"], max_age=365 * 86400, samesite="lax", secure=True)
    return response


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"
    # Только Trusted Types — не полноценный CSP: script-src с nonce сломал бы
    # ~10 шаблонов с inline <script> (аналитика, cookie-баннер, canvas hero и
    # т.д.) без возможности проверить это в реальном браузере отсюда.
    # require-trusted-types-for безопасен сам по себе: единственный innerHTML
    # в коде (dashboard.html) уже переписан на createElement/textContent.
    response.headers["Content-Security-Policy"] = "require-trusted-types-for 'script'"
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
app.include_router(pages.router)
app.include_router(vpn.router)
app.include_router(trial.router)
app.include_router(faq.router)
app.include_router(happ.router)
app.include_router(status.router)
app.include_router(auth.router)
app.include_router(actions.router)
app.include_router(payments.router)
app.include_router(go.router)
# обезличенная телеметрия фронта (hz_web_kit, 06.10.26): суммы за день, без cookie и идентификаторов
app.include_router(make_telemetry_router(
    str(Path(__file__).resolve().parent.parent / "my_logger" / "stats" / "telemetry.db"), site="vpn"))


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.error(f"Необработанное исключение: {request.method} {request.url.path}", exc_info=True)
    notify_error(request, exc)
    return JSONResponse({"detail": "Internal Server Error"}, status_code=500)
