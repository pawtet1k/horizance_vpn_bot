"""Счётчик переходов для воронки: /go/app/<приложение> и /go/bot.

Зачем: скачивание приложения и переход в Telegram-бота — внешние ссылки, их
не видно ни в логах nginx, ни в наших таблицах, а Метрика/GA4 видят только
тех, кто принял cookie. Ссылки на сайте ведут сюда: переход пишется в лог
(«go: …») и в access-лог nginx (его считает site_stats_daily.py), затем
сразу редирект туда, куда человек и шёл. src — страница, с которой ушли.
"""
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

import settings
from my_logger import logger
from request_helpers import get_client_ip

router = APIRouter(tags=["go"])

BOT_LINK = "https://t.me/Horizance_vpn_bot?start=183640993"


def _app_url(name: str) -> str | None:
    url = settings.VPN_CLIENT_DOWNLOAD_URLS.get(name)
    if url:
        return url
    for client in settings.IOS_ALTERNATIVE_CLIENTS:
        if client["name"].lower() == name.lower():
            return client["url"]
    return None


def app_link(name: str, src: str) -> str:
    """Ссылка «скачать приложение» через счётчик — для шаблонов и роутеров."""
    return f"/go/app/{name}?src={src}"


@router.get("/go/app/{name}")
async def go_app(name: str, request: Request, src: str = "-"):
    url = _app_url(name)
    logger.info(f"go: app={name} src={src[:40]} ip={get_client_ip(request)}")
    return RedirectResponse(url or "/", status_code=302)


@router.get("/go/bot")
async def go_bot(request: Request, src: str = "-"):
    logger.info(f"go: bot src={src[:40]} ip={get_client_ip(request)}")
    return RedirectResponse(BOT_LINK, status_code=302)
