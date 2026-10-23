"""Общий Jinja2Templates для всех роутеров сайта — один инстанс вместо
по одному на файл, чтобы не дублировать настройку окружения.

`settings` доступен в любом шаблоне как глобальная переменная (например
`{{ settings.default_price }}` или `{{ settings.YANDEX_METRIKA_COUNTER_ID }}`),
без необходимости прокидывать его в context каждого TemplateResponse.
"""

from pathlib import Path

from fastapi.templating import Jinja2Templates
from markupsafe import Markup

import settings

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
CSS_DIR = STATIC_DIR / "css"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["settings"] = settings


def static_v(path: str) -> str:
    """`/static/img/logo.svg` -> `/static/img/logo.svg?v=<mtime>`.

    /static/ отдаётся с Cache-Control: max-age=31536000, immutable (app.py) —
    без этого браузер год не увидит новую версию картинки под тем же именем.
    Версия в query-параметре меняет URL при изменении файла, так что
    агрессивный кэш остаётся корректным.
    """
    file_path = STATIC_DIR / path.removeprefix("/static/")
    try:
        version = int(file_path.stat().st_mtime)
    except OSError:
        return path
    return f"{path}?v={version}"


templates.env.globals["static_v"] = static_v

# Страницы с английской версией на своём адресе (18.10.26, как в Watoto: свой URL на язык + hreflang) — русский путь → английский.
# Только эти попадают в поиск по-английски; остальные переводятся по cookie/языку браузера на том же адресе.
EN_PAGES = {"/": "/en/", "/trial": "/en/trial"}
RU_PAGES = {en: ru for ru, en in EN_PAGES.items()}


def lang_alternates(path: str) -> dict | None:
    """{'ru': '/', 'en': '/en/'} для страниц с двумя адресами — hreflang и переключатель языка; иначе None."""
    if path in EN_PAGES:
        return {"ru": path, "en": EN_PAGES[path]}
    if path in RU_PAGES:
        return {"ru": RU_PAGES[path], "en": path}
    return None


templates.env.globals["lang_alternates"] = lang_alternates


def inline_css() -> Markup:
    """Tailwind + Font Awesome сабсет, инлайнятся прямо в <head> одним <style>
    (см. base.html/faq.html) — вместо отдельных <link rel="stylesheet">.

    Оба файла в сумме ~7 КБ, так что лишний round-trip за внешним CSS обходится
    дороже, чем инлайнинг: убирает их из "render-blocking requests" и из
    цепочки критических запросов целиком. Читаются заново на каждый рендер
    (не кешируются в процессе), чтобы website/tailwind/build.sh сразу
    отражался без перезапуска сервиса.

    Результат оборачивается в Markup, иначе автоэкранирование Jinja2 превращает
    одинарные кавычки внутри CSS в &#39; — а содержимое <style> браузер разбирает
    как есть, без раскодирования HTML-сущностей. На практике это ломало ровно те
    объявления, где кавычки обязательны: @font-face (src: url('...')) и
    font-family: 'Font Awesome 7 Free', то есть все иконки Font Awesome на сайте.
    Экранировать здесь нечего: файлы собираются локально (website/tailwind/build.sh),
    пользовательских данных в них нет.
    """
    return Markup("".join((CSS_DIR / name).read_text() for name in ("tailwind.css", "fa-subset.css")))


templates.env.globals["inline_css"] = inline_css


# --- Язык страницы (решение владельца 10.10.26: иностранцу — английский) ---
# request.state.en ставит middleware site_language в app.py. Поисковым роботам всегда русский: сайт продвигается
# в русском поиске, и английская версия для Googlebot (US-IP, Accept-Language: en) убила бы позиции.
from jinja2 import pass_context  # noqa: E402


@pass_context
def t(ctx, ru: str, en: str) -> str:
    request = ctx.get("request")
    return en if request is not None and getattr(request.state, "en", False) else ru


@pass_context
def is_en(ctx) -> bool:
    request = ctx.get("request")
    return bool(request is not None and getattr(request.state, "en", False))


templates.env.globals["t"] = t
templates.env.globals["is_en"] = is_en


import whop  # noqa: E402
templates.env.globals["whop_enabled"] = whop.enabled
