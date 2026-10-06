"""Серверная отправка событий в счётчики аналитики (то, что не может
подтвердить браузер — например обычный POST формы, сразу редиректящий
дальше, без единого момента с JS-счётчиком на странице).

Яндекс.Метрика и GA4 отправляются через их Measurement Protocol.
send_goal() — единая точка входа, шлёт в оба счётчика параллельно.
"""

import asyncio
import time

import httpx
from fastapi import Request

import credentials
import settings
from my_logger import logger


async def send_metrika_goal(request: Request, goal: str) -> None:
    """Засчитывает цель в Яндекс.Метрике через Measurement Protocol.
    Требует токен из настроек счётчика (Данные и безопасность → Measurement
    Protocol), сохранённый в credentials.py как YANDEX_METRIKA_MP_TOKEN.
    """
    ym_uid = request.cookies.get("_ym_uid")
    token = getattr(credentials, "YANDEX_METRIKA_MP_TOKEN", None)
    if not ym_uid or not token:
        logger.warning(f"Цель '{goal}' не отправлена в Метрику: нет _ym_uid или YANDEX_METRIKA_MP_TOKEN")
        return
    try:
        async with httpx.AsyncClient() as client:
            await client.get(
                "https://mc.yandex.ru/collect/",
                params={
                    "tid": settings.YANDEX_METRIKA_COUNTER_ID,
                    "cid": ym_uid,
                    "t": "event",
                    "ea": goal,
                    "dl": str(request.url),
                    "et": int(time.time()),
                    "ms": token,
                },
            )
        logger.info(f"Цель '{goal}' отправлена в Метрику (cid={ym_uid})")
    except Exception as e:
        logger.warning(f"Не удалось отправить цель '{goal}' в Метрику: {e}")


async def send_ga4_event(request: Request, event_name: str, params: dict | None = None) -> None:
    """Засчитывает событие в GA4 через Measurement Protocol.
    Требует client_id из браузерной _ga cookie (формат GA1.1.<client_id>...)
    и API secret, сохранённый в credentials.py как GA4_API_SECRET.
    """
    ga_cookie = request.cookies.get("_ga")
    api_secret = getattr(credentials, "GA4_API_SECRET", None)
    if not ga_cookie or not api_secret:
        logger.warning(f"Событие '{event_name}' не отправлено в GA4: нет cookie _ga или GA4_API_SECRET")
        return
    parts = ga_cookie.split(".")
    client_id = ".".join(parts[-2:]) if len(parts) >= 2 else ga_cookie
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                "https://www.google-analytics.com/mp/collect",
                params={
                    "measurement_id": settings.GA4_MEASUREMENT_ID,
                    "api_secret": api_secret,
                },
                json={
                    "client_id": client_id,
                    "events": [{"name": event_name, "params": params or {}}],
                },
            )
        logger.info(f"Событие '{event_name}' отправлено в GA4 (client_id={client_id})")
    except Exception as e:
        logger.warning(f"Не удалось отправить событие '{event_name}' в GA4: {e}")


async def send_goal(request: Request, goal: str, params: dict | None = None) -> None:
    """Засчитывает цель одновременно в Яндекс.Метрике и GA4."""
    await asyncio.gather(
        send_metrika_goal(request, goal),
        send_ga4_event(request, goal, params),
    )
