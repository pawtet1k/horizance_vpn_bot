"""Доступность нод для клиентов по my_logger/stats/node_uptime.csv (node_monitor.run_uptime_probe, с 16.10.26).
Только чтение. python3 analytics/uptime.py [дней=30]

Доступность = доля минутных проверок, в которые порт VPN ноды принял TCP-подключение.
«Сервис» = в эту минуту работала хотя бы одна нода (в подписке Happ все ноды, клиент может переключиться)
и «все ноды» — строже. Сбой = 2+ проверки подряд без ответа (одиночный промах — шум сети).
Не видит блокировок по пути из России: проверка идёт с NL."""
import csv, sys, collections
from datetime import datetime, timedelta, timezone

P = '/home/pashavpsadm/horizance_vpn_bot/my_logger/stats/node_uptime.csv'
days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
since = datetime.now(timezone.utc) - timedelta(days=days)

by_node = collections.defaultdict(list)   # node -> [(t, ok)]
by_minute = collections.defaultdict(dict)  # 'YYYY-MM-DDTHH:MM' -> {node: ok}
for r in csv.DictReader(open(P)):
    t = datetime.fromisoformat(r['utc'])
    if t < since:
        continue
    ok = r['ok'] == '1'
    by_node[r['node']].append((t, ok))
    by_minute[r['utc'][:16]][r['node']] = ok

if not by_node:
    sys.exit('нет данных')
first = min(t for v in by_node.values() for t, _ in v)
print(f"С {first:%d.%m %H:%M} UTC, проверка раз в минуту\n")
print(f"{'Нода':6}{'проверок':>10}{'доступность':>13}{'сбоев 2+ мин':>14}{'самый долгий':>14}")
for node, rows in sorted(by_node.items()):
    rows.sort()
    up = sum(ok for _, ok in rows)
    runs, cur = [], 0
    for _, ok in rows:
        if ok:
            if cur:
                runs.append(cur)
            cur = 0
        else:
            cur += 1
    if cur:
        runs.append(cur)
    long_runs = [x for x in runs if x >= 2]
    print(f"{node:6}{len(rows):>10}{100 * up / len(rows):>12.3f}%{len(long_runs):>14}{max(runs, default=0):>11} мин")

any_up = sum(any(m.values()) for m in by_minute.values())
all_up = sum(all(m.values()) for m in by_minute.values())
n = len(by_minute)
print(f"\nСервис (хотя бы одна нода): {100 * any_up / n:.3f}%   все ноды сразу: {100 * all_up / n:.3f}%   минут: {n}")
