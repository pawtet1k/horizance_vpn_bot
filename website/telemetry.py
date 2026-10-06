"""Обезличенная телеметрия сайта (Horizance, 06.10.2026).

Считает, что происходит у людей на фронте — просмотры страниц, нажатия на элементы
с data-t="имя", ошибки JS, таймауты ожидания, — БЕЗ cookie и БЕЗ идентификаторов:
в базу попадают только суммы за день (день, путь, событие, тип устройства → счётчик).
IP используется лишь в памяти для защиты от флуда и никуда не пишется. Поэтому это не
персональные данные и согласие на cookie для неё не нужно (в отличие от Метрики/Clarity).

Подключение в FastAPI-приложение:
    from telemetry import make_router
    app.include_router(make_router("/path/to/telemetry.db", site="vpn"))
и на страницы: <script src="/static/js/hz-telemetry.js" defer></script>

Отчёт:  python3 telemetry.py /path/to/telemetry.db [дней=7]
"""
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import Response

MSK = timezone(timedelta(hours=3))
EVENT_RE = re.compile(r"^[a-z0-9_.:-]{1,48}$")
MAX_EVENTS_PER_REQUEST = 30
RATE_LIMIT = (120, 60)  # не больше 120 запросов с IP за 60 с — дальше молча отбрасываем
# id в путях (/vpn/<uuid>, /blog/123) схлопываем, иначе каждая подписка — отдельная «страница»
_ID_PARTS = re.compile(r"/(?:[0-9a-f]{8}-[0-9a-f-]{27,}|t_[0-9a-f]{6,}|\d{3,})(?=/|$)", re.I)


def _norm_path(raw) -> str:
    path = str(raw or "/").split("?")[0].split("#")[0][:120]
    path = _ID_PARTS.sub("/:id", path)
    return re.sub(r"[^a-zA-Z0-9/_.:-]", "", path)[:80] or "/"


def _init(db_path):
    with sqlite3.connect(db_path) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS daily (
            day TEXT, site TEXT, path TEXT, event TEXT, device TEXT, n INTEGER,
            PRIMARY KEY (day, site, path, event, device))""")


def make_router(db_path: str, site: str) -> APIRouter:
    _init(db_path)
    router = APIRouter()
    hits: dict[str, list] = {}

    @router.post("/api/t", include_in_schema=False)
    async def track(request: Request):
        ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "-")
        now = time.monotonic()
        window = hits.setdefault(ip, [now, 0])
        if now - window[0] > RATE_LIMIT[1]:
            window[0], window[1] = now, 0
        window[1] += 1
        if len(hits) > 10000:
            hits.clear()
        if window[1] > RATE_LIMIT[0]:
            return Response(status_code=204)
        try:
            # sendBeacon шлёт text/plain — читаем тело сами, а не через request.json()
            data = json.loads((await request.body())[:8192] or b"{}")
            events = data.get("e") or []
        except (ValueError, AttributeError):
            return Response(status_code=204)
        ua = request.headers.get("user-agent", "")
        device = "m" if re.search(r"Mobi|Android|iPhone|iPad", ua) else "d"
        day = datetime.now(MSK).strftime("%Y-%m-%d")
        rows = []
        for item in events[:MAX_EVENTS_PER_REQUEST]:
            if not isinstance(item, list) or len(item) != 2:
                continue
            name, path = str(item[0]), _norm_path(item[1])
            if EVENT_RE.match(name):
                rows.append((day, site, path, name, device))
        if rows:
            with sqlite3.connect(db_path) as c:
                c.executemany(
                    "INSERT INTO daily VALUES (?, ?, ?, ?, ?, 1) "
                    "ON CONFLICT(day, site, path, event, device) DO UPDATE SET n = n + 1", rows)
        return Response(status_code=204)

    return router


def report(db_path: str, days: int = 7):
    since = (datetime.now(MSK) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    with sqlite3.connect(db_path) as c:
        print(f"События с {since}: событие | путь | моб/комп | всего")
        for ev, path, m, d, n in c.execute(
            "SELECT event, path, SUM(CASE WHEN device='m' THEN n END), SUM(CASE WHEN device='d' THEN n END), SUM(n) "
            "FROM daily WHERE day >= ? GROUP BY event, path ORDER BY event = 'pv', SUM(n) DESC", (since,)):
            print(f"{ev:28} {path:34} {m or 0:>6} {d or 0:>6} {n:>7}")


if __name__ == "__main__":
    report(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 7)
