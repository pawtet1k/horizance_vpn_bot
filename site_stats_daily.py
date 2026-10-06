#!/usr/bin/env python3
"""Суточные итоги посещаемости сайтов из логов nginx — хранятся ВСЕГДА.

Зачем: сами логи nginx живут ограниченное время (logrotate), а общая
статистика посещаемости нужна за всё время. Скрипт читает все доступные
логи (текущий, ротированные, .gz, старый архив access.log.oldformat),
считает итоги по каждой паре (дата, сайт) и сливает их в
my_logger/stats/daily_visits.csv. Строки за даты, которых в логах уже нет,
не трогаются — поэтому CSV только растёт. Запуск: cron раз в сутки
(/etc/cron.d/horizance-site-stats, от root — логи nginx читает только adm).

Столбцы:
  date, host         — день (по времени в логе) и сайт (для старого формата "-")
  requests           — все запросы
  visitors           — уникальные IP+User-Agent без ботов (как «гости» GoAccess)
  pageviews          — GET со статусом 200/304 к страницам (не статика, не /sub/, не /api/)
  trial_issued       — POST /trial/connect → 303 (триал выдан или повторный клик)
  sub_requests       — GET /sub/… (приложения обновляют подписку)
  bot_link_clicks    — переходы в Telegram-бота с сайта (/go/bot, с 26.09.2026)
  app_clicks         — клики «скачать приложение» (/go/app/…, с 26.09.2026)
"""
import csv
import glob
import gzip
import os
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

LOG_GLOB = ["/var/log/nginx/access.log", "/var/log/nginx/access.log.*"]
OUT = Path(__file__).resolve().parent / "my_logger" / "stats" / "daily_visits.csv"
FIELDS = ["date", "host", "requests", "visitors", "pageviews",
          "trial_issued", "sub_requests", "bot_link_clicks", "app_clicks"]

# vhost_combined: host ip - - [time] "req" status bytes "ref" "ua"
NEW = re.compile(r'^(\S+) (\S+) \S+ \S+ \[([^\]]+)\] "([^"]*)" (\d{3}) \S+ "[^"]*" "([^"]*)"')
# combined (до 26.08.2026): ip - - [time] "req" status bytes "ref" "ua"
OLD = re.compile(r'^(\S+) \S+ \S+ \[([^\]]+)\] "([^"]*)" (\d{3}) \S+ "[^"]*" "([^"]*)"')
BOT_UA = re.compile(r"bot|crawl|spider|slurp|curl|wget|python|go-http|scan|headless", re.I)
STATIC = re.compile(r"\.(css|js|png|jpe?g|gif|svg|ico|webp|woff2?|ttf|txt|xml|json|map)(\?|$)", re.I)


def _open(path):
    return gzip.open(path, "rt", errors="replace") if path.endswith(".gz") else open(path, errors="replace")


def collect() -> dict:
    agg = defaultdict(lambda: {"requests": 0, "visitors": set(), "pageviews": 0,
                               "trial_issued": 0, "sub_requests": 0, "bot_link_clicks": 0,
                               "app_clicks": 0})
    files = sorted({f for g in LOG_GLOB for f in glob.glob(g) if not f.endswith(".bak")})
    for path in files:
        with _open(path) as fh:
            for line in fh:
                m = NEW.match(line)
                if m and m.group(2).count(".") == 3 or (m and ":" in m.group(2)):
                    host, ip, ts, req, status, ua = m.groups()
                else:
                    m = OLD.match(line)
                    if not m:
                        continue
                    ip, ts, req, status, ua = m.groups()
                    host = "-"
                try:
                    day = datetime.strptime(ts.split(":", 1)[0], "%d/%b/%Y").strftime("%Y-%m-%d")
                except ValueError:
                    continue
                parts = req.split()
                method, url = (parts[0], parts[1]) if len(parts) >= 2 else ("", "")
                a = agg[(day, host.lower())]
                a["requests"] += 1
                if not BOT_UA.search(ua):
                    a["visitors"].add((ip, ua))
                if (method == "GET" and status in ("200", "304") and not STATIC.search(url)
                        and not url.startswith(("/sub/", "/api/", "/static/", "/css/", "/js/", "/img/", "/fonts/"))):
                    a["pageviews"] += 1
                if method == "POST" and url.startswith("/trial/connect") and status == "303":
                    a["trial_issued"] += 1
                if method == "GET" and url.startswith("/sub/"):
                    a["sub_requests"] += 1
                if url.startswith("/go/bot"):
                    a["bot_link_clicks"] += 1
                if url.startswith("/go/app/"):
                    a["app_clicks"] += 1
    return agg


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    rows = {}
    if OUT.exists():
        with OUT.open() as fh:
            for r in csv.DictReader(fh):
                rows[(r["date"], r["host"])] = r
    today = datetime.now().strftime("%Y-%m-%d")
    for (day, host), a in collect().items():
        if day >= today:  # сегодняшний день ещё не закончился — посчитаем завтра
            continue
        rows[(day, host)] = {
            "date": day, "host": host, "requests": a["requests"],
            "visitors": len(a["visitors"]), "pageviews": a["pageviews"],
            "trial_issued": a["trial_issued"], "sub_requests": a["sub_requests"],
            "bot_link_clicks": a["bot_link_clicks"], "app_clicks": a["app_clicks"],
        }
    tmp = OUT.with_suffix(".tmp")
    with tmp.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for key in sorted(rows):
            w.writerow({k: rows[key].get(k, "") for k in FIELDS})
    os.replace(tmp, OUT)
    print(f"{OUT}: {len(rows)} строк")


if __name__ == "__main__":
    main()
