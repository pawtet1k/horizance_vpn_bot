"""Страница «что работает сейчас» (/status).

Ценность страницы — исключительно в свежести данных, поэтому JSON читается
на КАЖДЫЙ запрос, а не при импорте модуля (как blog_articles.json в
routers/pages.py). Тот же приём уже используется в templates_env.inline_css():
файл перечитывается при каждом рендере, чтобы правка отражалась без
перезапуска сервиса. Здесь это принципиально: обновление статуса должно
стоить правки одного файла, иначе страница перестанет обновляться.

Данные ведутся вручную (website/status_data.json) — никакого автоматического
мониторинга за ними не стоит, и страница прямо об этом говорит.
"""

import json
from pathlib import Path

from fastapi import APIRouter, Request

from templates_env import templates

STATUS_DATA_PATH = Path(__file__).parent.parent / "status_data.json"

router = APIRouter(tags=["status"])


@router.get("/status")
async def status_page(request: Request):
    with open(STATUS_DATA_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return templates.TemplateResponse(request, "status.html", {"data": data})
