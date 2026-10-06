#!/usr/bin/env python3
"""Короткая выжимка из JSON-отчёта GoAccess. Читает stdin, печатает сводку.

Вызывается из site_stats.sh — отдельным файлом, а не heredoc'ом внутри шелла,
чтобы не воевать с экранированием кавычек.
"""
import json
import sys


def num(n) -> str:
    return f"{n:,}".replace(",", " ")


def panel(data, key, title, limit=10):
    items = data.get(key, {}).get("data", [])
    if not items:
        return
    print(f"\n=== {title} ===")
    for item in items[:limit]:
        hits = item.get("hits", {}).get("count", 0)
        visitors = item.get("visitors", {}).get("count", 0)
        name = item.get("data", "?")
        print(f"  {num(visitors):>7} гостей {num(hits):>9} запр.  {str(name)[:68]}")


def main() -> None:
    # в логах попадаются запросы сканеров с сырыми байтами, и GoAccess
    # переносит их в JSON как есть — декодируем терпимо, иначе падаем
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print("GoAccess не отдал JSON — проверьте log-format в goaccess.conf",
              file=sys.stderr)
        sys.exit(1)

    general = data.get("general", {})
    print("=== ОБЩЕЕ ===")
    print(f"  период:            {general.get('start_date')} .. "
          f"{general.get('end_date')}")
    print(f"  запросов:          {num(general.get('total_requests', 0))}")
    print(f"  уникальных гостей: {num(general.get('unique_visitors', 0))}")
    print(f"  не найдено (404):  {num(general.get('unique404', 0))}")

    panel(data, "vhosts", "САЙТЫ")
    panel(data, "requests", "СТРАНИЦЫ")
    panel(data, "referring_sites", "ОТКУДА ПРИШЛИ")
    panel(data, "keyphrases", "ПОИСКОВЫЕ ФРАЗЫ")
    panel(data, "os", "ОПЕРАЦИОННЫЕ СИСТЕМЫ", 5)
    panel(data, "not_found", "НЕ НАЙДЕНО (404)", 5)


if __name__ == "__main__":
    main()
