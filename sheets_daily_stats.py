"""Ночное обновление Google Таблицы «Horizance VPN — метрики по дням» — копия статистики со страницы Notion.

    .venv/bin/python3 sheets_daily_stats.py          # cron 00:55, после notion_daily_stats.py

Графики в Notion недоступны на бесплатном тарифе (владелец 24.10.26), поэтому графики — здесь. Скрипт каждый раз:
  * переписывает листы «Дни» и «Недели» целиком (те же числа, что в Notion: stats_growth.py + notion_daily_stats);
  * «*» — дни, где оплаты перенесены на первую попытку или засчитаны наперёд (сбой UDM Pay 11–16.10.26): числа в
    ячейках остаются числами (иначе графики их не видят), «*» и причина — в последнем столбце, ячейки подсвечены;
  * пересоздаёт графики на этих листах с точным диапазоном строк;
  * переписывает лист «Как считается» (определения, итоги автообновления, список «*»).
Доступ — сервисный аккаунт googlesheets@driven-realm-431222-h1 (у него права редактора на таблицу).
"""
import sqlite3
import sys
from datetime import datetime, timedelta

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import service_account

from my_logger import logger
from notion_daily_stats import MSK, ROOT, STARRED, _num, day_rows, stats, week_rows
from stats_growth import FORECAST_UNPAID, MOVED_TO_ATTEMPT

SPREADSHEET_ID = "1QNeuAOs3SfFVQt9W-0VTRV8MP_LipiNASbBQh8W_klE"
KEY_FILE = "/home/pashavpsadm/driven-realm-431222-h1-89dc926e24ee.json"
API = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}"
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
DAYS, WEEKS, NOTES = "Дни", "Недели", "Как считается"
DAY_HEADER = ["Дата", "День", "Бот", "Бот ср3", "Бот ср7", "Триалы", "Триалы ср3", "Триалы ср7",
              "Новые плательщики", "Новые ср3", "Новые ср7", "Все оплаты", "*"]
WEEK_HEADER = ["Неделя (пн)", "Пришли в бот", "Триалы сайта", "Новые плательщики", "Все оплаты",
               "Конверсия в оплату, %", "Когорта закрыта", "Неделя полная", "*"]
STAR_FILL = {"red": 1, "green": 0.95, "blue": 0.8}


def _client():
    creds = service_account.Credentials.from_service_account_file(
        KEY_FILE, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    creds.refresh(Request())
    return httpx.Client(headers={"Authorization": f"Bearer {creds.token}"}, timeout=60)


def _star_notes():
    """Дата «дд.мм» → причина «*» по каждому человеку (те же списки, что двигают числа в stats_growth)."""
    c = sqlite3.connect(f"file:{ROOT / 'users.db'}?mode=ro", uri=True)
    name = lambda uid: (c.execute("SELECT username FROM users WHERE user_id = ?", (uid,)).fetchone() or [None])[0] or str(uid)
    notes = {}
    for uid, real, attempt, new in MOVED_TO_ATTEMPT:
        kind = "новый" if new else "продление"
        notes.setdefault(f"{attempt:%d.%m}", []).append(f"{name(uid)} ({kind}) оплатил {real:%d.%m} — отнесено к первой попытке")
        notes.setdefault(f"{real:%d.%m}", []).append(f"{name(uid)}: оплата {real:%d.%m} вычтена, стоит на {attempt:%d.%m}")
    for uid, attempt in FORECAST_UNPAID:
        notes.setdefault(f"{attempt:%d.%m}", []).append(f"{name(uid)} (новый): не оплатил после сбоя — прогноз, засчитан наперёд")
    return notes


def _sheet_ids(client):
    meta = client.get(API, params={"fields": "sheets(properties(sheetId,title),charts(chartId))"})
    meta.raise_for_status()
    return {s["properties"]["title"]: (s["properties"]["sheetId"], [ch["chartId"] for ch in s.get("charts", [])])
            for s in meta.json()["sheets"]}


def _rng(sheet_id, col, n_rows):
    return {"sourceRange": {"sources": [{"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": n_rows + 1,
                                         "startColumnIndex": col, "endColumnIndex": col + 1}]}}


def _chart(sheet_id, title, n_rows, series, row, col, chart_type="COMBO", subtitle=None):
    """series: [(столбец, COLUMN|LINE)] по оси X — столбец 0 (дата / неделя)."""
    return {"addChart": {"chart": {
        "spec": {"title": title, **({"subtitle": subtitle} if subtitle else {}),
                 "basicChart": {"chartType": chart_type, "legendPosition": "BOTTOM_LEGEND", "headerCount": 1,
                                "domains": [{"domain": _rng(sheet_id, 0, n_rows)}],
                                "series": [{"series": _rng(sheet_id, c, n_rows), "type": t, "targetAxis": "LEFT_AXIS"}
                                           for c, t in series]}},
        "position": {"overlayPosition": {"anchorCell": {"sheetId": sheet_id, "rowIndex": row, "columnIndex": col},
                                         "widthPixels": 720, "heightPixels": 320}}}}}


def main():
    end = datetime.now(MSK).date() - timedelta(days=1)
    rows, summary = stats(end)
    notes = _star_notes()

    days = []
    for d, row in day_rows(rows, end):
        star = "; ".join(notes.get(row[0], [])) if row[0] in STARRED else ""
        days.append([row[0], WEEKDAYS[d.weekday()]] + [_num(row[i]) for i in range(1, 11)] + [star])
    days = [[("" if v is None else v) for v in r] for r in days]

    weeks = []
    for monday, w in week_rows(rows, end):
        week_days = {f"{monday + timedelta(i):%d.%m}" for i in range(7)}
        star = "есть «*» в днях: " + ", ".join(sorted(week_days & set(STARRED))) if week_days & set(STARRED) else ""
        weeks.append([f"{monday:%d.%m}–{monday + timedelta(6):%d.%m}", w["Пришли в бот"], w["Триалы сайта"],
                      w["Новые плательщики"], w["Все оплаты"],
                      # конверсию рисуем только у закрытых когорт: у остальных 14 бесплатных дней ещё не прошли
                      w["Конверсия, %"] if w["Когорта закрыта"] and w["Конверсия, %"] is not None else "",
                      "да" if w["Когорта закрыта"] else "нет", "да" if w["Неделя полная"] else "нет", star])

    starred_days = sorted(STARRED, key=lambda x: (x[3:], x[:2]))
    notes_sheet = [
        [f"Обновлено {datetime.now(MSK):%d.%m.%Y %H:%M} МСК (данные по {end:%d.%m}) — sheets_daily_stats.py, те же числа, что в Notion"],
        [""],
        ["Бот — новые аккаунты (users.start_date): /start в боте и вход на сайте через Telegram."],
        ["Триалы — пробные доступы с сайта (день по Москве)."],
        ["Новые плательщики — платит впервые в жизни. Повторные оплаты и продления сюда не входят; кто платил переводом на карту до 13.08, «новым» не считается."],
        ["Все оплаты — все оплаты по дате платежа (UDM Pay + NOWPayments)."],
        ["ср3 / ср7 — скользящие средние за 3 и 7 дней."],
        ["Недели — пн–вс. Конверсия — % оплативших среди пришедших в бот за неделю, только недели, где у всех прошли 14 бесплатных дней."],
        [""],
        ["* Сбой UDM Pay 11–16.10: оплаты отнесены к дню первой попытки; трое, кто упёрся в сбой и не оплатил, засчитаны наперёд на день попытки (прогноз, не деньги)."],
        *[[f"{d}: " + "; ".join(notes.get(d, []))] for d in starred_days],
        [""],
        ["Итоги (автообновление):"],
        *[[line] for line in summary],
    ]

    with _client() as client:
        sheets = _sheet_ids(client)
        if NOTES not in sheets:
            client.post(f"{API}:batchUpdate", json={"requests": [{"addSheet": {"properties": {"title": NOTES}}}]}
                        ).raise_for_status()
            sheets = _sheet_ids(client)
        for title in (DAYS, WEEKS, NOTES):
            client.post(f"{API}/values/{title}!A1:Z1000:clear").raise_for_status()
        client.post(f"{API}/values:batchUpdate", json={"valueInputOption": "RAW", "data": [
            {"range": f"{DAYS}!A1", "values": [DAY_HEADER] + days},
            {"range": f"{WEEKS}!A1", "values": [WEEK_HEADER] + weeks},
            {"range": f"{NOTES}!A1", "values": notes_sheet},
        ]}).raise_for_status()

        day_id, day_charts = sheets[DAYS]
        week_id, week_charts = sheets[WEEKS]
        n_days, n_weeks = len(days), len(weeks)
        requests = [{"deleteEmbeddedObject": {"objectId": cid}} for cid in day_charts + week_charts]
        requests.append({"updateSpreadsheetProperties": {"properties": {
            "title": f"Horizance VPN — метрики по дням (15.08–{end:%d.%m.%Y})"}, "fields": "title"}})
        # подсветка: сначала снять старую, потом закрасить строки со «*»
        for sid, ncols, data in ((day_id, len(DAY_HEADER), days), (week_id, len(WEEK_HEADER), weeks)):
            requests.append({"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": 1000,
                                                      "startColumnIndex": 0, "endColumnIndex": ncols},
                                            "cell": {"userEnteredFormat": {}}, "fields": "userEnteredFormat"}})
            for i, r in enumerate(data, start=1):
                if r[-1]:
                    requests.append({"repeatCell": {"range": {"sheetId": sid, "startRowIndex": i, "endRowIndex": i + 1,
                                                              "startColumnIndex": 0, "endColumnIndex": ncols},
                                                    "cell": {"userEnteredFormat": {"backgroundColor": STAR_FILL}},
                                                    "fields": "userEnteredFormat.backgroundColor"}})
        star_note = "Жёлтые строки — «*»: сбой UDM Pay 11–16.10 (перенос на первую попытку / прогноз), см. столбец «*»"
        x = len(DAY_HEADER) + 1  # графики справа от таблицы
        requests += [
            _chart(day_id, "Новые в боте: за день и ср. 7 дней", n_days, [(2, "COLUMN"), (4, "LINE")], 0, x),
            _chart(day_id, "Триалы сайта: за день и ср. 7 дней", n_days, [(5, "COLUMN"), (7, "LINE")], 17, x),
            _chart(day_id, "Новые плательщики: за день и ср. 7 дней", n_days, [(8, "COLUMN"), (10, "LINE")], 34, x,
                   subtitle=star_note),
            _chart(day_id, "Все оплаты за день", n_days, [(11, "COLUMN")], 51, x, subtitle=star_note),
        ]
        x = len(WEEK_HEADER) + 1
        requests += [
            _chart(week_id, "Пришли в бот по неделям", n_weeks, [(1, "COLUMN")], 0, x,
                   subtitle="Последняя неделя может быть неполной (столбец «Неделя полная»)"),
            _chart(week_id, "Все оплаты по неделям", n_weeks, [(4, "COLUMN")], 17, x, subtitle=star_note),
            _chart(week_id, "Новые плательщики по неделям", n_weeks, [(3, "COLUMN")], 34, x, subtitle=star_note),
            _chart(week_id, "Конверсия в оплату, % (закрытые когорты)", n_weeks, [(5, "COLUMN")], 51, x),
        ]
        client.post(f"{API}:batchUpdate", json={"requests": requests}).raise_for_status()
    print(f"Google Таблица обновлена: дней {n_days}, недель {n_weeks}, данные по {end:%d.%m}")
    logger.info(f"Google Таблица статистики обновлена: дней {n_days}, недель {n_weeks}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.error(f"Google Таблица статистики: обновление не удалось: {e}")
        raise
