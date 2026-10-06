import sys
from pathlib import Path
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

app = FastAPI()
app.add_middleware(
    SessionMiddleware,
    secret_key=credentials.SESSION_SECRET_KEY,
    session_cookie="hz_session",
    same_site="lax",
    https_only=True,
)


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
