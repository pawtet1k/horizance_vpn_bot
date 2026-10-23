"""Ежедневное обновление Notion-страницы «Новые пользователи и оплаты — по дням» (решение владельца 11.10.26).

Берёт вывод stats_growth.py (единственный источник цифр, сверен 10.10.26) по вчерашний день и:
  * дописывает в таблицу недостающие дни (уже стоящие строки не трогает);
  * обновляет дату в заголовке «Обновлено …» и в названии страницы;
  * переписывает один абзац с меткой «🤖 Автообновление» (итоги, удержание, конверсия) — ручные заметки остаются.

Ключ — credentials.NOTION_API_KEY; страницу нужно подключить к интеграции в Notion (••• → Connections).
Cron: /etc/cron.d или crontab пользователя, раз в сутки после полуночи МСК.

    .venv/bin/python3 notion_daily_stats.py          # обновить
    .venv/bin/python3 notion_daily_stats.py --dry    # только показать, что будет сделано
"""
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

import credentials
from stats_growth import FORECAST_UNPAID, MOVED_TO_ATTEMPT
from my_logger import logger

PAGE_ID = "3e70df4fe76181c8bf49eb4080c83ed6"
# Графики — в Google Таблице (sheets_daily_stats.py): на бесплатном Notion их не видно (владелец 24.10.26), базы «Дни»/«Недели» удалены.
API = "https://api.notion.com/v1"
HEADERS = {"Authorization": f"Bearer {credentials.NOTION_API_KEY}", "Notion-Version": "2022-06-28"}
MARK = "🤖 Автообновление"
MSK = timezone(timedelta(hours=3))
# Дни, где оплаты перенесены на день первой попытки (stats_growth.MOVED_TO_ATTEMPT, сбой UDM Pay 11–16.10.26): счёт и средние
# уже пересчитаны в stats_growth, здесь только «*» у числа — столбец 7 «Новые плательщики», 10 «Все оплаты».
STARRED = {}
for _uid, _real, _att, _new in MOVED_TO_ATTEMPT:
    for _d in (_real, _att):
        STARRED.setdefault(f"{_d:%d.%m}", set()).update({7, 10} if _new else {10})
for _uid, _att in FORECAST_UNPAID:  # не оплатили, засчитаны наперёд
    STARRED.setdefault(f"{_att:%d.%m}", set()).update({7, 10})
ROOT = Path(__file__).resolve().parent


def stats(end):
    out = subprocess.run([sys.executable, str(ROOT / "stats_growth.py"), "--to", end.isoformat()],
                         cwd=ROOT, capture_output=True, text=True, check=True).stdout
    rows = [[c.strip() for c in line.strip("|").split("|")]
            for line in out.splitlines() if re.match(r"^\| \d\d\.\d\d \|", line)]
    summary = [line.strip() for line in out.splitlines()
               if line.startswith(("Итого", "Продление", "Активные", "Конверсия"))]
    return rows, summary


def text(content):
    return [{"type": "text", "text": {"content": content}}]


def children(client, block_id):
    r = client.get(f"{API}/blocks/{block_id}/children", params={"page_size": 100})
    r.raise_for_status()
    return r.json()["results"]


def plain(rich):
    return "".join(t.get("plain_text", "") for t in rich)


def _num(cell):
    try:
        return float(cell.rstrip("*"))
    except ValueError:
        return None  # «—»: скользящей средней ещё нет


def _cohorts(today):
    """Неделя прихода в бот (понедельник) → (пришли, оплатили, когорта закрыта). Владелец не считается."""
    import sqlite3
    c = sqlite3.connect(f"file:{ROOT / 'users.db'}?mode=ro", uri=True)
    out = {}
    for user_id, start, paid in c.execute("SELECT user_id, start_date, paid_periods FROM users"):
        if user_id == credentials_owner():
            continue
        d = datetime.strptime(start, "%d.%m.%y").date()
        if d >= today:
            continue  # как и остальные числа — по вчерашний день
        monday = d - timedelta(days=d.weekday())
        came, payers = out.get(monday, (0, 0))
        out[monday] = (came + 1, payers + (1 if paid else 0))
    trial = 14  # бесплатные дни в боте (settings.trial_days)
    return {m: (came, payers, m + timedelta(days=6 + trial) < today) for m, (came, payers) in out.items()}


def credentials_owner():
    import settings
    return settings.OWNER_TELEGRAM_ID


def day_rows(rows, end):
    """Строки stats_growth → [(дата, строка)]; в таблице только «дд.мм», год берём из конца периода."""
    return [(datetime.strptime(f"{row[0]}.{end.year}", "%d.%m.%Y").date(), row) for row in rows]


def week_rows(rows, end):
    """Недели пн–вс из дневных строк + конверсия когорты прихода в бот: [(понедельник, {столбец: значение})]
    для листа «Недели» в Google Таблице (sheets_daily_stats.py)."""
    weeks = {}
    for d, row in day_rows(rows, end):
        monday = d - timedelta(days=d.weekday())
        w = weeks.setdefault(monday, [0, 0, 0, 0, 0])
        for i, col in enumerate((1, 4, 7, 10)):
            w[i] += _num(row[col]) or 0
        w[4] += 1
    cohorts = _cohorts(end + timedelta(days=1))
    out = []
    for monday, (bot, trials, new, paid, n_days) in sorted(weeks.items()):
        came, payers, closed = cohorts.get(monday, (0, 0, False))
        out.append((monday, {
            "Пришли в бот": bot, "Триалы сайта": trials, "Новые плательщики": new,
            "Все оплаты": paid, "Конверсия, %": round(100 * payers / came, 1) if came else None,
            "Когорта закрыта": closed, "Неделя полная": n_days == 7,
        }))
    return out


def main(dry=False):
    end = datetime.now(MSK).date() - timedelta(days=1)
    rows, summary = stats(end)
    with httpx.Client(headers=HEADERS, timeout=30) as client:
        blocks = children(client, PAGE_ID)
        table = next(b for b in blocks if b["type"] == "table")
        existing = set()
        cursor = None
        while True:  # в таблице больше 100 строк — читаем постранично
            params = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
            r = client.get(f"{API}/blocks/{table['id']}/children", params=params)
            r.raise_for_status()
            data = r.json()
            existing |= {plain(b["table_row"]["cells"][0]) for b in data["results"]}
            if not data.get("has_more"):
                break
            cursor = data["next_cursor"]
        new = [row for row in rows if row[0] not in existing]
        for row in new:
            for col in STARRED.get(row[0], ()):
                row[col] += "*"
        updated = f"Обновлено {datetime.now(MSK):%d.%m.%Y} (данные по {end:%d.%m}) — скрипт `stats_growth.py`"
        auto = f"{MARK} {datetime.now(MSK):%d.%m %H:%M}: " + " · ".join(summary)
        print(f"новых строк: {[r[0] for r in new]}\n{updated}\n{auto}")
        if dry:
            return
        if new:
            r = client.patch(f"{API}/blocks/{table['id']}/children", json={"children": [
                {"type": "table_row", "table_row": {"cells": [text(c) for c in row]}} for row in new]})
            r.raise_for_status()
        heading = next((b for b in blocks if b["type"] == "heading_2"
                        and plain(b["heading_2"]["rich_text"]).startswith("Обновлено")), None)
        if heading:
            client.patch(f"{API}/blocks/{heading['id']}", json={"heading_2": {"rich_text": text(updated)}}).raise_for_status()
        mark = next((b for b in blocks if b["type"] == "paragraph"
                     and plain(b["paragraph"]["rich_text"]).startswith(MARK)), None)
        if mark:
            client.patch(f"{API}/blocks/{mark['id']}", json={"paragraph": {"rich_text": text(auto)}}).raise_for_status()
        else:  # первый запуск: абзац сразу после таблицы
            client.patch(f"{API}/blocks/{PAGE_ID}/children", json={
                "after": table["id"], "children": [{"type": "paragraph", "paragraph": {"rich_text": text(auto)}}]
            }).raise_for_status()
        client.patch(f"{API}/pages/{PAGE_ID}", json={"properties": {"title": {"title": text(
            f"Новые пользователи и оплаты — по дням (15.08–{end:%d.%m.%Y})")}}}).raise_for_status()
    logger.info(f"Notion: таблица статистики обновлена, добавлено строк {len(new)}")


if __name__ == "__main__":
    try:
        main(dry="--dry" in sys.argv)
    except Exception as e:
        logger.error(f"Notion: обновление таблицы статистики не удалось: {e}")
        raise
