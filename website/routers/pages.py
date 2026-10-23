import json
import os
import re
from pathlib import Path
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, RedirectResponse
from starlette.background import BackgroundTask

import credentials
import settings
from utils import generate_qrcode
from db import UserManager
from templates_env import templates

STATIC_DIR = Path(__file__).parent.parent / "static"
BLOG_ARTICLES_PATH = Path(__file__).parent.parent / "blog_articles.json"

_db = UserManager(db_name=str(Path("~/horizance_vpn_bot/users.db").expanduser().resolve()))

with open(BLOG_ARTICLES_PATH, encoding="utf-8") as f:
    ARTICLES = json.load(f)
ARTICLES_BY_SLUG = {a["slug"]: a for a in ARTICLES}

# Тот же диплинк, что уже используется в routers/vpn.py (support-url в заголовках подписки).
BOT_LINK = settings.site_bot_link("qr")

router = APIRouter(tags=["pages"])

@router.get("/")
async def index_page(request: Request):
    # иностранцу — короткая английская главная (index_en.html); роботам и русским — index.html (app.site_language)
    return templates.TemplateResponse(request, "index_en.html" if request.state.en else "index.html", {})


@router.get("/en/")
async def index_page_en(request: Request):
    # английская главная на своём адресе — для поиска (hreflang в base.html); app.site_language ставит state.en
    return templates.TemplateResponse(request, "index_en.html", {})


@router.get("/en")
async def index_page_en_slash():
    return RedirectResponse("/en/", status_code=301)

def safe_next(url: str | None) -> str:
    """Куда вернуть после входа (?next=). Только путь на нашем сайте:
    «//evil.com» и «/\\evil.com» браузер считает чужим доменом."""
    if url and url.startswith("/") and not url.startswith(("//", "/\\")):
        return url
    return "/dashboard"


@router.get("/auth")
async def auth_page(request: Request, next: str | None = None):
    next_url = safe_next(next)
    if request.session.get("user_id"):
        return RedirectResponse(url=next_url)
    telegram_bot_id = credentials.BOT_TOKEN.split(":", 1)[0]
    return templates.TemplateResponse(request, "auth.html", {
        "telegram_bot_id": telegram_bot_id, "next_url": next_url,
    })

@router.get("/dashboard")
async def dashboard_page(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse(url="/auth")
    user = _db.get_user(user_id)
    user_name = (user.first_name or user.username) if user else None
    return templates.TemplateResponse(request, "dashboard.html", {"user_name": user_name})

@router.get("/privacy")
async def privacy_page(request: Request):
    return templates.TemplateResponse(request, "privacy.html", {})

@router.get("/oferta")
async def oferta_page(request: Request):
    return templates.TemplateResponse(request, "oferta.html", {})

@router.get("/blog")
async def blog_page(request: Request):
    return templates.TemplateResponse(request, "blog.html", {"articles": ARTICLES})

def _related_articles(article, limit=3):
    """«Читать также»: сначала статьи, на которые ссылается текст, затем соседи по списку."""
    slugs = [s for s in ARTICLES_BY_SLUG
             if s != article["slug"] and f'/blog/{s}' in article["content_html"]]
    related = [ARTICLES_BY_SLUG[s] for s in slugs[:limit]]
    if len(related) < limit:
        idx = ARTICLES.index(article)
        for step in range(1, len(ARTICLES)):
            candidate = ARTICLES[(idx + step) % len(ARTICLES)]
            if candidate is not article and candidate not in related:
                related.append(candidate)
            if len(related) == limit:
                break
    return related


def _reading_time_min(article):
    words = len(re.sub(r"<[^>]+>", " ", article["content_html"]).split())
    return max(1, round(words / 170))


@router.get("/blog/{slug}")
async def blog_article_page(request: Request, slug: str):
    article = ARTICLES_BY_SLUG.get(slug)
    if not article:
        return RedirectResponse(url="/blog")
    return templates.TemplateResponse(request, "article.html", {
        "article": article,
        "related": _related_articles(article),
        "reading_time": _reading_time_min(article),
    })

# pay/config/invite_friend/help реализованы в routers/actions.py,
# которые переиспользуют bot.py/subscription.py через CapturingMessenger.

# Публичный QR на бота — переиспользует ту же utils.generate_qrcode,
# которой пользуется bot.py для реферальных QR-кодов.
@router.get("/qr_code")
async def qr_code_page():
    qr_file = generate_qrcode(BOT_LINK)
    return FileResponse(
        qr_file, media_type="image/png",
        background=BackgroundTask(os.remove, qr_file)
    )

@router.get("/favicon.ico")
async def favicon_ico():
    return FileResponse(STATIC_DIR / "img" / "favicon.ico", media_type="image/x-icon")

@router.get("/robots.txt")
async def robots_txt():
    return FileResponse(STATIC_DIR / "robots.txt", media_type="text/plain")

@router.get("/llms.txt")
async def llms_txt():
    return FileResponse(STATIC_DIR / "llms.txt", media_type="text/plain")

@router.get("/sitemap.xml")
async def sitemap_xml():
    return FileResponse(STATIC_DIR / "sitemap.xml", media_type="application/xml")

# Ключ-файл верификации IndexNow (Bing/Yandex) — та же схема, что у robots/llms/sitemap выше.
@router.get("/9fe4371d284b4a77becf9e0239bead33.txt")
async def indexnow_key():
    return FileResponse(STATIC_DIR / "9fe4371d284b4a77becf9e0239bead33.txt", media_type="text/plain")
